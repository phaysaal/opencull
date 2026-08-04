"""Persistent project library independent from the transient activity queue."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scan import BITMAP_EXTENSIONS, image_files

from .project import (
    ensure_project_layout,
    load_or_create_folder_project,
    load_project,
    project_manifest_path,
    project_sha256,
    register_file_artifact,
    update_project,
)
from .report import ReportError, load_report

CATALOG_FORMAT = "darkimiya-project-catalog-v1"


class ProjectCatalogError(ValueError):
    """A project-library request cannot be completed safely."""


def _latest_artifact_path(manifest: dict[str, Any], key: str) -> str:
    """The most recent path recorded under one artifact key.

    Some artifacts are singletons and some are append-only history, so the
    manifest holds either an object or a list. The newest of a list is the
    one a person means when they say "the shortlist".
    """
    value = manifest.get("artifacts", {}).get(key)
    if isinstance(value, dict):
        return str(value.get("path", ""))
    if isinstance(value, list):
        for item in reversed(value):
            if isinstance(item, dict) and item.get("path"):
                return str(item["path"])
    return ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
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


class ProjectCatalog:
    """Small Application Support index pointing at folder-owned projects."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self._state = self._load()

    def _empty(self) -> dict[str, Any]:
        return {"format": CATALOG_FORMAT, "revision": 0, "projects": []}

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectCatalogError(f"cannot read project library: {exc}") from exc
        if not isinstance(value, dict) or value.get("format") != CATALOG_FORMAT:
            raise ProjectCatalogError("project library has an unsupported format")
        if not isinstance(value.get("projects"), list):
            raise ProjectCatalogError("project library entries are invalid")
        return value

    def _save(self) -> None:
        self._state["revision"] = int(self._state.get("revision", 0)) + 1
        self._state["updated_at"] = _now()
        _atomic_json(self.path, self._state)

    def add(self, source_folder: str) -> dict[str, Any]:
        source = Path(source_folder).expanduser().resolve()
        if not source.is_dir():
            raise ProjectCatalogError(f"project folder is unavailable: {source}")
        try:
            project_path, project = load_or_create_folder_project(
                source, source.name)
        except (OSError, ValueError) as exc:
            raise ProjectCatalogError(f"cannot create project: {exc}") from exc
        self._upsert({
            "id": project["id"], "name": project.get("name", source.name),
            "photos": str(source), "project": str(project_path),
            "added_at": _now(), "last_opened_at": _now(),
        })
        self._save()
        return self.public()

    def add_existing_report(
        self, source_folder: str, report_path: str,
    ) -> dict[str, Any]:
        """Import a previously opened result as project evidence, idempotently."""
        source = Path(source_folder).expanduser().resolve()
        report = Path(report_path).expanduser().resolve()
        try:
            loaded = load_report(report)
        except ReportError:
            # The native recent-items library also contains shortlist and
            # development evidence. Those are not culling reports and must not
            # become project selection evidence.
            return self.public()
        record = next((
            item for item in self._state["projects"]
            if Path(str(item["photos"])).expanduser() == source
        ), None)
        if record is None:
            if source.is_dir():
                self.add(str(source))
                record = next(
                    item for item in self._state["projects"]
                    if Path(str(item["photos"])).expanduser() == source)
            else:
                record = {
                    "id": "legacy-" + hashlib.sha256(
                        str(source).encode("utf-8")).hexdigest()[:20],
                    "name": source.name or report.stem,
                    "photos": str(source),
                    "project": str(project_manifest_path(source)),
                    "added_at": _now(), "last_opened_at": _now(),
                    "imported_from_recent_review": True,
                }
                self._state["projects"].append(record)
        record["historical_report"] = str(report)
        if not source.is_dir():
            record["import_warning"] = (
                "The photograph folder is currently unavailable. Reconnect its "
                "volume to restore previews and continue this project."
            )
            self._save()
            return self.public()
        missing = [name for name in loaded.photo_names if not (source / name).is_file()]
        if missing:
            record["import_warning"] = (
                f"{len(missing)} of {len(loaded.photo_names)} report photographs "
                "are currently missing from the project folder. Reconnect or "
                "recover the source files to restore every preview."
            )
            self._save()
        else:
            record.pop("import_warning", None)
        manifest = load_project(Path(str(record["project"])))
        current = manifest.get("artifacts", {}).get("culling_report")
        if isinstance(current, dict) and current.get("path"):
            return self.public()
        register_file_artifact(
            Path(str(record["project"])), "culling_report", report,
            stage="cull", singleton=True)
        return self.public()

    def import_jobs(self, queue: dict[str, Any]) -> None:
        """Make legacy queue-owned folders discoverable without deleting jobs."""
        changed = False
        for job in queue.get("jobs", []):
            if job.get("kind", "culling") != "culling":
                continue
            project_id = str(job.get("project_id") or "").strip()
            photos_text = str(job.get("photos") or "").strip()
            if not photos_text:
                continue
            photos = Path(photos_text).expanduser().resolve()
            project_path = Path(str(job.get("project") or "")).expanduser()
            record = next((
                item for item in self._state["projects"]
                if Path(str(item.get("photos", ""))).expanduser() == photos
            ), None)
            name = photos.name or "Recovered project"
            if photos.is_dir():
                try:
                    project_path, manifest = load_or_create_folder_project(
                        photos, name)
                    project_id = str(manifest["id"])
                    name = str(manifest.get("name") or name)
                except (OSError, ValueError):
                    continue
            elif not project_id:
                project_id = "legacy-" + hashlib.sha256(
                    str(photos).encode("utf-8")).hexdigest()[:20]
            if record is None:
                record = {
                    "id": project_id, "name": name,
                    "photos": str(photos), "project": str(project_path),
                    "added_at": str(job.get("created_at") or _now()),
                    "last_opened_at": str(job.get("created_at") or _now()),
                    "imported_from_queue": True,
                }
                self._state["projects"].append(record)
                changed = True
            elif photos.is_dir() and (
                record.get("id") != project_id
                or record.get("project") != str(project_path)
            ):
                record["id"] = project_id
                record["project"] = str(project_path)
                record["name"] = name
                changed = True
        if changed:
            self._save()
        # Completed legacy culls predate project identifiers. Their immutable
        # result is still useful project evidence, even when some originals
        # have since been moved away.
        for job in queue.get("jobs", []):
            if (job.get("kind", "culling") != "culling"
                    or job.get("status") != "completed"):
                continue
            photos = str(job.get("photos") or "").strip()
            report = str(job.get("output") or "").strip()
            if photos and report and Path(photos).expanduser().is_dir() \
                    and Path(report).expanduser().is_file():
                self.add_existing_report(photos, report)

    def _upsert(self, record: dict[str, Any]) -> None:
        self._state["projects"] = [
            item for item in self._state["projects"]
            if item.get("id") != record["id"]
            and Path(str(item.get("photos", ""))).expanduser() != Path(record["photos"])
        ]
        self._state["projects"].insert(0, record)

    def remove(self, project_id: str) -> dict[str, Any]:
        """Forget a folder without touching anything inside it.

        This removes the library entry only. The photographs, the project
        manifest, any culling report and any review sidecar all stay where
        they are, so adding the folder again recovers the work. Nothing in
        Darkimiya deletes a photograph.
        """
        record = self.record_for(project_id)
        self._state["projects"] = [
            item for item in self._state["projects"]
            if item.get("id") != project_id
        ]
        self._state["revision"] = int(self._state.get("revision", 0)) + 1
        self._save()
        return record

    def record_for(self, project_id: str) -> dict[str, Any]:
        for record in self._state["projects"]:
            if record.get("id") == project_id:
                return record
        raise ProjectCatalogError("unknown Darkimiya project")

    def public(self, queue: dict[str, Any] | None = None) -> dict[str, Any]:
        jobs = (queue or {}).get("jobs", [])
        projects = []
        for record in self._state["projects"]:
            project_path = Path(str(record.get("project", ""))).expanduser()
            photos = Path(str(record.get("photos", ""))).expanduser()
            manifest: dict[str, Any] = {}
            if project_path.is_file():
                try:
                    manifest = load_project(project_path)
                except ValueError:
                    manifest = {}
            project_jobs = [
                job for job in jobs
                if job.get("project_id") == record.get("id")
                or (
                    job.get("kind", "culling") == "culling"
                    and str(job.get("photos") or "").strip()
                    and Path(str(job.get("photos"))).expanduser().resolve()
                    == photos.resolve()
                )
            ]
            culling_jobs = [job for job in project_jobs if job.get("kind", "culling") == "culling"]
            current_cull = next(
                (job for job in reversed(culling_jobs)
                 if job.get("status") not in {"completed", "cancelled"}),
                culling_jobs[-1] if culling_jobs else None)
            report_record = manifest.get("artifacts", {}).get("culling_report")
            report = str(report_record.get("path", "")) if isinstance(report_record, dict) else ""
            if not report:
                report = str(record.get("historical_report") or "")
            # The assessment is a second run over the same folder, so the
            # library needs its state as well as the cull's: a card cannot
            # offer to start one that is already in flight.
            assessment_jobs = [
                job for job in project_jobs
                if job.get("kind") == "professional_shortlist"]
            current_assessment = next(
                (job for job in reversed(assessment_jobs)
                 if job.get("status") not in {"completed", "cancelled"}),
                assessment_jobs[-1] if assessment_jobs else None)
            shortlist = _latest_artifact_path(manifest, "shortlist")
            projects.append({
                **record,
                "name": str(manifest.get("name") or record.get("name") or photos.name),
                "stage": str(manifest.get("stage") or "import"),
                "available": photos.is_dir(),
                "manifest_available": project_path.is_file(),
                "manifest_sha256": project_sha256(project_path) if project_path.is_file() else "",
                "report": report,
                "report_available": bool(report and Path(report).is_file()),
                "culling": current_cull,
                "assessment": current_assessment,
                "shortlist": shortlist,
                "shortlist_available": bool(
                    shortlist and Path(shortlist).is_file()),
                "activity_count": len(project_jobs),
            })
        return {
            "format": CATALOG_FORMAT,
            "revision": self._state.get("revision", 0),
            "projects": projects,
        }

    def manual_selection_report(self, project_id: str) -> Path:
        """Create a deterministic all-included report without AI culling."""
        record = self.record_for(project_id)
        photos = Path(str(record["photos"])).expanduser().resolve()
        project_path = Path(str(record["project"])).expanduser().resolve()
        if not photos.is_dir() or not project_path.is_file():
            raise ProjectCatalogError("project folder or manifest is unavailable")
        paths = image_files(photos, True)
        if not paths:
            raise ProjectCatalogError("project contains no supported photographs")
        families: dict[tuple[str, str], list[Path]] = {}
        for path in paths:
            relative = path.relative_to(photos)
            families.setdefault(
                (relative.parent.as_posix().casefold(), relative.stem.casefold()),
                []).append(path)
        clusters = []
        decisions = []
        inventory = []
        for index, family in enumerate(families.values(), start=1):
            names = sorted(path.relative_to(photos).as_posix() for path in family)
            preferred = [name for name in names if Path(name).suffix.casefold() in BITMAP_EXTENSIONS]
            keepers = preferred[:1] or names[:1]
            cluster_id = f"manual-{index:05d}"
            clusters.append({"cluster_id": cluster_id, "photos": names})
            decisions.append({
                "cluster_id": cluster_id, "photos": keepers,
                "rationale": "Included for manual selection; no AI culling was performed.",
                "confidence": 1.0, "warning": "Manual-selection import",
                "fallback": False, "photographic_assessment": [],
            })
            inventory.extend(
                f"{name}\0{(photos / name).stat().st_size}\0{(photos / name).stat().st_mtime_ns}"
                for name in names)
        identity = hashlib.sha256("\n".join(inventory).encode("utf-8")).hexdigest()
        layout = ensure_project_layout(photos)
        base = layout["Reports"] / f"{photos.name}-manual-selection.json"
        output = base
        revision = 2
        while output.exists():
            try:
                existing = json.loads(output.read_text(encoding="utf-8"))
                if existing.get("manifest_sha256") == identity:
                    register_file_artifact(
                        project_path, "culling_report", output,
                        stage="cull", singleton=True)
                    return output
            except (OSError, json.JSONDecodeError):
                pass
            output = base.with_name(f"{base.stem}-v{revision}.json")
            revision += 1
        report = {
            "format": "opencull-report-v2",
            "manifest_sha256": identity,
            "clusters": clusters, "keep": decisions,
            "warnings": ["AI culling was skipped; all asset families await human selection."],
            "adaptive_clustering": {"enabled": False, "mode": "manual-selection"},
            "notice": "Created locally by Darkimiya without model or network use.",
        }
        _atomic_json(output, report)
        register_file_artifact(
            project_path, "culling_report", output,
            stage="cull", singleton=True)
        update_project(project_path, stage="cull")
        return output
