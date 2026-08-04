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
from development_engine import _srgb_to_linear_rec2020, render_recipe
from recipe_compiler import compile_recipe
from scan import open_preview

from . import dialogs
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


class DevelopmentWorkspace:
    """The renders, recipes and candidates belonging to one project."""

    def __init__(
        self,
        project_path: Path,
        project_layout: dict[str, Path],
        raw_sources: RawSourceStore,
        directions: Callable[[], dict] | None = None,
    ):
        self.project_path = project_path
        self.project_layout = project_layout
        self.raw_sources = raw_sources
        # Edit directions come from the shortlist, which the develop stage
        # does not own. A caller that has no shortlist passes nothing and
        # gets a workspace with no candidates, which is the truth.
        self.directions = directions or (lambda: {"available": False})
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

    # --- rendering ------------------------------------------------------

    def recipe_preview(
        self, photo: str, style: str, engine: str, demosaic: str,
        maximum: int,
    ) -> Path:
        """Render one bounded local proof without registering an export artifact."""
        builtin_styles = set(BUILTIN_STYLES)
        if engine not in set(ENGINES):
            raise ValueError("unsupported development preview engine")
        if demosaic not in set(DEMOSAIC_MODES):
            raise ValueError("unsupported development preview demosaic mode")
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
        source = reference
        if engine == "darktable" and entry.get("raw_files"):
            candidate = Path(str(entry["raw_files"][0])).expanduser().resolve()
            if candidate.is_file():
                source = candidate
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
        source_stat = source.stat()
        identity = hashlib.sha256(json.dumps({
            "photo": photo, "style": style, "engine": engine,
            "demosaic": demosaic, "maximum": maximum,
            "recipe": recipe, "source": str(source),
            "mtime": source_stat.st_mtime_ns, "size": source_stat.st_size,
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
                baseline_source = native_cache
                calibration_reference = None
            else:
                baseline_source = small_reference
                calibration_reference = small_reference
            with Image.open(baseline_source) as opened:
                rgb = np.asarray(
                    ImageOps.exif_transpose(opened).convert("RGB"),
                    dtype=np.float32) / 255.0
            linear = np.clip(_srgb_to_linear_rec2020(rgb), 0.0, 1.0)
            baseline = work / "baseline.tiff"
            tifffile.imwrite(baseline, np.uint16(linear * 65535.0 + 0.5))
            result = render_recipe(
                baseline, recipe, work / "render", allow_incomplete=True,
                reference_jpeg=calibration_reference)
            temporary_output = destination.with_name(
                f".{destination.name}.{secrets.token_hex(4)}.tmp")
            shutil.copy2(Path(result["output"]["path"]), temporary_output)
            os.replace(temporary_output, destination)
        return destination

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
        reference = (Path(str(workspace["source_folder"])) / photo).resolve()
        if not reference.is_file():
            raise ValueError("reference photograph is unavailable")
        maximum = max(open_preview(reference).size)
        preview = self.recipe_preview(photo, style, engine, demosaic, maximum)
        digest = hashlib.sha256(preview.read_bytes()).hexdigest()
        destination = self.project_layout["Developments"] / (
            f"{Path(photo).stem}.{style}.{engine}.{digest[:12]}.jpg")
        if not destination.is_file():
            shutil.copy2(preview, destination)
        artifact = {
            "variant": style if engine == "default" else f"{style}-darktable-guided",
            "source_photo": photo,
            "path": str(destination),
            "provenance": str(portable.get("source_path", "")),
            "recipe_revision": 1,
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
