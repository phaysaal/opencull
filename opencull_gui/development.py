"""The develop stage, with no interface attached.

This lived inside the HTTP server, which meant the only way to reach it was
to open a socket and ask. None of it is actually about HTTP: it reads the
project manifest, renders a recipe against a photograph, and registers what
it made. Holding it here lets the native window and the web page run the
same code instead of two drifting copies of it.

Nothing here writes to a source photograph. Renders go to the project's own
Developments and Previews directories, and the manifest is appended to, not
rewritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from PIL import Image, ImageOps

from darktable_engine import render_darktable_default
from development_engine import (
    RECIPE_ENGINE_REVISION,
    _srgb_to_linear_rec2020,
    render_recipe,
)
from raw_developer import render_baseline
from recipe_compiler import compile_recipe
from scan import RAW_EXTENSIONS, open_preview

from . import dialogs
from .adjustments import apply as apply_adjustments
from .jobs import JobError
from .project import load_project, project_sha256, update_project
from .raw_sources import RawSourceStore
from .shortlist import ShortlistError

BUILTIN_STYLES = ("calibrated", "standard", "signature", "creative", "personal")
ENGINES = ("default", "darktable")
DEMOSAIC_MODES = (
    "markesteijn-1-pass",
    "markesteijn-3-pass",
    "markesteijn-3-pass-vng",
)


def available_decoders() -> set[str]:
    """Which RAW decoders this machine actually has.

    darktable is preferred where it is installed. The LibRaw baseline is the
    deterministic fallback and comparison oracle: OpenCull's own renderer,
    decoding to scene-linear Rec.2020 without anyone's look applied.

    With neither, the only thing left is the rendering the camera embedded in
    the file, which is a judgement already made rather than a decode. That is
    honest to fall back to and dishonest to call a development.
    """
    decoders = set()
    try:
        from darktable_engine import find_darktable_cli

        find_darktable_cli()
        decoders.add("darktable")
    except Exception:
        pass
    if raw_baseline_tools_present():
        decoders.add("libraw")
    return decoders


def raw_baseline_tools_present() -> bool:
    """Whether every tool render_baseline shells out to is installed."""
    return all(
        shutil.which(name) for name in ("dcraw_emu", "raw-identify")
    ) and bool(shutil.which("magick") or shutil.which("convert"))


def suggested_filename(photo: str, variant: str) -> str:
    """A delivery name that says which frame and which treatment it is."""
    safe = "-".join(
        part for part in re.sub(r"[^A-Za-z0-9]+", "-", variant).split("-")
        if part).lower() or "render"
    return f"{Path(photo).stem}-{safe}.jpg"


def shrink_linear(tiff: Path, maximum: int, destination: Path) -> Path:
    """Resample a scene-linear baseline down to proof size.

    Done on the linear data rather than after display encoding, which is the
    only way a downscale averages light the way light actually averages.
    """
    array = np.asarray(tifffile.imread(tiff))
    if array.ndim != 3 or array.shape[2] < 3:
        raise ValueError("baseline decode is not a colour image")
    array = array[..., :3].astype(np.float32)
    height, width = array.shape[:2]
    longest = max(height, width)
    if longest > maximum:
        size = (max(1, round(width * maximum / longest)),
                max(1, round(height * maximum / longest)))
        # Pillow has no 16-bit colour mode, so each channel is resampled as a
        # single-channel float image and the three are stacked back together.
        array = np.stack([
            np.asarray(
                Image.fromarray(array[..., channel], mode="F").resize(
                    size, Image.Resampling.LANCZOS))
            for channel in range(3)
        ], axis=-1)
    tifffile.imwrite(
        destination, np.uint16(np.clip(array + 0.5, 0, 65535)))
    return destination


class DevelopmentWorkspace:
    """The renders, recipes and candidates belonging to one project."""

    def __init__(
        self,
        project_path: Path,
        project_layout: dict[str, Path],
        raw_sources: RawSourceStore,
        directions: Callable[[], dict] | None = None,
        decoders: set[str] | None = None,
    ):
        self.project_path = project_path
        self.project_layout = project_layout
        self.raw_sources = raw_sources
        # Edit directions come from the shortlist, which the develop stage
        # does not own. A caller that has no shortlist passes nothing and
        # gets a workspace with no candidates, which is the truth.
        self.directions = directions or (lambda: {"available": False})
        # Detected once and held, so that what the interface says is about to
        # happen and what the renderer then does cannot disagree. Installing
        # a decoder takes effect the next time a project is opened.
        self.decoders = available_decoders() if decoders is None else set(decoders)
        self.project: dict[str, Any] = load_project(project_path)
        self._native_locks: dict[str, threading.Lock] = {}

    def bind(self, project_path: Path, project_layout: dict[str, Path]) -> None:
        """Follow a project that has moved, as migration moves it."""
        self.project_path = project_path
        self.project_layout = project_layout
        self.project = load_project(project_path)

    # --- what there is to develop ---------------------------------------

    def payload(self) -> dict:
        # Guided development runs in a separate worker process and registers
        # its render directly in the durable project manifest.  Refresh here
        # so a completed job becomes visible without reopening the review.
        self.project = load_project(self.project_path)
        artifacts = self.project.get("artifacts", {})
        directions: list[dict] = []
        directions_path = ""
        try:
            direction_payload = self.directions()
            if direction_payload.get("available"):
                value = direction_payload.get("directions", {})
                directions = value.get("entries", []) if isinstance(value, dict) else []
                directions_path = str(direction_payload.get("path", ""))
        except ShortlistError:
            directions = []
        candidates = []
        for entry in directions:
            if not isinstance(entry, dict) or not entry.get("photo"):
                continue
            photo = str(entry["photo"])
            candidates.append({**entry, "raw_files": self.raw_files(photo)})
        return {"format": "opencull-development-workspace-v1",
                "variants": artifacts.get("renders", []) or [],
                "renderer_comparisons": artifacts.get("renderer_comparisons", []) or [],
                "recipes": artifacts.get("recipes", []) or [],
                "calibration": artifacts.get("calibration"),
                "candidates": candidates,
                "edit_directions_path": directions_path,
                "source_folder": self.project.get("source_folder"),
                "default_export_directory": str(self.project_layout["Exports"]),
                "rendering": self.project.get("rendering") or {
                    "engine": "darktable",
                    "demosaic": "markesteijn-3-pass",
                },
                "project_sha256": project_sha256(self.project_path)}

    def raw_files(self, photo: str) -> list[str]:
        """The external RAW files that were matched to one photograph."""
        raw = self.raw_sources.public()
        if not raw.get("configured"):
            return []
        root = Path(raw["root"])
        return [
            str((root / relative).resolve())
            for relative in raw.get("matches", {}).get(photo, [])
        ]

    def source_for(self, photo: str) -> Path:
        """The file a render is actually made from.

        A matched external RAW where there is one, and otherwise the
        photograph in the folder -- which may itself be a RAW.
        """
        matched = self.raw_files(photo)
        if matched:
            candidate = Path(matched[0]).expanduser()
            if candidate.is_file():
                return candidate
        return (Path(str(self.project.get("source_folder") or "")) / photo)

    def decoder_for(self, photo: str, decoders: set[str] | None = None) -> dict:
        """Which decoder this photograph will get, and what that means.

        Settled and reported before rendering rather than discovered inside
        it, so the interface can say what it is about to do. The three are
        not equivalent and the interface must not imply that they are.
        """
        available = self.decoders if decoders is None else decoders
        if self.source_for(photo).suffix.casefold() not in RAW_EXTENSIONS:
            return {"id": "rendered", "engine": "default",
                    "label": "Rendered file",
                    "note": "No RAW for this frame, so the rendered file is used."}
        if "darktable" in available:
            return {"id": "darktable", "engine": "darktable",
                    "label": "darktable",
                    "note": "A RAW, demosaiced by darktable."}
        if "libraw" in available:
            return {"id": "libraw", "engine": "default",
                    "label": "OpenCull · LibRaw",
                    "note": ("A RAW, decoded by OpenCull's own LibRaw baseline "
                             "to scene-linear Rec.2020.")}
        return {"id": "embedded", "engine": "default",
                "label": "Camera preview",
                "note": ("Neither darktable nor LibRaw is installed, so the "
                         "camera's own embedded rendering is used. That is a "
                         "judgement already made, not a development.")}

    def treatments(self, photo: str) -> list[dict]:
        """The treatments that can actually be rendered for one photograph.

        A treatment nobody can render is worse than no treatment at all: the
        interface offers it, the photographer chooses it, and the render
        fails. So this reports only what has a recipe behind it.
        """
        workspace = self.payload()
        entry = next((item for item in workspace.get("candidates", [])
                      if item.get("photo") == photo), None)
        # The calibrated baseline is the photograph as the camera and the
        # demosaic saw it, with no interpretation on top. It needs no recipe
        # and no edit direction, so every photograph has it. This is what
        # develop means for a folder that has only been culled: see the
        # picture properly, before anything has been suggested about it.
        available: list[dict] = [{
            "id": "calibrated", "name": "Calibrated baseline",
            "intent": "Neutral technical rendering, nothing interpreted.",
            "kind": "builtin"}]
        if entry is not None:
            for style in BUILTIN_STYLES:
                if style == "calibrated" or not entry.get(f"{style}_recipe"):
                    continue
                available.append({
                    "id": style,
                    "name": str(entry.get(f"{style}_title") or style.title()),
                    "intent": str(entry.get(f"{style}_intent") or ""),
                    "kind": "builtin"})
        for item in workspace.get("recipes", []):
            if not isinstance(item, dict):
                continue
            if item.get("format") != "darkimiya-portable-recipe-v1":
                continue
            if str(item.get("photo", "")) not in {"", photo}:
                continue
            available.append({
                "id": str(item.get("id", "")),
                "name": str(item.get("name") or "Imported recipe"),
                "intent": f"Imported from {Path(str(item.get('source_path', ''))).name}",
                "kind": "imported"})
        return available

    # --- what was asked for, and whether it was done --------------------

    def suggestion_for(self, photo: str, style: str) -> str:
        """What this treatment was told to do, in the words it was told in.

        Verification asks whether a rendering did what was asked. The
        calibrated baseline is asked to do nothing -- it interprets nothing
        by definition -- so there is no claim to check and this answers
        empty rather than inventing one.
        """
        if style == "calibrated":
            return ""
        entry = next((item for item in self.payload().get("candidates", [])
                      if item.get("photo") == photo), None)
        if entry is None:
            return ""
        parts = [
            str(entry.get(f"{style}_intent") or "").strip(),
            str(entry.get(f"{style}_instructions") or "").strip(),
        ]
        guardrails = str(entry.get("guardrails") or "").strip()
        if guardrails:
            parts.append(f"Guardrails: {guardrails}")
        return "\n\n".join(part for part in parts if part)

    def render_record(self, photo: str, variant: str) -> dict | None:
        """The most recent registered render of one treatment of one frame.

        Asked without rendering anything: the manifest already knows what
        was made, and re-rendering to find out where a file is would cost
        the thing it is trying to look up.
        """
        self.project = load_project(self.project_path)
        found = None
        for item in self.project.get("artifacts", {}).get("renders", []) or []:
            if not isinstance(item, dict):
                continue
            if (item.get("source_photo") == photo
                    and item.get("variant") == variant
                    and item.get("path")):
                found = item
        return found

    def verifications(self) -> list[dict]:
        return [
            item for item in
            self.project.get("artifacts", {}).get("verifications", []) or []
            if isinstance(item, dict)
        ]

    def verification_for(self, developed: str | Path) -> dict | None:
        """The certificate covering one rendered file, if there is one.

        A certificate is bound to the exact bytes it judged, so it belongs
        to one render and not to the treatment in general. Re-rendering
        produces a different file, which correctly has no certificate yet.
        """
        wanted = str(Path(str(developed)).expanduser().resolve())
        self.project = load_project(self.project_path)
        found = None
        for item in self.verifications():
            path = Path(str(item.get("path", "")))
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("format") != "opencull-semantic-verification-v1":
                continue
            evidence = value.get("evidence", {}).get("developed", {})
            if str(evidence.get("path", "")) == wanted:
                found = value
        return found

    # --- rendering ------------------------------------------------------

    def compiled_recipe(self, photo: str, style: str, engine: str) -> dict:
        """What the renderer will actually execute for one treatment.

        The recipe is the only thing a rendering comes from, so fine tuning
        needs it in hand before anything is rendered. Settling it separately
        also means the same compile answers the page and the render, rather
        than the page describing one recipe and the renderer running another.
        """
        return self._prepare(photo, style, engine)["recipe"]

    def _prepare(self, photo: str, style: str, engine: str) -> dict:
        """Settle the source photograph and the recipe for one treatment."""
        builtin_styles = set(BUILTIN_STYLES)
        if engine not in set(ENGINES):
            raise ValueError("unsupported development preview engine")
        workspace = self.payload()
        entry = next((item for item in workspace.get("candidates", [])
                      if item.get("photo") == photo), None)
        if entry is None:
            if style != "calibrated":
                raise ValueError("photograph has no edit direction")
            # The baseline interprets nothing, so it needs no direction. Only
            # the RAW match matters, and that is indexed separately.
            entry = {"photo": photo, "raw_files": self.raw_files(photo)}
        reference = (Path(str(workspace["source_folder"])) / photo).resolve()
        if not reference.is_file():
            raise ValueError("reference photograph is unavailable")
        # A matched RAW is only worth reaching for if something here can
        # decode it. Otherwise the reference photograph is the source, and
        # the render is honestly the rendering that already existed.
        decoders = self.decoders
        source = reference
        if engine == "darktable" or "libraw" in decoders:
            for matched in entry.get("raw_files") or []:
                candidate = Path(str(matched)).expanduser().resolve()
                if candidate.is_file():
                    source = candidate
                    break
        source_kind = "raw" if source.suffix.casefold() not in {".jpg", ".jpeg"} else "jpeg"
        recipe_value = entry.get(f"{style}_recipe")
        portable = None
        if style not in builtin_styles:
            portable = next((
                item for item in workspace.get("recipes", [])
                if isinstance(item, dict)
                and item.get("format") == "darkimiya-portable-recipe-v1"
                and item.get("id") == style
                and str(item.get("photo", "")) in {"", photo}
            ), None)
            if portable is None:
                raise ValueError("unsupported development preview treatment")
            recipe_value = portable.get("recipe")
        if style == "calibrated":
            recipe = {
                "format": "opencull-development-recipe-v1",
                "source_photo": photo, "source_kind": source_kind,
                "style": style, "title": "Calibrated baseline",
                "intent": "Neutral technical preview.",
                "working_space": "scene-linear-rec2020-d65",
                "operations": [], "guardrails": [], "diagnostics": [],
                "coverage": {"instructions": 0, "executable": 0,
                             "guardrails": 0, "unsupported": 0},
            }
        elif portable is not None:
            if not isinstance(recipe_value, dict) or not isinstance(
                recipe_value.get("operations"), list
            ):
                raise ValueError("portable recipe has no executable operations")
            recipe = dict(recipe_value)
            recipe.update({
                "format": "opencull-development-recipe-v1",
                "source_photo": photo,
                "source_kind": source_kind,
                "style": style,
                "title": str(portable.get("name") or recipe.get("title") or style),
            })
        else:
            if not recipe_value:
                raise ValueError("selected treatment has no executable recipe")
            recipe = compile_recipe(
                photo, style, str(entry.get(f"{style}_title", style.title())),
                str(entry.get(f"{style}_intent", "")), recipe_value,
                str(entry.get("guardrails", "")), source_kind)
        return {"recipe": recipe, "source": source, "reference": reference,
                "source_kind": source_kind}

    def recipe_preview(
        self, photo: str, style: str, engine: str, demosaic: str,
        maximum: int, adjustments: dict | None = None,
    ) -> Path:
        """Render one bounded local proof without registering an export artifact.

        ``adjustments`` are the photographer's bounded changes to the
        compiled operations. They are folded in before the cache identity is
        taken, so an adjusted proof is a different rendering with a
        different name rather than one quietly overwriting the other.
        """
        if demosaic not in set(DEMOSAIC_MODES):
            raise ValueError("unsupported development preview demosaic mode")
        prepared = self._prepare(photo, style, engine)
        recipe = prepared["recipe"]
        source = prepared["source"]
        reference = prepared["reference"]
        source_kind = prepared["source_kind"]
        decoders = self.decoders
        if adjustments:
            recipe = apply_adjustments(recipe, adjustments)
        source_stat = source.stat()
        identity = hashlib.sha256(json.dumps({
            "photo": photo, "style": style, "engine": engine,
            "demosaic": demosaic, "maximum": maximum,
            "recipe": recipe, "source": str(source),
            "mtime": source_stat.st_mtime_ns, "size": source_stat.st_size,
            "renderer": RECIPE_ENGINE_REVISION,
        }, sort_keys=True).encode()).hexdigest()[:24]
        destination = self.project_layout["Previews"] / "DevelopRecipes" / (
            f"{Path(photo).stem}.{style}.{engine}.{identity}.jpg")
        if destination.is_file():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".develop-preview-", dir=destination.parent,
        ) as temporary:
            work = Path(temporary)
            # open_preview, not Image.open: the photographs in the folder may
            # themselves be RAW, which Pillow cannot read. For those it is the
            # camera's own embedded rendering, which is what "the reference"
            # honestly means when no demosaic has been run.
            small = open_preview(reference)
            small.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
            small_reference = work / "reference.jpg"
            small.save(small_reference, "JPEG", quality=94)
            if engine == "darktable":
                native_identity = hashlib.sha256(
                    f"{source}|{source_stat.st_mtime_ns}|{source_stat.st_size}|"
                    f"{maximum}|{demosaic}".encode()).hexdigest()[:20]
                native_cache = self.project_layout["Previews"] / "DevelopNative" / (
                    f"{source.stem}.{maximum}.{demosaic}.{native_identity}.jpg")
                native_cache.parent.mkdir(parents=True, exist_ok=True)
                lock = self._native_locks.setdefault(native_identity, threading.Lock())
                with lock:
                    if not native_cache.is_file():
                        native = render_darktable_default(
                            source, work / "darktable", max_dimension=maximum,
                            demosaic_mode=demosaic)
                        staged_native = native_cache.with_name(
                            f".{native_cache.name}.{secrets.token_hex(4)}.tmp")
                        shutil.copy2(Path(native["output"]["path"]), staged_native)
                        os.replace(staged_native, native_cache)
                baseline = self._from_display(native_cache, work)
                calibration_reference = None
            elif source_kind == "raw" and "libraw" in decoders:
                # OpenCull's own renderer: the deterministic fallback the
                # renderer plan describes. It already produces the
                # scene-linear Rec.2020 baseline render_recipe is written
                # against, so it is used as it is rather than round-tripped
                # down through a display JPEG and back up again.
                baseline = self._libraw_baseline(
                    source, source_stat, maximum, work)
                calibration_reference = None
            else:
                baseline = self._from_display(small_reference, work)
                calibration_reference = small_reference
            result = render_recipe(
                baseline, recipe, work / "render", allow_incomplete=True,
                reference_jpeg=calibration_reference)
            temporary_output = destination.with_name(
                f".{destination.name}.{secrets.token_hex(4)}.tmp")
            shutil.copy2(Path(result["output"]["path"]), temporary_output)
            os.replace(temporary_output, destination)
        return destination

    @staticmethod
    def _from_display(image: Path, work: Path) -> Path:
        """Lift a display-encoded rendering into the scene-linear baseline."""
        with Image.open(image) as opened:
            rgb = np.asarray(
                ImageOps.exif_transpose(opened).convert("RGB"),
                dtype=np.float32) / 255.0
        linear = np.clip(_srgb_to_linear_rec2020(rgb), 0.0, 1.0)
        baseline = work / "baseline.tiff"
        tifffile.imwrite(baseline, np.uint16(linear * 65535.0 + 0.5))
        return baseline

    def _libraw_baseline(
        self, source: Path, source_stat, maximum: int, work: Path,
    ) -> Path:
        """Decode a RAW with LibRaw, and keep the result at proof size.

        The decode is the expensive part and does not depend on the recipe,
        so it is cached beside the darktable decodes and shared by every
        treatment of the same photograph.
        """
        identity = hashlib.sha256(
            f"{source}|{source_stat.st_mtime_ns}|{source_stat.st_size}|"
            f"{maximum}|libraw".encode()).hexdigest()[:20]
        cache = self.project_layout["Previews"] / "DevelopBaselines" / (
            f"{source.stem}.{maximum}.libraw.{identity}.tiff")
        cache.parent.mkdir(parents=True, exist_ok=True)
        lock = self._native_locks.setdefault(identity, threading.Lock())
        with lock:
            if not cache.is_file():
                record = render_baseline(source, work / "libraw")
                full = Path(record["outputs"]["linear_tiff"]["path"])
                staged = cache.with_name(
                    f".{cache.name}.{secrets.token_hex(4)}.tmp")
                shrink_linear(full, maximum, staged)
                os.replace(staged, cache)
        return cache

    # --- recipes --------------------------------------------------------

    def import_recipe(self, path: Path | None = None) -> dict:
        """Choose and register a portable, executable local development recipe."""
        if path is None:
            try:
                chosen = dialogs.choose_files("Import a Darkimiya recipe")[0]
            except dialogs.DialogError as exc:
                raise JobError(str(exc)) from exc
            path = Path(chosen)
        path = Path(path).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("recipe must be an available JSON file smaller than 2 MiB")
        try:
            imported = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read recipe JSON: {exc}") from exc
        if not isinstance(imported, dict):
            raise ValueError("recipe JSON must be an object")
        recipe = imported.get("recipe", imported)
        if not isinstance(recipe, dict) or not isinstance(recipe.get("operations"), list):
            raise ValueError(
                "recipe JSON must contain an executable operations list")
        if any(not isinstance(operation, dict) for operation in recipe["operations"]):
            raise ValueError("every recipe operation must be an object")
        canonical = json.dumps(recipe, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        name = str(
            imported.get("name") or imported.get("title")
            or recipe.get("title") or path.stem
        ).strip()[:80] or "Imported recipe"
        recipe_id = f"custom-{digest[:16]}"
        artifact = {
            "format": "darkimiya-portable-recipe-v1",
            "id": recipe_id,
            "name": name,
            "origin": "imported",
            "source_path": str(path),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "imported_at": datetime.now(UTC).isoformat(),
            "photo": str(imported.get("photo", "")),
            "recipe": recipe,
        }
        recipes = list(
            self.project.get("artifacts", {}).get("recipes", []) or [])
        recipes = [
            item for item in recipes
            if not (isinstance(item, dict) and item.get("id") == recipe_id)
        ]
        recipes.append(artifact)
        self.project = update_project(
            self.project_path, stage="develop", artifacts={"recipes": recipes})
        return {"recipe": artifact, "development": self.payload()}

    def render_portable(
        self, photo: str, style: str, engine: str, demosaic: str,
    ) -> dict:
        """Render an imported recipe at the reference photograph's full dimensions."""
        workspace = self.payload()
        portable = next((
            item for item in workspace.get("recipes", [])
            if isinstance(item, dict)
            and item.get("format") == "darkimiya-portable-recipe-v1"
            and item.get("id") == style
            and str(item.get("photo", "")) in {"", photo}
        ), None)
        if portable is None:
            raise ValueError("portable recipe is not registered for this photograph")
        return self.render_full(
            photo, style, engine, demosaic,
            provenance=str(portable.get("source_path", "")))

    def render_full(
        self, photo: str, style: str, engine: str, demosaic: str,
        provenance: str = "", adjustments: dict | None = None,
    ) -> dict:
        """Render a treatment at the photograph's own dimensions, and record it.

        The proof on screen is deliberately bounded, so it is not the thing to
        hand anybody. This makes the full-size render and registers it in the
        project manifest, which is what makes it a render rather than another
        preview -- and what lets it be exported afterwards.

        A render carrying the photographer's adjustments is registered as its
        own variant. Delivering it must never be mistaken for delivering the
        treatment as the model wrote it.
        """
        workspace = self.payload()
        reference = (Path(str(workspace["source_folder"])) / photo).resolve()
        if not reference.is_file():
            raise ValueError("reference photograph is unavailable")
        maximum = max(open_preview(reference).size)
        preview = self.recipe_preview(
            photo, style, engine, demosaic, maximum, adjustments)
        digest = hashlib.sha256(preview.read_bytes()).hexdigest()
        destination = self.project_layout["Developments"] / (
            f"{Path(photo).stem}.{style}.{engine}.{digest[:12]}.jpg")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file():
            shutil.copy2(preview, destination)
        variant = style if engine == "default" else f"{style}-darktable-guided"
        artifact = {
            "variant": f"{variant}-adjusted" if adjustments else variant,
            "source_photo": photo,
            "path": str(destination),
            "provenance": provenance or str(
                workspace.get("edit_directions_path") or ""),
            "recipe_revision": 2 if adjustments else 1,
            "adjustments": list((adjustments or {}).keys()),
            "sha256": digest,
            "created_at": datetime.now(UTC).isoformat(),
        }
        renders = list(
            self.project.get("artifacts", {}).get("renders", []) or [])
        if not any(
            isinstance(item, dict)
            and item.get("source_photo") == photo
            and item.get("variant") == artifact["variant"]
            and item.get("sha256") == digest
            for item in renders
        ):
            renders.append(artifact)
        self.project = update_project(
            self.project_path, stage="develop", artifacts={"renders": renders})
        return {"render": artifact, "development": self.payload()}

    # --- export ---------------------------------------------------------

    def export_payload(self) -> dict:
        self.project = load_project(self.project_path)
        artifacts = self.project.get("artifacts", {})
        render_history = [
            item for item in artifacts.get("renders", []) or []
            if isinstance(item, dict) and item.get("path")
        ]
        # The manifest is intentionally append-only evidence.  The delivery UI,
        # however, should offer one current choice per photo/treatment instead
        # of presenting every historical render revision as a duplicate.
        current: dict[tuple[str, str], dict] = {}
        for item in render_history:
            key = (str(item.get("source_photo", "")),
                   str(item.get("variant") or item.get("style") or "render"))
            previous = current.get(key)
            if previous is None or (
                str(item.get("created_at", "")),
                int(item.get("recipe_revision", 0) or 0),
            ) >= (
                str(previous.get("created_at", "")),
                int(previous.get("recipe_revision", 0) or 0),
            ):
                current[key] = item
        renders = list(current.values())
        renders.sort(key=lambda item: (
            str(item.get("source_photo", "")).casefold(),
            str(item.get("variant", "")).casefold()))
        for item in renders:
            item["suggested_filename"] = suggested_filename(
                str(item.get("source_photo") or item.get("path") or "developed"),
                str(item.get("variant") or "render"))
        return {"format": "opencull-export-workspace-v1",
                "exports": artifacts.get("exports", []) or [],
                "renders": renders,
                "render_history_count": len(render_history),
                "hidden_render_revisions": len(render_history) - len(renders),
                "default_export_directory": str(self.project_layout["Exports"]),
                "project_sha256": project_sha256(self.project_path)}

    def export_render(self, source: str, destination: str,
                      sequence: int | None = None) -> dict:
        """Copy one registered render out to where it was asked for.

        Only a render the manifest knows about can leave, so an export is
        always of something with a recorded provenance. An existing file at
        the destination is never written over: the copy takes the next free
        name and the record says both what was asked for and what was made.
        """
        allowed = {str(Path(str(item.get("path", ""))).expanduser().resolve())
                   for item in self.project.get("artifacts", {}).get("renders", [])
                   if isinstance(item, dict)}
        source_path = str(Path(source).expanduser().resolve())
        if source_path not in allowed or not Path(source_path).is_file():
            raise ValueError("source render is not linked to this project")
        requested_path = Path(destination).expanduser().resolve()
        destination_path = requested_path
        revision = 2
        while destination_path.exists():
            destination_path = requested_path.with_name(
                f"{requested_path.stem}-{revision}{requested_path.suffix}")
            revision += 1
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        record = {"source": source_path, "destination": str(destination_path),
                  "requested_destination": str(requested_path),
                  "created_at": datetime.now(UTC).isoformat(),
                  # Where this frame sits in the delivery's telling. An
                  # album's order is a decision like any other, so it is
                  # recorded like any other.
                  **({"sequence": int(sequence)} if sequence is not None
                     else {}),
                  "sha256": hashlib.sha256(destination_path.read_bytes()).hexdigest()}
        exports = list(self.project.get("artifacts", {}).get("exports", []) or [])
        exports.append(record)
        self.project = update_project(self.project_path, stage="export",
                                      artifacts={"exports": exports})
        return record
