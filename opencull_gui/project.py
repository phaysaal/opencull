"""Versioned, folder-local project context shared by Darkimiya's UX stages."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .branding import (
    CULLING_ENGINE_NAME,
    LEGACY_PROJECT_DIRECTORY_NAMES,
    PRODUCT_NAME,
    PROJECT_DIRECTORY_NAME,
    PROJECT_MANIFEST_NAME,
)

FORMAT = "darkimiya-project-v1"
LEGACY_FORMATS = {"opencull-project-v1"}
MIGRATION_FORMAT = "darkimiya-project-migration-v1"
PROJECT_SUBDIRECTORIES = (
    "Reports", "Reviews", "Recipes", "Developments", "Verification",
    "Exports", "Operations", "Previews", "RAW Reserve", "Rejected",
)
MANAGED_PROJECT_DIRECTORY_NAMES = {
    PROJECT_DIRECTORY_NAME.casefold(),
    *(name.casefold() for name in LEGACY_PROJECT_DIRECTORY_NAMES),
}
ARTIFACT_KEYS = (
    "culling_report", "culling_review", "shortlist", "shortlist_review", "style_profile",
    "raw_source_map", "calibration", "edit_directions", "recipes", "renders", "renderer_comparisons",
    "verifications", "adjustments", "exports", "operations", "rejected",
    "raw_reserve",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def project_directory(source_folder: Path) -> Path:
    """Return the managed directory for a one-folder Darkimiya project.

    A new project gets the hidden directory. A folder that already
    carries a directory from an earlier release keeps using it, so
    nothing existing is orphaned by the rename.
    """
    root = source_folder.expanduser().resolve()
    for name in (PROJECT_DIRECTORY_NAME, *LEGACY_PROJECT_DIRECTORY_NAMES):
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return root / PROJECT_DIRECTORY_NAME


def project_manifest_path(source_folder: Path) -> Path:
    return project_directory(source_folder) / PROJECT_MANIFEST_NAME


def is_managed_project_path(root: Path, candidate: Path) -> bool:
    """Whether a candidate is inside Darkimiya-owned project storage."""
    try:
        relative = candidate.resolve().relative_to(root.expanduser().resolve())
    except (OSError, ValueError):
        return False
    return bool(
        relative.parts
        and relative.parts[0].casefold() in MANAGED_PROJECT_DIRECTORY_NAMES)


def ensure_project_layout(source_folder: Path) -> dict[str, Path]:
    """Create the explicit, inspectable storage areas owned by a project."""
    root = project_directory(source_folder)
    root.mkdir(parents=True, exist_ok=True)
    paths = {"root": root, "manifest": root / PROJECT_MANIFEST_NAME}
    for name in PROJECT_SUBDIRECTORIES:
        path = root / name
        path.mkdir(exist_ok=True)
        paths[name] = path
    return paths


def preferred_project_path(
    source_folder: Path, legacy_path: Path | None = None,
) -> Path:
    """Prefer folder-local state, falling back to an existing legacy manifest."""
    current = project_manifest_path(source_folder)
    if current.is_file():
        return current
    if legacy_path is not None:
        legacy = legacy_path.expanduser().resolve()
        if legacy.is_file():
            return legacy
    return current


def create_project(path: Path, name: str, source_folder: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    source = source_folder.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"project source folder is unavailable: {source}")
    if path == project_manifest_path(source):
        ensure_project_layout(source)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    artifacts = {key: [] for key in ARTIFACT_KEYS}
    artifacts["culling_report"] = None
    value = {
        "format": FORMAT, "id": uuid.uuid4().hex,
        "product": PRODUCT_NAME, "culling_engine": CULLING_ENGINE_NAME,
        "name": str(name).strip() or source.name or "Darkimiya project",
        "created_at": _now(), "updated_at": _now(),
        "source_folder": str(source),
        "managed_directory": str(project_directory(source)),
        "active_style_profile": None,
        "about": "",
        "rendering": {
            "engine": "darktable",
            "demosaic": "markesteijn-3-pass",
            "spectrum": "visible",
            "cutoff_nm": 0.0,
        },
        "stage": "import", "artifacts": artifacts, "history": [],
    }
    _atomic_write(path, value)
    return value


def _relocated(value: Any, old: str, new: str) -> Any:
    """Rewrite every stored path that lived under the folder's old home."""
    if isinstance(value, str):
        if value == old:
            return new
        if value.startswith(old.rstrip("/") + "/"):
            return new + value[len(old):]
        return value
    if isinstance(value, list):
        return [_relocated(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _relocated(item, old, new) for key, item in value.items()}
    return value


def load_project(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read project manifest {path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("format") not in {FORMAT, *LEGACY_FORMATS}
    ):
        raise ValueError("unsupported Darkimiya project manifest")
    if not isinstance(value.get("artifacts"), dict):
        raise ValueError("project artifacts are invalid")
    # The manifest lives inside the folder it describes, so where it was
    # found IS the folder's current home. If the folder has moved since
    # the manifest was written, every recorded path still points at the
    # old home; re-anchor them all to the new one and persist, and the
    # project is portable by being opened.
    if path.parent.name.casefold() in MANAGED_PROJECT_DIRECTORY_NAMES:
        actual = str(path.parent.parent)
        recorded = str(value.get("source_folder") or "")
        if recorded and recorded != actual:
            value = _relocated(value, recorded, actual)
            value["source_folder"] = actual
            _atomic_write(path, value)
    return value


def load_or_create(path: Path, name: str, source_folder: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    return load_project(path) if path.is_file() else create_project(path, name, source_folder)


def load_or_create_folder_project(
    source_folder: Path, name: str = "", legacy_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Open the preferred project manifest without silently migrating legacy data."""
    path = preferred_project_path(source_folder, legacy_path)
    return path, load_or_create(path, name, source_folder)


def update_project(path: Path, **changes: Any) -> dict[str, Any]:
    value = load_project(path)
    if "about" in changes and changes["about"] is not None:
        # What the photographer says this shoot is. Models cannot infer a
        # subject they have never been told about: a solar eclipse read
        # as a crescent moon is not a failure of looking, it is a
        # question nobody answered.
        value["about"] = " ".join(str(changes["about"]).split())[:600]
    for key in ("name", "source_folder", "active_style_profile", "stage",
                "last_phase"):
        if key in changes and changes[key] is not None:
            value[key] = str(changes[key]) if key != "active_style_profile" else changes[key]
    if "artifacts" in changes:
        if not isinstance(changes["artifacts"], dict):
            raise ValueError("project artifacts must be an object")
        value["artifacts"].update(changes["artifacts"])
    if "rendering" in changes:
        rendering = changes["rendering"]
        if not isinstance(rendering, dict):
            raise ValueError("project rendering preference must be an object")
        engine = str(rendering.get("engine", ""))
        demosaic = str(rendering.get("demosaic", ""))
        if engine not in {"default", "darktable"}:
            raise ValueError("unsupported project rendering engine")
        if demosaic not in {
            "markesteijn-1-pass", "markesteijn-3-pass",
            "markesteijn-3-pass-vng",
        }:
            raise ValueError("unsupported project demosaic preference")
        # What the camera was looking at. An infrared shoot is a property
        # of the whole album -- the filter was on the front of the lens
        # for all of it -- rather than something decided frame by frame.
        spectrum = str(rendering.get("spectrum", "visible")) or "visible"
        if spectrum not in {"visible", "infrared"}:
            raise ValueError("unsupported project spectrum")
        # Which filter was on the front. It separates one infrared shoot
        # from another -- 720nm still has colour to work with, 850nm has
        # none -- so it is worth recording rather than inferring.
        try:
            cutoff = float(rendering.get("cutoff_nm") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("filter cut-off must be a number") from exc
        if cutoff and not 300 <= cutoff <= 1400:
            raise ValueError(
                "a filter cut-off is stated in nanometres, between 300 "
                "and 1400")
        value["rendering"] = {
            "engine": engine, "demosaic": demosaic, "spectrum": spectrum,
            "cutoff_nm": cutoff if spectrum == "infrared" else 0.0}
    value["updated_at"] = _now()
    value.setdefault("history", []).append({"updated_at": value["updated_at"],
                                             "changes": sorted(changes)})
    _atomic_write(Path(path).expanduser().resolve(), value)
    return value


def project_sha256(path: Path) -> str:
    return hashlib.sha256(path.expanduser().resolve().read_bytes()).hexdigest()


def _legacy_artifact_paths(value: Any) -> list[Path]:
    """Collect explicit artifact paths without guessing what prose may mean."""
    paths: list[Path] = []
    if isinstance(value, list):
        for item in value:
            paths.extend(_legacy_artifact_paths(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "path", "provenance", "contact_sheet", "destination",
                "journal_path", "output",
            } and isinstance(item, str) and item.strip():
                paths.append(Path(item).expanduser())
            else:
                paths.extend(_legacy_artifact_paths(item))
    return paths


def legacy_migration_preview(
    legacy_path: Path, source_folder: Path,
) -> dict[str, Any]:
    """Describe an explicit legacy migration without changing either project."""
    legacy = legacy_path.expanduser().resolve()
    source = source_folder.expanduser().resolve()
    destination = project_manifest_path(source)
    if not source.is_dir():
        raise ValueError(f"project source folder is unavailable: {source}")
    value = load_project(legacy)
    if value.get("format") not in LEGACY_FORMATS:
        return {
            "available": False, "reason": "project already uses the current format",
            "legacy_path": str(legacy), "destination_path": str(destination),
        }
    declared_source = str(value.get("source_folder", "")).strip()
    if declared_source and Path(declared_source).expanduser().resolve() != source:
        raise ValueError("legacy project belongs to a different source folder")
    if destination.is_file():
        return {
            "available": False, "reason": "folder-local project already exists",
            "legacy_path": str(legacy), "destination_path": str(destination),
        }
    artifact_paths = _legacy_artifact_paths(value.get("artifacts", {}))
    missing = sorted({str(path) for path in artifact_paths if not path.exists()})
    legacy_hash = project_sha256(legacy)
    identity = hashlib.sha256(
        f"{legacy_hash}\0{source}\0{destination}".encode()
    ).hexdigest()
    return {
        "format": MIGRATION_FORMAT,
        "available": True,
        "migration_id": identity[:16],
        "confirmation_code": f"{int(identity[:12], 16) % 10000:04d}",
        "legacy_path": str(legacy),
        "legacy_format": value["format"],
        "legacy_sha256": legacy_hash,
        "destination_path": str(destination),
        "source_folder": str(source),
        "artifact_categories": len(value.get("artifacts", {})),
        "artifact_paths": len(artifact_paths),
        "missing_artifact_paths": missing,
        "original_manifest_preserved": True,
        "photographs_moved": 0,
    }


def migrate_legacy_project(
    legacy_path: Path, source_folder: Path, confirmation: str,
) -> dict[str, Any]:
    """Create a current folder-local project while preserving legacy evidence."""
    preview = legacy_migration_preview(legacy_path, source_folder)
    if not preview.get("available"):
        raise ValueError(str(preview.get("reason", "legacy migration is unavailable")))
    if str(confirmation).strip() != preview["confirmation_code"]:
        raise ValueError("migration confirmation code does not match")

    legacy = Path(preview["legacy_path"])
    source = Path(preview["source_folder"])
    destination = Path(preview["destination_path"])
    legacy_value = load_project(legacy)
    layout = ensure_project_layout(source)
    journal_path = layout["Operations"] / (
        f"project-migration-{preview['migration_id']}.json")
    started = _now()
    journal = {
        **preview,
        "status": "planned",
        "started_at": started,
        "completed_at": None,
        "journal_path": str(journal_path),
        "destination_sha256": None,
    }
    _atomic_write(journal_path, journal)

    artifacts = {key: [] for key in ARTIFACT_KEYS}
    artifacts["culling_report"] = None
    artifacts.update(deepcopy(legacy_value.get("artifacts", {})))
    migrated = deepcopy(legacy_value)
    migrated.update({
        "format": FORMAT,
        "id": str(legacy_value.get("id") or uuid.uuid4().hex),
        "product": PRODUCT_NAME,
        "culling_engine": CULLING_ENGINE_NAME,
        "name": str(legacy_value.get("name") or source.name or "Darkimiya project"),
        "created_at": str(legacy_value.get("created_at") or started),
        "updated_at": started,
        "source_folder": str(source),
        "managed_directory": str(project_directory(source)),
        "active_style_profile": legacy_value.get("active_style_profile"),
        "stage": str(legacy_value.get("stage") or "import"),
        "artifacts": artifacts,
        "migration": {
            "format": MIGRATION_FORMAT,
            "migration_id": preview["migration_id"],
            "legacy_path": str(legacy),
            "legacy_format": preview["legacy_format"],
            "legacy_sha256": preview["legacy_sha256"],
            "migrated_at": started,
            "original_manifest_preserved": True,
        },
    })
    history = list(legacy_value.get("history", []) or [])
    history.append({
        "updated_at": started,
        "changes": ["explicit_legacy_migration"],
        "legacy_sha256": preview["legacy_sha256"],
    })
    migrated["history"] = history
    _atomic_write(destination, migrated)
    loaded = load_project(destination)
    destination_hash = project_sha256(destination)
    if loaded.get("format") != FORMAT or project_sha256(legacy) != preview["legacy_sha256"]:
        raise ValueError("migration validation failed")
    journal.update({
        "status": "completed",
        "completed_at": _now(),
        "destination_sha256": destination_hash,
    })
    _atomic_write(journal_path, journal)
    return {
        "project": loaded,
        "path": str(destination),
        "sha256": destination_hash,
        "journal": journal,
    }


def _file_artifact(
    artifact_path: Path, kind: str, job_id: str = "",
) -> dict[str, Any]:
    resolved = artifact_path.expanduser().resolve()
    record: dict[str, Any] = {
        "kind": kind,
        "path": str(resolved),
        "created_at": _now(),
    }
    if job_id:
        record["job_id"] = job_id
    if resolved.is_file():
        record["sha256"] = hashlib.sha256(resolved.read_bytes()).hexdigest()
        record["size_bytes"] = resolved.stat().st_size
    return record


def register_file_artifact(
    path: Path, key: str, artifact_path: Path, *, stage: str,
    job_id: str = "", singleton: bool = False,
) -> dict[str, Any]:
    """Register a durable file by identity without duplicating project history."""
    if key not in ARTIFACT_KEYS:
        raise ValueError(f"unsupported project artifact: {key}")
    project_path = path.expanduser().resolve()
    value = load_project(project_path)
    record = _file_artifact(artifact_path, key, job_id)
    current = value.get("artifacts", {}).get(key)
    if singleton:
        if isinstance(current, dict) and (
            current.get("path") == record["path"]
            and current.get("sha256") == record.get("sha256")
        ):
            return value
        return update_project(
            project_path, stage=stage, artifacts={key: record})
    items = list(current or []) if isinstance(current, list) else []
    duplicate = any(
        isinstance(item, dict)
        and item.get("path") == record["path"]
        and item.get("sha256") == record.get("sha256")
        for item in items
    )
    if duplicate:
        return value
    items.append(record)
    return update_project(project_path, stage=stage, artifacts={key: items})


def register_job_output(path: Path, job: dict[str, Any]) -> dict[str, Any]:
    """Attach a successfully produced queue artifact to its owning project."""
    mapping = {
        "culling": ("culling_report", "cull", True),
        "professional_shortlist": ("shortlist", "shortlist", False),
        "edit_suggestions": ("edit_directions", "style", False),
        "semantic_verification": ("verifications", "verify", False),
        "style_profile": ("style_profile", "style", False),
    }
    selected = mapping.get(str(job.get("kind", "")))
    if selected is None:
        return load_project(path)
    key, stage, singleton = selected
    return register_file_artifact(
        path, key, Path(str(job["output"])), stage=stage,
        job_id=str(job.get("id", "")), singleton=singleton)


def register_render(path: Path, render: dict[str, Any]) -> dict[str, Any]:
    """Append a render artifact to its project manifest without overwriting history."""
    project_path = path.expanduser().resolve()
    value = load_project(project_path)
    output = render.get("output", {})
    artifact = {
        "variant": render.get("recipe", {}).get("style", "render"),
        "source_photo": render.get("recipe", {}).get("source_photo"),
        "path": output.get("path"),
        "provenance": str(Path(output.get("path", "")).with_suffix(".render.json")),
        "recipe_revision": render.get("recipe", {}).get("revision", 0),
        "calibration": render.get("calibration"),
        "sha256": output.get("sha256"),
        "created_at": render.get("created_at", _now()),
    }
    renders = list(value.get("artifacts", {}).get("renders", []) or [])
    duplicate = any(
        item.get("variant") == artifact["variant"]
        and item.get("source_photo") == artifact["source_photo"]
        and item.get("sha256") == artifact["sha256"]
        for item in renders if isinstance(item, dict)
    )
    if not duplicate:
        renders.append(artifact)
    return update_project(project_path, stage="develop", artifacts={"renders": renders})


def export_project_render(
    path: Path, source: Path, destination: Path, *, export_key: str,
    photo: str, style: str, engine: str,
) -> dict[str, Any]:
    """Atomically deliver a project-linked render and register its evidence."""
    project_path = path.expanduser().resolve()
    value = load_project(project_path)
    source_path = source.expanduser().resolve()
    allowed = {
        str(Path(str(item.get("path", ""))).expanduser().resolve())
        for item in value.get("artifacts", {}).get("renders", []) or []
        if isinstance(item, dict) and item.get("path")
    }
    if str(source_path) not in allowed or not source_path.is_file():
        raise ValueError("source render is not linked to this project")
    requested = destination.expanduser().resolve()
    delivered = requested
    revision = 2
    while delivered.exists():
        delivered = requested.with_name(
            f"{requested.stem}-{revision}{requested.suffix}")
        revision += 1
    delivered.parent.mkdir(parents=True, exist_ok=True)
    temporary = delivered.with_name(f".{delivered.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source_path, temporary)
        with temporary.open("rb") as handle:
            digest = hashlib.sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        os.replace(temporary, delivered)
    finally:
        temporary.unlink(missing_ok=True)
    record = {
        "format": "darkimiya-image-export-v1",
        "export_key": export_key,
        "photo": photo,
        "style": style,
        "engine": engine,
        "source": str(source_path),
        "destination": str(delivered),
        "requested_destination": str(requested),
        "created_at": _now(),
        "sha256": digest.hexdigest(),
        "size": delivered.stat().st_size,
    }
    exports = list(value.get("artifacts", {}).get("exports", []) or [])
    exports.append(record)
    update_project(project_path, stage="develop", artifacts={"exports": exports})
    return record


def register_renderer_comparison(
    path: Path, comparison: dict[str, Any],
) -> dict[str, Any]:
    """Link an immutable paired-render report and contact sheet to a project."""
    project_path = path.expanduser().resolve()
    value = load_project(project_path)
    source = comparison.get("source", {})
    recipe = comparison.get("recipe", {})
    visual = comparison.get("visual_comparison", {})
    artifact = {
        "source_photo": recipe.get("source_photo")
        or Path(str(source.get("path", ""))).name,
        "source_sha256": source.get("sha256"),
        "style": recipe.get("style"),
        "recipe_sha256": recipe.get("sha256"),
        "report": comparison.get("report_path"),
        "contact_sheet": visual.get("path"),
        "contact_sheet_sha256": visual.get("sha256"),
        "technical_comparison": comparison.get("technical_comparison", {}),
        "research_notice": comparison.get("research_notice"),
        "demosaic": comparison.get("demosaic", {}),
        "created_at": comparison.get("created_at", _now()),
    }
    artifacts = list(
        value.get("artifacts", {}).get("renderer_comparisons", []) or [])
    duplicate = any(
        item.get("source_sha256") == artifact["source_sha256"]
        and item.get("recipe_sha256") == artifact["recipe_sha256"]
        and item.get("contact_sheet_sha256") == artifact["contact_sheet_sha256"]
        for item in artifacts if isinstance(item, dict))
    if not duplicate:
        artifacts.append(artifact)
    return update_project(
        project_path, stage="develop",
        artifacts={"renderer_comparisons": artifacts})


def import_legacy_development_artifacts(
    project_path: Path, legacy_path: Path,
) -> dict[str, Any]:
    """Link intact legacy renders to a folder project without moving files."""
    current_path = project_path.expanduser().resolve()
    previous_path = legacy_path.expanduser().resolve()
    current = load_project(current_path)
    if current_path == previous_path or not previous_path.is_file():
        return current
    try:
        previous = load_project(previous_path)
    except ValueError:
        return current
    try:
        same_source = (
            Path(str(current.get("source_folder", ""))).expanduser().resolve()
            == Path(str(previous.get("source_folder", ""))).expanduser().resolve()
        )
    except (OSError, ValueError):
        same_source = False
    if not same_source:
        return current

    def available(key: str, item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        candidates = (
            [item.get("path"), item.get("provenance")]
            if key == "renders" else
            [item.get("contact_sheet"), item.get("report")]
        )
        return any(
            isinstance(candidate, str) and candidate
            and Path(candidate).expanduser().is_file()
            for candidate in candidates
        )

    changed: dict[str, int] = {}
    for key in ("renders", "renderer_comparisons"):
        existing = list(current.get("artifacts", {}).get(key, []) or [])
        fingerprints = {
            hashlib.sha256(json.dumps(
                item, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False).encode("utf-8")).hexdigest()
            for item in existing if isinstance(item, dict)
        }
        imported = 0
        for item in previous.get("artifacts", {}).get(key, []) or []:
            if not available(key, item):
                continue
            fingerprint = hashlib.sha256(json.dumps(
                item, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False).encode("utf-8")).hexdigest()
            if fingerprint in fingerprints:
                continue
            existing.append(deepcopy(item))
            fingerprints.add(fingerprint)
            imported += 1
        if imported:
            current["artifacts"][key] = existing
            changed[key] = imported
    if not changed:
        return current
    timestamp = _now()
    current["stage"] = "develop"
    current["updated_at"] = timestamp
    current.setdefault("history", []).append({
        "updated_at": timestamp,
        "kind": "legacy-development-artifact-import",
        "source": str(previous_path),
        "source_sha256": hashlib.sha256(previous_path.read_bytes()).hexdigest(),
        "imported": changed,
        "files_moved": False,
    })
    _atomic_write(current_path, current)
    return current
