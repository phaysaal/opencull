"""Local HTTP server for immutable evidence and separate human review state."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import re
import subprocess
import secrets
import sys
import io
import shutil
import os
import tempfile
import threading
import numpy as np
import tifffile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

from darktable_engine import render_darktable_default
from development_engine import _srgb_to_linear_rec2020, render_recipe
from recipe_compiler import compile_recipe

from .actions import ActionController, ActionError, export_bytes
from .photos import PhotoError, PhotoStore, PreviewManager
from .report import ReportIndex
from .reviews import ReviewError, ReviewStore
from .xmp import xmp_zip
from .jobs import JobError, JobManager
from .providers import ProviderError, ProviderStore
from .faces import FaceError, FaceStore
from .shortlist import ShortlistError, ShortlistIndex, load_shortlist
from .shortlist_reviews import (
    default_shortlist_review_path,
    ShortlistReviewError,
    ShortlistReviewStore,
)
from .raw_sources import RawSourceError, RawSourceStore
from .project import (
    ensure_project_layout, legacy_migration_preview, load_or_create_folder_project,
    import_legacy_development_artifacts, load_project, migrate_legacy_project, project_sha256,
    register_file_artifact, update_project,
)


STATIC_ROOT = Path(__file__).with_name("static")


def _atomic_json_file(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        report: ReportIndex,
        photos: PhotoStore,
        reviews: ReviewStore,
        measurements: dict | None = None,
        manifest_path: str | None = None,
        preview_workers: int | None = None,
        jobs: JobManager | None = None,
        providers: ProviderStore | None = None,
        faces: FaceStore | None = None,
        shortlist_path: Path | None = None,
        shortlist_default_path: Path | None = None,
        shutdown_jobs: bool = True,
    ):
        super().__init__(address, ReviewHandler)
        self.report = report
        self.photos = photos
        self.reviews = reviews
        self.csrf_token = secrets.token_urlsafe(32)
        self.previews = PreviewManager(photos, workers=preview_workers)
        self.jobs = jobs
        self.shutdown_jobs = shutdown_jobs
        self.providers = providers
        self.faces = faces
        legacy_project_path = report.path.with_suffix(".opencull-project.json")
        self.project_path, self.project = load_or_create_folder_project(
            photos.root, report.path.stem, legacy_project_path)
        self.project = import_legacy_development_artifacts(
            self.project_path, legacy_project_path)
        self.project_layout = ensure_project_layout(photos.root)
        self.raw_sources = RawSourceStore(
            self.project_layout["Reports"] /
            f"{report.path.stem}.raw-source.json", report)
        self.actions = ActionController(
            report, reviews, photos, self.project_path, self.raw_sources)
        self.project = register_file_artifact(
            self.project_path, "culling_report", report.path,
            stage="cull", singleton=True)
        self.shortlist: ShortlistIndex | None = None
        self.shortlist_reviews: ShortlistReviewStore | None = None
        self.default_shortlist_path = (
            shortlist_default_path.expanduser().resolve()
            if shortlist_default_path is not None
            else self.project_layout["Reports"] /
                 f"{report.path.stem}.professional-shortlist.json")
        if shortlist_path is not None and shortlist_path.is_file():
            self.load_shortlist(shortlist_path)
        self.payload = {
            **report.public_payload(photos.root),
            "measurements": measurements or {},
            "manifest_path": manifest_path,
            "raw_source": self.raw_sources.public(),
            "project": {**self.project, "path": str(self.project_path),
                         "sha256": project_sha256(self.project_path)},
        }

    def project_payload(self) -> dict:
        self.project_path, self.project = load_or_create_folder_project(
            self.photos.root, self.report.path.stem,
            self.report.path.with_suffix(".opencull-project.json"))
        if self.reviews.path.is_file():
            self.project = register_file_artifact(
                self.project_path, "culling_review", self.reviews.path,
                stage="cull")
        return {**self.project, "path": str(self.project_path),
                "sha256": project_sha256(self.project_path)}

    def migration_payload(self) -> dict:
        return legacy_migration_preview(self.project_path, self.photos.root)

    def migrate_project(self, confirmation: str) -> dict:
        result = migrate_legacy_project(
            self.project_path, self.photos.root, confirmation)
        self.project_path = Path(result["path"])
        self.project_layout = ensure_project_layout(self.photos.root)
        self.project = result["project"]
        self.actions.bind_project(self.project_path)
        self.project = register_file_artifact(
            self.project_path, "culling_report", self.report.path,
            stage="cull", singleton=True)
        self.payload["project"] = {
            **self.project, "path": str(self.project_path),
            "sha256": project_sha256(self.project_path),
        }
        result["project"] = self.payload["project"]
        return result

    def style_profile_payload(self) -> dict:
        requested = self.project.get("active_style_profile")
        if not requested:
            return {"available": False, "reason": "no personal style profile is selected"}
        path = Path(str(requested)).expanduser().resolve()
        if not path.is_file():
            return {"available": False, "reason": "selected style profile is unavailable",
                    "path": str(path)}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShortlistError(f"cannot read style profile: {exc}") from exc
        if value.get("format") != "opencull-personal-style-profile-v1":
            raise ShortlistError("unsupported personal style profile")
        return {"available": True, "path": str(path), "profile": value}

    def development_payload(self) -> dict:
        # Guided development runs in a separate worker process and registers
        # its render directly in the durable project manifest.  Refresh here
        # so a completed job becomes visible without reopening the review.
        self.project = load_project(self.project_path)
        artifacts = self.project.get("artifacts", {})
        directions: list[dict] = []
        directions_path = ""
        try:
            direction_payload = self.edit_directions_payload()
            if direction_payload.get("available"):
                value = direction_payload.get("directions", {})
                directions = value.get("entries", []) if isinstance(value, dict) else []
                directions_path = str(direction_payload.get("path", ""))
        except ShortlistError:
            directions = []
        raw = self.raw_sources.public()
        raw_root = Path(raw["root"]) if raw.get("configured") else None
        candidates = []
        for entry in directions:
            if not isinstance(entry, dict) or not entry.get("photo"):
                continue
            photo = str(entry["photo"])
            raw_files = [
                str((raw_root / relative).resolve())
                for relative in raw.get("matches", {}).get(photo, [])
            ] if raw_root else []
            candidates.append({**entry, "raw_files": raw_files})
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

    def development_recipe_preview(
        self, photo: str, style: str, engine: str, demosaic: str,
        maximum: int,
    ) -> Path:
        """Render one bounded local proof without registering an export artifact."""
        builtin_styles = {"calibrated", "standard", "signature", "creative", "personal"}
        if engine not in {"default", "darktable"}:
            raise ValueError("unsupported development preview engine")
        if demosaic not in {
            "markesteijn-1-pass", "markesteijn-3-pass",
            "markesteijn-3-pass-vng",
        }:
            raise ValueError("unsupported development preview demosaic mode")
        workspace = self.development_payload()
        entry = next((item for item in workspace.get("candidates", [])
                      if item.get("photo") == photo), None)
        if entry is None:
            raise ValueError("photograph has no edit direction")
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
            with Image.open(reference) as opened:
                small = ImageOps.exif_transpose(opened).convert("RGB")
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
                locks = getattr(self, "_development_preview_locks", None)
                if locks is None:
                    locks = {}
                    self._development_preview_locks = locks
                lock = locks.setdefault(native_identity, threading.Lock())
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

    def import_development_recipe(self) -> dict:
        """Choose and register a portable, executable local development recipe."""
        if sys.platform != "darwin":
            raise JobError("native recipe selection is available only on macOS")
        result = subprocess.run(
            [
                "osascript", "-e",
                'POSIX path of (choose file with prompt "Import a Darkimiya recipe" '
                'of type {"public.json"})',
            ],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if result.returncode != 0:
            if "-128" in result.stderr:
                raise JobError("recipe selection was cancelled")
            raise JobError(f"recipe chooser failed: {result.stderr.strip()}")
        path = Path(result.stdout.strip()).expanduser().resolve()
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
            "imported_at": datetime.now(timezone.utc).isoformat(),
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
        return {"recipe": artifact, "development": self.development_payload()}

    def render_portable_development_recipe(
        self, photo: str, style: str, engine: str, demosaic: str,
    ) -> dict:
        """Render an imported recipe at the reference photograph's full dimensions."""
        workspace = self.development_payload()
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
        with Image.open(reference) as opened:
            maximum = max(opened.size)
        preview = self.development_recipe_preview(
            photo, style, engine, demosaic, maximum)
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
            "created_at": datetime.now(timezone.utc).isoformat(),
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
        return {"render": artifact, "development": self.development_payload()}

    def verification_payload(self) -> dict:
        return {"format": "opencull-verification-workspace-v1",
                "certificates": self.project.get("artifacts", {}).get("verifications", []) or [],
                "project_sha256": project_sha256(self.project_path)}

    def export_payload(self) -> dict:
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
        export_directory = self.project_layout["Exports"]
        for item in renders:
            source_name = Path(str(item.get("source_photo") or
                                   item.get("path") or "developed")).stem
            variant = str(item.get("variant") or "render")
            safe_variant = "-".join(
                part for part in re.sub(r"[^A-Za-z0-9]+", "-", variant).split("-")
                if part).lower() or "render"
            item["suggested_filename"] = f"{source_name}-{safe_variant}.jpg"
        return {"format": "opencull-export-workspace-v1",
                "exports": artifacts.get("exports", []) or [],
                "renders": renders,
                "render_history_count": len(render_history),
                "hidden_render_revisions": len(render_history) - len(renders),
                "default_export_directory": str(export_directory),
                "project_sha256": project_sha256(self.project_path)}

    def export_render(self, source: str, destination: str) -> dict:
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
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "sha256": hashlib.sha256(destination_path.read_bytes()).hexdigest()}
        exports = list(self.project.get("artifacts", {}).get("exports", []) or [])
        exports.append(record)
        self.project = update_project(self.project_path, stage="export",
                                      artifacts={"exports": exports})
        return record
        return self.export_payload()

    def load_shortlist(self, path: Path) -> dict:
        shortlist = load_shortlist(path, self.report, self.photos.root)
        reviews = ShortlistReviewStore(
            self.project_layout["Reviews"] /
            f"{shortlist.path.stem}.review.json", shortlist)
        migration = reviews.merge_legacy(
            default_shortlist_review_path(shortlist.path))
        self.shortlist = shortlist
        self.shortlist_reviews = reviews
        self.project = register_file_artifact(
            self.project_path, "shortlist", shortlist.path,
            stage="shortlist")
        if migration["merged"]:
            self.project = register_file_artifact(
                self.project_path, "shortlist_review", reviews.path,
                stage="shortlist")
        return self.shortlist_payload()

    def shortlist_payload(self) -> dict:
        if self.shortlist is None or self.shortlist_reviews is None:
            return {
                "available": False,
                "default_path": str(self.default_shortlist_path),
            }
        return {
            "available": True,
            "shortlist_path": str(self.shortlist.path),
            "shortlist": self.shortlist.data,
            "review": self.shortlist_reviews.public_state(),
        }

    def edit_directions_payload(self) -> dict:
        if self.shortlist is None or self.shortlist_reviews is None:
            return {"available": False, "reason": "shortlist is not loaded"}
        review_state = self.shortlist_reviews.public_state()
        revision = review_state["revision"]
        base_path = self.project_layout["Recipes"] / (
            f"{self.shortlist.path.stem}.edit-directions-r{revision}.json")
        shortlist_sha = hashlib.sha256(self.shortlist.path.read_bytes()).hexdigest()
        next_path = base_path
        version = 2
        while next_path.exists():
            next_path = base_path.with_name(
                f"{base_path.stem}-v{version}{base_path.suffix}")
            version += 1

        prefix = f"{self.shortlist.path.stem}.edit-directions-r"
        search_roots = [self.project_layout["Recipes"]]
        if self.shortlist.path.parent != self.project_layout["Recipes"]:
            search_roots.append(self.shortlist.path.parent)
        paths = list(dict.fromkeys(
            path
            for root in search_roots
            for path in root.glob(f"{prefix}*.json")
            if not path.name.endswith(".checkpoint.json")
        ))
        paths.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        valid: list[tuple[Path, dict]] = []
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(value, dict)
                and value.get("format") == "opencull-edit-directions-v1"
                and value.get("shortlist_sha256") == shortlist_sha
            ):
                valid.append((path, value))

        exact = [
            (path, value) for path, value in valid
            if value.get("review_revision") == revision
        ]
        selected = {
            photo for photo, entry in review_state.get("entries", {}).items()
            if isinstance(entry, dict) and entry.get("interesting") is True
        }
        active_style_sha = ""
        active_style = self.project.get("active_style_profile")
        if active_style:
            try:
                active_style_sha = hashlib.sha256(
                    Path(str(active_style)).expanduser().resolve().read_bytes()
                ).hexdigest()
            except OSError:
                active_style_sha = ""
        ordered = exact + [item for item in valid if item not in exact]
        if not ordered:
            return {
                "available": False,
                "default_path": str(next_path),
                "processed_photos": [],
                "processed_count": 0,
                "regeneration_required": False,
            }

        path, value = ordered[0]
        # A targeted regeneration report contains one photograph. Overlay the
        # newest exact-revision entry for each photo onto older complete reports
        # so regenerating one rejection never hides the rest of the batch.
        entries_by_photo: dict[str, dict] = {}
        personal_style_photos: set[str] = set()
        personal_fields = (
            "personal_title", "personal_intent", "personal_instructions",
            "personal_recipe",
        )
        for _, report in ordered:
            report_style_sha = str(report.get("style_profile_sha256", ""))
            for entry in report.get("entries", []):
                photo = entry.get("photo") if isinstance(entry, dict) else None
                if (
                    isinstance(photo, str)
                    and photo not in entries_by_photo
                    and (not selected or photo in selected)
                ):
                    entries_by_photo[photo] = entry
                    has_personal = all(
                        isinstance(entry.get(field), str)
                        and bool(entry[field].strip())
                        for field in personal_fields
                    )
                    # Reports predating style provenance remain usable when
                    # they visibly contain a complete personal treatment.
                    style_matches = (
                        not active_style_sha
                        or report_style_sha == active_style_sha
                        or not report_style_sha
                    )
                    if has_personal and style_matches:
                        personal_style_photos.add(photo)
        available_entries = [
            entries_by_photo[photo] for photo in sorted(entries_by_photo)
        ]
        available_photos = {entry.get("photo") for entry in available_entries}
        complete_photos = available_photos
        if active_style:
            complete_photos = available_photos & personal_style_photos
        processed_photos = sorted(
            photo for photo in (selected & complete_photos)
            if isinstance(photo, str)
        )
        directions = {
            **value,
            "review_revision": revision,
            "entries": available_entries,
            "assembled_from": [str(candidate) for candidate, _ in ordered],
        }
        encoded = json.dumps(
            directions, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")
        effective_path = self.project_layout["Recipes"] / (
            f"{self.shortlist.path.stem}.effective-edit-directions-"
            f"{hashlib.sha256(encoded).hexdigest()[:16]}.json")
        if not effective_path.is_file():
            _atomic_json_file(effective_path, directions)
        missing = sorted(selected - complete_photos)
        return {
            "available": True,
            "path": str(effective_path),
            "directions": directions,
            "default_path": str(next_path),
            "partial": not exact or bool(missing),
            "missing_photos": missing,
            "processed_photos": processed_photos,
            "processed_count": len(processed_photos),
            "regeneration_required": bool(processed_photos),
            "selection_revision": revision,
        }

    def server_close(self) -> None:
        previews = getattr(self, "previews", None)
        if previews is not None:
            previews.shutdown()
        jobs = getattr(self, "jobs", None)
        if jobs is not None and self.shutdown_jobs:
            jobs.shutdown()
        faces = getattr(self, "faces", None)
        if faces is not None:
            faces.shutdown()
        super().server_close()


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, format: str, *args: object) -> None:
        print(f"[OpenCull GUI] {self.address_string()} - {format % args}")

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, value: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str | None = None) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
            return
        mime = content_type or mimetypes.guess_type(path.name)[0]
        self._headers(HTTPStatus.OK, mime or "application/octet-stream", len(body))
        self.wfile.write(body)

    def _immutable_preview_file(self, path: Path, revision: str) -> None:
        """Serve only a completed JPEG artifact through the preview file route."""
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "preview file not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("ETag", f'"{revision}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _image_preview(self, path: Path, maximum: int) -> None:
        """Serve a screen-bounded local preview without altering its source."""
        try:
            stat = path.stat()
            identity = hashlib.sha256(
                f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{maximum}".encode()
            ).hexdigest()[:20]
            cache_dir = self.server.project_layout["Previews"] / "Develop"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / f"{path.stem}-{maximum}-{identity}.jpg"
            if cached.is_file():
                self._file(cached, "image/jpeg")
                return
        except (OSError, KeyError):
            cached = None
        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, "JPEG", quality=91, optimize=True)
                body = output.getvalue()
        except (OSError, ValueError):
            self._json({"error": "development preview could not be decoded"},
                       HTTPStatus.NOT_FOUND)
            return
        if cached is not None:
            try:
                temporary = cached.with_name(f".{cached.name}.{secrets.token_hex(4)}.tmp")
                temporary.write_bytes(body)
                os.replace(temporary, cached)
            except OSError:
                temporary.unlink(missing_ok=True)
        self._headers(HTTPStatus.OK, "image/jpeg", len(body))
        self.wfile.write(body)

    def _download(
        self, body: bytes, content_type: str, filename: str
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "").rsplit(":", 1)[0].lower()
        return host in {"127.0.0.1", "localhost"}

    def _require_host(self) -> bool:
        if self._valid_host():
            return True
        self._json({"error": "invalid Host header"}, HTTPStatus.FORBIDDEN)
        return False

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ReviewError("Invalid Content-Length.") from exc
        if length <= 0 or length > 65536:
            raise ReviewError("Request body must be between 1 byte and 64 KiB.")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ReviewError("Request body is not valid JSON.") from exc
        if not isinstance(value, dict):
            raise ReviewError("Request body must be a JSON object.")
        return value

    def _require_csrf(self) -> bool:
        supplied = self.headers.get("X-OpenCull-CSRF", "")
        if secrets.compare_digest(supplied, self.server.csrf_token):
            return True
        self._json({"error": "invalid review token"}, HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:
        if not self._require_host():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json({
                "ok": True,
                "mode": "immutable evidence with separate human review",
            })
            return
        if parsed.path == "/api/report":
            self._json({
                **self.server.payload,
                "review": self.server.reviews.public_state(),
                "csrf_token": self.server.csrf_token,
            })
            return
        if parsed.path == "/api/project":
            self._json(self.server.project_payload())
            return
        if parsed.path == "/api/project/migration":
            self._json(self.server.migration_payload())
            return
        if parsed.path == "/api/style-profile":
            self._json(self.server.style_profile_payload())
            return
        if parsed.path == "/api/style-photo":
            requested = parse_qs(parsed.query).get("path", [""])[0]
            path = Path(requested).expanduser().resolve()
            image_types = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
            if path.suffix.casefold() not in image_types or not path.is_file():
                self._json({"error": "style photograph is unavailable"}, HTTPStatus.NOT_FOUND)
            else:
                self._file(path)
            return
        if parsed.path == "/api/development":
            self._json(self.server.development_payload())
            return
        if parsed.path == "/api/development/preview":
            query = parse_qs(parsed.query)
            try:
                maximum = max(240, min(
                    int(query.get("max", ["960"])[0]), 1440))
                preview = self.server.development_recipe_preview(
                    query.get("photo", [""])[0],
                    query.get("style", [""])[0],
                    query.get("engine", ["darktable"])[0],
                    query.get("demosaic", ["markesteijn-3-pass"])[0],
                    maximum,
                )
            except (ValueError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._file(preview, "image/jpeg")
            return
        if parsed.path == "/api/development/image":
            query = parse_qs(parsed.query)
            requested = query.get("path", [""])[0]
            allowed = {str(Path(str(item.get("path", ""))).expanduser().resolve())
                       for item in self.server.project.get("artifacts", {}).get("renders", [])
                       if isinstance(item, dict)}
            allowed.update(
                str(Path(str(item.get("contact_sheet", ""))).expanduser().resolve())
                for item in self.server.project.get("artifacts", {}).get(
                    "renderer_comparisons", [])
                if isinstance(item, dict) and item.get("contact_sheet"))
            path = str(Path(requested).expanduser().resolve())
            if path not in allowed or not Path(path).is_file():
                self._json({"error": "development image is not linked to this project"}, HTTPStatus.NOT_FOUND)
            else:
                try:
                    maximum = int(query.get("max", ["0"])[0])
                except ValueError:
                    maximum = 0
                if maximum:
                    self._image_preview(Path(path), max(160, min(maximum, 2560)))
                else:
                    self._file(Path(path), "image/jpeg")
            return
        if parsed.path == "/api/verification":
            self._json(self.server.verification_payload())
            return
        if parsed.path == "/api/export-project":
            self._json(self.server.export_payload())
            return
        if parsed.path == "/api/review":
            self._json(self.server.reviews.public_state())
            return
        if parsed.path == "/api/shortlist":
            self._json(self.server.shortlist_payload())
            return
        if parsed.path == "/api/raw-source":
            self._json(self.server.raw_sources.public())
            return
        if parsed.path == "/api/edit-directions":
            self._json(self.server.edit_directions_payload())
            return
        if parsed.path == "/api/shortlist/export":
            if self.server.shortlist_reviews is None:
                self._json(
                    {"error": "professional shortlist is not loaded"},
                    HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.shortlist_reviews.export())
            return
        if parsed.path == "/api/action/recovery":
            self._json({
                "journals": self.server.actions.recoverable_journals()})
            return
        if parsed.path == "/api/action/cleanup-candidates":
            try:
                self._json(self.server.actions.cleanup_candidates())
            except ActionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/jobs":
            if self.server.jobs is None:
                self._json({"error": "job manager is disabled"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.jobs.public())
            return
        if parsed.path == "/api/jobs/style-profile-result":
            if self.server.jobs is None:
                self._json({"error": "job manager is disabled"}, HTTPStatus.NOT_FOUND)
            else:
                try:
                    self._json(self.server.jobs.style_profile_result(
                        parse_qs(parsed.query).get("job_id", [""])[0]))
                except JobError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/providers":
            if self.server.providers is None:
                self._json(
                    {"error": "provider profiles are disabled"},
                    HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.providers.public())
            return
        if parsed.path == "/api/people":
            if self.server.faces is None:
                self._json(
                    {"error": "private face indexing is disabled"},
                    HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.faces.public())
            return
        if parsed.path == "/api/person-faces":
            if self.server.faces is None:
                self._json(
                    {"error": "private face indexing is disabled"},
                    HTTPStatus.NOT_FOUND)
                return
            query = parse_qs(parsed.query)
            try:
                payload = self.server.faces.person_faces(
                    query.get("id", [""])[0],
                    offset=int(query.get("offset", ["0"])[0]),
                    limit=int(query.get("limit", ["100"])[0]),
                )
            except (FaceError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(payload)
            return
        if parsed.path == "/api/face":
            if self.server.faces is None:
                self._json(
                    {"error": "private face indexing is disabled"},
                    HTTPStatus.NOT_FOUND)
                return
            query = parse_qs(parsed.query)
            try:
                crop = self.server.faces.face_crop(
                    query.get("id", [""])[0])
            except FaceError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            output = io.BytesIO()
            crop.save(output, "JPEG", quality=90)
            body = output.getvalue()
            self._headers(HTTPStatus.OK, "image/jpeg", len(body))
            self.wfile.write(body)
            return
        if parsed.path == "/api/export":
            self._json(self.server.reviews.export())
            return
        if parsed.path == "/api/export/file":
            query = parse_qs(parsed.query)
            try:
                body, content_type, filename = export_bytes(
                    self.server.reviews,
                    query.get("policy", ["human_only"])[0],
                    query.get("format", ["json"])[0],
                )
            except ActionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._download(body, content_type, filename)
            return
        if parsed.path == "/api/export/xmp":
            query = parse_qs(parsed.query)
            try:
                body, filename = xmp_zip(
                    self.server.reviews,
                    query.get("policy", ["effective"])[0],
                )
            except ActionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._download(body, "application/zip", filename)
            return
        if parsed.path == "/api/action/status":
            query = parse_qs(parsed.query)
            try:
                self._json(self.server.actions.status(
                    query.get("id", [""])[0]))
            except ActionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/previews/status":
            query = parse_qs(parsed.query)
            names = query.get("name", [])
            size = query.get("size", ["thumb"])[0]
            try:
                self._json(self.server.previews.status(names, size))
            except PhotoError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/previews/file":
            query = parse_qs(parsed.query)
            name = query.get("name", [""])[0]
            size = query.get("size", ["thumb"])[0]
            revision = query.get("revision", [""])[0]
            try:
                expected = self.server.photos.preview_revision(name, size)
                preview = self.server.photos.cached_preview(name, size)
            except PhotoError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if not revision or revision != expected or preview is None:
                self._json(
                    {"error": "preview artifact is not ready"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._immutable_preview_file(preview, expected)
            return
        if parsed.path == "/api/cache":
            self._json(self.server.previews.progress())
            return
        if parsed.path == "/api/image":
            query = parse_qs(parsed.query)
            name = query.get("name", [""])[0]
            size = query.get("size", ["thumb"])[0]
            try:
                preview = self.server.photos.cached_preview(name, size)
            except PhotoError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if preview is None:
                state = self.server.previews.request(name, size)
                self._json(state, HTTPStatus.ACCEPTED)
                return
            self._file(preview, "image/jpeg")
            return
        static_name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        if static_name not in {"index.html", "app.js", "styles.css"}:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._file(STATIC_ROOT / static_name)

    def do_POST(self) -> None:
        if not self._require_host() or not self._require_csrf():
            return
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/project":
                changes = {key: body[key] for key in
                           ("name", "source_folder", "active_style_profile", "stage", "artifacts", "rendering")
                           if key in body}
                self._json(update_project(self.server.project_path, **changes))
                self.server.project = self.server.project_payload()
                return
            if parsed.path == "/api/project/migrate":
                self._json(self.server.migrate_project(
                    str(body.get("confirmation", ""))))
                return
            if parsed.path == "/api/export-project":
                self._json(self.server.export_render(str(body.get("source", "")),
                                                      str(body.get("destination", ""))))
                return
            if parsed.path == "/api/export/reveal":
                requested = Path(str(body.get("path", ""))).expanduser().resolve()
                exports = {
                    str(Path(str(item.get("destination", ""))).expanduser().resolve())
                    for item in self.server.project.get("artifacts", {}).get("exports", [])
                    if isinstance(item, dict)
                }
                if str(requested) not in exports or not requested.is_file():
                    raise ValueError("exported file is not linked to this project")
                if sys.platform != "darwin":
                    raise ValueError("Finder reveal is available only on macOS")
                subprocess.Popen(["open", "-R", str(requested)])
                self._json({"path": str(requested)})
                return
            if parsed.path == "/api/export/pick-folder":
                if sys.platform != "darwin":
                    raise JobError("native destination selection is available only on macOS")
                result = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose export folder")'],
                                        capture_output=True, text=True, timeout=300, check=False)
                if result.returncode != 0:
                    raise JobError("destination selection was cancelled")
                self._json({"path": result.stdout.strip()})
                return
            if parsed.path == "/api/development/import-recipe":
                self._json(self.server.import_development_recipe())
                return
            if parsed.path == "/api/development/render-portable":
                self._json(self.server.render_portable_development_recipe(
                    str(body.get("photo", "")), str(body.get("style", "")),
                    str(body.get("engine", "darktable")),
                    str(body.get("demosaic", "markesteijn-3-pass")),
                ))
                return
            if parsed.path == "/api/review/cluster":
                state = self.server.reviews.update_cluster(
                    str(body.get("cluster_id", "")),
                    body.get("keepers"),
                    body.get("note"),
                    body.get("reviewed"),
                    body.get("revision"),
                    body.get("photo_annotations"),
                    str(body.get("action", "review")),
                )
            elif parsed.path == "/api/review/undo":
                state = self.server.reviews.undo(body.get("revision"))
            elif parsed.path == "/api/review/position":
                state = self.server.reviews.update_position(
                    str(body.get("cluster_id", "")),
                    body.get("revision"),
                )
            elif parsed.path == "/api/shortlist/load":
                result = self.server.load_shortlist(
                    Path(str(body.get("path", ""))))
                self._json(result)
                return
            elif parsed.path == "/api/shortlist/review":
                if self.server.shortlist_reviews is None:
                    raise ShortlistReviewError(
                        "professional shortlist is not loaded")
                state = self.server.shortlist_reviews.update(
                    str(body.get("photo", "")),
                    body.get("tier"),
                    body.get("edit_raw"),
                    body.get("note"),
                    body.get("reviewed"),
                    body.get("revision"),
                    interesting=body.get("interesting", False),
                )
            elif parsed.path == "/api/shortlist/undo":
                if self.server.shortlist_reviews is None:
                    raise ShortlistReviewError(
                        "professional shortlist is not loaded")
                state = self.server.shortlist_reviews.undo(
                    body.get("revision"))
            elif parsed.path == "/api/previews/focus":
                visible = body.get("visible", [])
                prefetch = body.get("prefetch", [])
                if (
                    not isinstance(visible, list)
                    or not isinstance(prefetch, list)
                    or len(visible) > 128
                    or len(prefetch) > 256
                    or any(not isinstance(name, str)
                           for name in visible + prefetch)
                ):
                    raise ReviewError("Invalid preview focus request.")
                result = self.server.previews.focus(
                    visible, prefetch, str(body.get("size", "thumb")))
                self._json(result)
                return
            elif parsed.path == "/api/previews/retry":
                result = self.server.previews.retry(
                    str(body.get("name", "")),
                    str(body.get("size", "thumb")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/cache/clear":
                result = self.server.previews.clear_cache()
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add(
                    str(body.get("photos", "")),
                    str(body.get("output", "")),
                    body.get("keep_per_group", 2),
                    body.get("recursive", False),
                    str(body.get("profile", "family")),
                    str(body.get("provider_profile_id", "")),
                    body.get("judge_panel"),
                    body.get("judge_votes", 5),
                    body.get("judge_required", 4),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/action":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.action(
                    str(body.get("job_id", "")),
                    str(body.get("action", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-professional":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_professional(
                    str(body.get("report", "")),
                    str(body.get("photos", "")),
                    str(body.get("output", "")),
                    str(body.get("review", "")),
                    str(body.get("policy", "effective")),
                    str(body.get("profile", "family")),
                    str(body.get("provider_profile_id", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-edit-suggestions":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_edit_suggestions(
                    shortlist=str(body.get("shortlist", "")),
                    review=str(body.get("review", "")),
                    photos=str(body.get("photos", "")),
                    output=str(body.get("output", "")),
                    profile=str(body.get("profile", "family")),
                    provider_profile_id=str(
                        body.get("provider_profile_id", "")),
                    model=str(body.get("model", "")),
                    consensus=bool(body.get("consensus", False)),
                    style_profile=str(body.get("style_profile", "")),
                    only_photo=str(body.get("only_photo", "")),
                    only_photos=(
                        body.get("only_photos")
                        if isinstance(body.get("only_photos"), list)
                        else []
                    ),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-semantic-verification":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_semantic_verification(
                    str(body.get("original", "")),
                    str(body.get("developed", "")),
                    str(body.get("suggestion", "")),
                    str(body.get("thumbnail", "")),
                    str(body.get("output", "")),
                    str(body.get("provider_profile_id", "")),
                    str(body.get("model", "")),
                    bool(body.get("consensus", False)),
                    str(self.server.project_path),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-style-profile":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_style_profile(
                    body.get("photos", ""), str(body.get("output", "")),
                    str(body.get("existing", "")), str(body.get("mode", "update")),
                    str(body.get("provider_profile_id", "")),
                    str(body.get("model", "")), int(body.get("limit", 64)),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-development-render":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_development_render(
                    str(body.get("baseline", "")), str(body.get("recipe", "")),
                    str(body.get("output_dir", "")), str(body.get("project", "")),
                    str(body.get("reference_jpeg", "")), str(body.get("adjustments", "")),
                    bool(body.get("allow_incomplete", False)))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-development-pipeline":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_development_pipeline(
                    str(body.get("source", "")),
                    str(body.get("reference", "")),
                    str(body.get("directions", "")),
                    str(body.get("photo", "")),
                    str(body.get("style", "")),
                    str(body.get("project", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-renderer-comparison":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_renderer_comparison(
                    str(body.get("source", "")), str(body.get("reference", "")),
                    str(body.get("directions", "")), str(body.get("photo", "")),
                    str(body.get("style", "")), str(body.get("opencull_render", "")),
                    str(body.get("project", "")),
                    str(body.get("demosaic", "markesteijn-1-pass")))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-renderer-export":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_renderer_export(
                    str(body.get("source", "")), str(body.get("reference", "")),
                    str(body.get("directions", "")), str(body.get("photo", "")),
                    str(body.get("style", "")), str(body.get("project", "")),
                    str(body.get("demosaic", "markesteijn-1-pass")))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-delivery-export":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_delivery_export(
                    str(body.get("source", "")), str(body.get("reference", "")),
                    str(body.get("directions", "")), str(body.get("photo", "")),
                    str(body.get("style", "")), str(body.get("engine", "")),
                    str(body.get("project", "")), str(body.get("destination", "")),
                    str(body.get("demosaic", "markesteijn-1-pass")),
                    str(body.get("render", "")))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/open-review":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.open_review(
                    str(body.get("job_id", "")))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/pick-folder":
                if sys.platform != "darwin":
                    raise JobError("native folder selection is available only on macOS")
                result = subprocess.run(
                    [
                        "osascript", "-e",
                        'POSIX path of (choose folder with prompt '
                        '"Choose a folder of photographs to cull")',
                    ],
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if result.returncode != 0:
                    if "-128" in result.stderr:
                        raise JobError("folder selection was cancelled")
                    raise JobError(
                        f"folder chooser failed: {result.stderr.strip()}")
                self._json({"photos": result.stdout.strip().rstrip("/")})
                return
            elif parsed.path == "/api/jobs/pick-style-photos":
                if sys.platform != "darwin":
                    raise JobError("native file selection is available only on macOS")
                script = (
                    'set picked to choose file with prompt "Add finished photographs" '
                    'of type {"public.image"} with multiple selections allowed\n'
                    'set output to ""\n'
                    'repeat with itemRef in picked\n'
                    'set output to output & (POSIX path of itemRef) & linefeed\n'
                    'end repeat\n'
                    'return output'
                )
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if result.returncode != 0:
                    if "-128" in result.stderr:
                        raise JobError("file selection was cancelled")
                    raise JobError(
                        f"file chooser failed: {result.stderr.strip()}")
                photos = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                self._json({"photos": photos})
                return
            elif parsed.path == "/api/jobs/pick-style-profile":
                if sys.platform != "darwin":
                    raise JobError("native file selection is available only on macOS")
                result = subprocess.run(
                    [
                        "osascript", "-e",
                        'POSIX path of (choose file with prompt '
                        '"Choose an existing personal style profile" of type '
                        '{"public.json"})',
                    ],
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if result.returncode != 0:
                    if "-128" in result.stderr:
                        raise JobError("file selection was cancelled")
                    raise JobError(
                        f"file chooser failed: {result.stderr.strip()}")
                self._json({"existing": result.stdout.strip()})
                return
            elif parsed.path == "/api/jobs/pick-style-output":
                if sys.platform != "darwin":
                    raise JobError("native file selection is available only on macOS")
                result = subprocess.run(
                    [
                        "osascript", "-e",
                        'POSIX path of (choose file name with prompt '
                        '"Save the personal style profile" default name '
                        '"personal-style-profile-v2.json")',
                    ],
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if result.returncode != 0:
                    if "-128" in result.stderr:
                        raise JobError("file selection was cancelled")
                    raise JobError(
                        f"save chooser failed: {result.stderr.strip()}")
                output = result.stdout.strip()
                if Path(output).suffix.casefold() != ".json":
                    output += ".json"
                self._json({"output": output})
                return
            elif parsed.path == "/api/raw-source/pick":
                if sys.platform != "darwin":
                    raise RawSourceError(
                        "native folder selection is available only on macOS")
                result = subprocess.run(
                    [
                        "osascript", "-e",
                        'POSIX path of (choose folder with prompt '
                        '"Choose the folder containing RAW originals")',
                    ],
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if result.returncode != 0:
                    if "-128" in result.stderr:
                        raise RawSourceError("folder selection was cancelled")
                    raise RawSourceError(
                        f"folder chooser failed: {result.stderr.strip()}")
                state = self.server.raw_sources.configure(
                    Path(result.stdout.strip().rstrip("/")))
                self.server.payload["raw_source"] = state
                self.server.project = register_file_artifact(
                    self.server.project_path, "raw_source_map",
                    self.server.raw_sources.path, stage="shortlist")
                self._json(state)
                return
            elif parsed.path == "/api/raw-source/configure":
                state = self.server.raw_sources.configure(
                    Path(str(body.get("path", ""))))
                self.server.payload["raw_source"] = state
                self.server.project = register_file_artifact(
                    self.server.project_path, "raw_source_map",
                    self.server.raw_sources.path, stage="shortlist")
                self._json(state)
                return
            elif parsed.path == "/api/action/pick-destination":
                if sys.platform != "darwin":
                    raise ActionError(
                        "native folder selection is available only on macOS")
                result = subprocess.run(
                    [
                        "osascript", "-e",
                        'POSIX path of (choose folder with prompt '
                        '"Choose a destination for selected photographs")',
                    ],
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if result.returncode != 0:
                    if "-128" in result.stderr:
                        raise ActionError("folder selection was cancelled")
                    raise ActionError(
                        f"folder chooser failed: {result.stderr.strip()}")
                self._json({
                    "destination": result.stdout.strip().rstrip("/")})
                return
            elif parsed.path == "/api/providers/save":
                if self.server.providers is None:
                    raise ProviderError("provider profiles are disabled")
                result = self.server.providers.save(
                    body.get("profile", {}),
                    body.get("revision"),
                    str(body.get("secret", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/providers/delete":
                if self.server.providers is None:
                    raise ProviderError("provider profiles are disabled")
                result = self.server.providers.delete(
                    str(body.get("profile_id", "")),
                    body.get("revision"),
                    bool(body.get("remove_credential", False)),
                )
                self._json(result)
                return
            elif parsed.path == "/api/providers/test":
                if self.server.providers is None:
                    raise ProviderError("provider profiles are disabled")
                result = self.server.providers.test_connection(
                    str(body.get("profile_id", "")))
                self._json(result)
                return
            elif parsed.path == "/api/people/start":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.start())
                return
            elif parsed.path == "/api/people/cancel":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.cancel())
                return
            elif parsed.path == "/api/people/rename":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.rename(
                    str(body.get("person_id", "")),
                    str(body.get("name", "")),
                    bool(body.get("confirmed", False)),
                ))
                return
            elif parsed.path == "/api/people/merge":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.merge(body.get("person_ids")))
                return
            elif parsed.path == "/api/people/split":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.split(
                    str(body.get("person_id", "")),
                    body.get("face_ids"),
                ))
                return
            elif parsed.path == "/api/people/forget":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                person_id = str(body.get("person_id", ""))
                if body.get("confirmation") != f"FORGET {person_id}":
                    raise FaceError(
                        f"confirmation must exactly equal: FORGET {person_id}")
                self._json(self.server.faces.forget(person_id))
                return
            elif parsed.path == "/api/people/delete-all":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                if body.get("confirmation") != "DELETE ALL PRIVATE FACE DATA":
                    raise FaceError(
                        "confirmation must exactly equal: "
                        "DELETE ALL PRIVATE FACE DATA")
                self._json(self.server.faces.delete_all())
                return
            elif parsed.path == "/api/action/preflight":
                result = self.server.actions.preflight(**body)
                self._json(result)
                return
            elif parsed.path == "/api/action/execute":
                result = self.server.actions.execute(
                    str(body.get("plan_id", "")),
                    str(body.get("confirmation", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/action/cancel":
                result = self.server.actions.cancel(
                    str(body.get("operation_id", "")))
                self._json(result)
                return
            elif parsed.path == "/api/action/rollback":
                result = self.server.actions.rollback(
                    str(body.get("operation_id", "")),
                    str(body.get("confirmation", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/action/resume":
                result = self.server.actions.resume_journal(
                    Path(str(body.get("journal_path", ""))),
                    str(body.get("confirmation", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/action/contact-sheet":
                result = self.server.actions.contact_sheet(
                    Path(str(body.get("destination", ""))),
                    str(body.get("policy", "human_only")),
                    str(body.get("confirmation", "")),
                    int(body.get("columns", 4)),
                    int(body.get("rows", 5)),
                )
                self._json(result)
                return
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            if parsed.path.startswith("/api/review/") and self.server.reviews.path.is_file():
                self.server.project = register_file_artifact(
                    self.server.project_path, "culling_review",
                    self.server.reviews.path, stage="cull")
            elif (
                parsed.path.startswith("/api/shortlist/")
                and self.server.shortlist_reviews is not None
                and self.server.shortlist_reviews.path.is_file()
            ):
                self.server.project = register_file_artifact(
                    self.server.project_path, "shortlist_review",
                    self.server.shortlist_reviews.path, stage="shortlist")
            elif (
                parsed.path.startswith("/api/raw-source/")
                and self.server.raw_sources.path.is_file()
            ):
                self.server.project = register_file_artifact(
                    self.server.project_path, "raw_source_map",
                    self.server.raw_sources.path, stage="shortlist")
            self._json(state)
        except (
            ReviewError, PhotoError, ActionError, JobError,
            ProviderError,
            FaceError,
            ShortlistError,
            ShortlistReviewError,
            RawSourceError,
            TypeError, ValueError,
        ) as exc:
            status = (
                HTTPStatus.CONFLICT
                if "reload" in str(exc).lower()
                or "different version" in str(exc).lower()
                else HTTPStatus.BAD_REQUEST
            )
            self._json({"error": str(exc)}, status)
