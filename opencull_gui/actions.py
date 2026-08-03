"""Deterministic, journaled Phase 4 export and organization operations."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import shutil
import tempfile
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from scan import BITMAP_EXTENSIONS, RAW_EXTENSIONS, open_preview

from .photos import PhotoError, PhotoStore
from .project import (
    ensure_project_layout,
    load_project,
    register_file_artifact,
    update_project,
)
from .raw_sources import RawSourceStore
from .report import ReportIndex
from .reviews import ReviewStore

PLAN_FORMAT = "opencull-operation-plan-v1"
JOURNAL_FORMAT = "opencull-operation-journal-v1"
PAIR_EXTENSIONS = RAW_EXTENSIONS | {".jpg", ".jpeg"}
POLICIES = {
    "human_only", "effective", "require_all", "ai_only", "modified_only",
}
LAYOUTS = {"opensull", "selected_by_cluster", "cluster_inspection"}
ACTIONS = {"copy", "move", "trash", "project_filter", "system_cleanup"}
SELECTION_SCOPES = {"selected", "unselected", "all"}
CLEANUP_POLICIES = {
    "preserve_useful_raws", "keep_every_raw", "jpeg_only", "inspect",
}


class ActionError(ValueError):
    """An export or filesystem operation is unsafe or invalid."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
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


def policy_clusters(review: ReviewStore, policy: str) -> list[dict[str, Any]]:
    if policy not in POLICIES:
        raise ActionError(f"unsupported selection policy: {policy}")
    exported = review.export()
    clusters = exported["clusters"]
    if policy == "require_all" and any(
            not cluster["human_reviewed"] for cluster in clusters):
        unreviewed = sum(
            not cluster["human_reviewed"] for cluster in clusters)
        raise ActionError(
            f"{unreviewed} clusters remain unreviewed; export is refused")
    chosen = []
    for cluster in clusters:
        ai = cluster["ai_keepers"]
        human = cluster["keepers"] if cluster["human_reviewed"] else []
        if policy in {"effective", "require_all"}:
            keepers = cluster["keepers"]
        elif policy == "human_only":
            keepers = human
        elif policy == "ai_only":
            keepers = ai
        else:
            keepers = (
                human if cluster["human_reviewed"]
                and set(human) != set(ai) else []
            )
        chosen.append({
            **cluster,
            "keepers": list(keepers),
            "export_policy": policy,
        })
    return chosen


def export_bytes(review: ReviewStore, policy: str, format_name: str) -> tuple[
        bytes, str, str]:
    clusters = policy_clusters(review, policy)
    rows = [
        {
            "cluster_id": cluster["cluster_id"],
            "filename": name,
            "decision_source": (
                "ai" if policy == "ai_only"
                else "human" if policy in {"human_only", "modified_only"}
                else "human" if cluster["human_reviewed"] else "ai"
            ),
            "human_note": cluster["human_note"],
        }
        for cluster in clusters for name in cluster["keepers"]
    ]
    if format_name == "json":
        payload = {
            "format": "opencull-selection-export-v1",
            "policy": policy,
            "exported_at": _now(),
            "clusters": clusters,
            "selected_photos": [row["filename"] for row in rows],
        }
        return (
            (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode(),
            "application/json",
            "opencull-selection.json",
        )
    if format_name == "text":
        return (
            ("\n".join(row["filename"] for row in rows) + "\n").encode(),
            "text/plain; charset=utf-8",
            "opencull-selection.txt",
        )
    if format_name == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "cluster_id", "filename", "decision_source", "human_note"],
        )
        writer.writeheader()
        writer.writerows(rows)
        return (
            output.getvalue().encode(),
            "text/csv; charset=utf-8",
            "opencull-selection.csv",
        )
    raise ActionError(f"unsupported export format: {format_name}")


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        raise ActionError(f"no existing parent for destination: {path}")
    return candidate


def _destination_for(
    root: Path,
    layout: str,
    cluster_number: int,
    name: str,
    selected: bool,
    preserve_relative: bool,
) -> Path:
    relative = Path(name) if preserve_relative else Path(name).name
    if layout == "opensull":
        return root / relative
    cluster = root / f"cluster-{cluster_number:04d}"
    if layout == "selected_by_cluster":
        category = "selected" if selected else "not-selected"
        return cluster / category / relative
    category = "selected" if selected else "not-selected"
    return cluster / category / relative


def _trash_root_for(source: Path, batch_name: str) -> Path:
    """Return the recoverable macOS Trash directory on the source volume."""
    resolved = source.resolve()
    parts = resolved.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path("/Volumes") / parts[2] / ".Trashes" / str(os.getuid()) / batch_name
    return Path.home() / ".Trash" / batch_name


def build_plan(
    report: ReportIndex,
    reviews: ReviewStore,
    photos: PhotoStore,
    destination: Path | None,
    policy: str,
    action: str,
    layout: str,
    preserve_relative: bool = False,
    include_companions: bool = False,
    selection_scope: str | None = None,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ActionError(f"unsupported action: {action}")
    if layout not in LAYOUTS:
        raise ActionError(f"unsupported layout: {layout}")
    if selection_scope is None:
        selection_scope = "all" if layout == "cluster_inspection" else "selected"
    if selection_scope not in SELECTION_SCOPES:
        raise ActionError(f"unsupported selection scope: {selection_scope}")
    if action == "trash" and selection_scope != "unselected":
        raise ActionError("Trash operations are limited to unselected photographs")
    if action != "trash":
        if destination is None:
            raise ActionError("destination is required")
        destination = destination.expanduser().resolve()
        if destination == photos.root:
            raise ActionError("destination cannot be the photo root itself")
    review_revision = reviews.public_state()["revision"]
    trash_token = hashlib.sha256(
        f"{report.path}:{review_revision}".encode()).hexdigest()[:8]
    trash_batch = f"Darkimiya-{report.path.stem}-{trash_token}"
    selected_clusters = policy_clusters(reviews, policy)
    items = []
    destination_names: set[str] = set()
    source_names: set[str] = set()
    missing = []
    collisions = []
    total_bytes = 0

    def add_item(
        cluster_number: int,
        cluster_id: str,
        name: str,
        selected: bool,
        companion: bool = False,
    ) -> None:
        nonlocal total_bytes
        identity = f"{cluster_id}\0{name}\0{selected}"
        if identity in source_names:
            return
        source_names.add(identity)
        try:
            source = photos.resolve(name)
        except PhotoError:
            missing.append(name)
            return
        relative = Path(name) if preserve_relative else Path(name).name
        target = (
            _trash_root_for(source, trash_batch) / relative
            if action == "trash"
            else _destination_for(
                destination, layout, cluster_number, name, selected,
                preserve_relative)
        )
        target_key = os.path.normcase(str(target))
        if target_key in destination_names or target.exists():
            collisions.append(str(target))
        destination_names.add(target_key)
        size = source.stat().st_size
        total_bytes += size
        items.append({
            "id": hashlib.sha256(identity.encode()).hexdigest()[:16],
            "cluster_id": cluster_id,
            "source_name": name,
            "source": str(source),
            "destination": str(target),
            "selected": selected,
            "companion": companion,
            "bytes": size,
            "status": "pending",
        })

    for number, cluster in enumerate(selected_clusters, start=1):
        selected = set(cluster["keepers"])
        if selection_scope == "selected":
            names = cluster["keepers"]
        elif selection_scope == "unselected":
            names = [
                name for name in cluster["photos"] if name not in selected]
        else:
            names = cluster["photos"]
        for name in names:
            add_item(number, cluster["cluster_id"], name, name in selected)
            if include_companions and Path(name).suffix.lower() in PAIR_EXTENSIONS:
                source_relative = Path(name)
                parent = photos.root / source_relative.parent
                if parent.is_dir():
                    for sibling in parent.iterdir():
                        if (
                            sibling.is_file()
                            and sibling.stem.casefold()
                            == source_relative.stem.casefold()
                            and sibling.suffix.lower() in PAIR_EXTENSIONS
                            and sibling.name != source_relative.name
                        ):
                            companion_name = (
                                source_relative.parent / sibling.name).as_posix()
                            add_item(
                                number, cluster["cluster_id"], companion_name,
                                name in selected, True)

    free_bytes = min(
        (shutil.disk_usage(_nearest_existing(Path(item["destination"]))).free
         for item in items),
        default=0,
    )
    errors = []
    if missing:
        errors.append(f"{len(missing)} source photographs are missing")
    if collisions:
        errors.append(f"{len(collisions)} destination collisions exist")
    if total_bytes > free_bytes:
        errors.append("destination does not have enough free space")
    if not items:
        errors.append("selection policy produced no files")
    plan_core = {
        "format": PLAN_FORMAT,
        "report_sha256": report.sha256,
        "review_revision": review_revision,
        "created_at": _now(),
        "policy": policy,
        "action": action,
        "layout": layout,
        "destination": (
            "macOS Trash" if action == "trash" else str(destination)),
        "selection_scope": selection_scope,
        "preserve_relative": bool(preserve_relative),
        "include_companions": bool(include_companions),
        "items": items,
        "summary": {
            "files": len(items),
            "selected_files": sum(item["selected"] for item in items),
            "unselected_files": sum(
                not item["selected"] for item in items),
            "companion_files": sum(item["companion"] for item in items),
            "bytes": total_bytes,
            "free_bytes": free_bytes,
            "missing": missing,
            "collisions": collisions,
            "errors": errors,
            "unreviewed_clusters": sum(
                not cluster["human_reviewed"]
                for cluster in selected_clusters),
        },
    }
    signature = hashlib.sha256(json.dumps(
        plan_core, sort_keys=True).encode()).hexdigest()
    return {
        **plan_core,
        "plan_id": signature[:20],
        "signature": signature,
        "confirmation_code": f"{int(signature[:8], 16) % 10000:04d}",
    }


def build_project_filter_plan(
    report: ReportIndex,
    reviews: ReviewStore,
    photos: PhotoStore,
    project_path: Path,
    raw_sources: RawSourceStore | None = None,
) -> dict[str, Any]:
    """Plan reversible project quarantine and conservative RAW retention."""
    project = load_project(project_path)
    if Path(project["source_folder"]).expanduser().resolve() != photos.root:
        raise ActionError("project source folder does not match this review")
    layout = ensure_project_layout(photos.root)
    clusters = policy_clusters(reviews, "require_all")
    revision = reviews.public_state()["revision"]
    keepers = {
        name for cluster in clusters for name in cluster.get("keepers", [])
    }
    report_photos = list(dict.fromkeys(
        name for cluster in clusters for name in cluster.get("photos", [])))
    reserve: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    def reserve_source(
        path: Path, source_name: str, anchor: str, external: bool,
        relative: Path,
    ) -> None:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            missing.append(str(resolved))
            return
        reserve[str(resolved)] = {
            "source": resolved, "source_name": source_name,
            "anchor": anchor, "external": external, "relative": relative,
        }

    raw_state = raw_sources.public() if raw_sources is not None else {}
    raw_root = (
        Path(str(raw_state.get("root", ""))).expanduser().resolve()
        if raw_state.get("configured") else None)
    for keeper in sorted(keepers):
        keeper_relative = Path(keeper)
        try:
            keeper_path = photos.resolve(keeper)
        except PhotoError:
            missing.append(keeper)
            continue
        if keeper_path.suffix.lower() in RAW_EXTENSIONS:
            reserve_source(
                keeper_path, keeper, keeper, False, keeper_relative)
        parent = keeper_path.parent
        try:
            siblings = list(parent.iterdir())
        except OSError:
            siblings = []
        for sibling in siblings:
            if (
                sibling.is_file()
                and sibling.stem.casefold() == keeper_path.stem.casefold()
                and sibling.suffix.lower() in RAW_EXTENSIONS
            ):
                relative = sibling.relative_to(photos.root)
                reserve_source(
                    sibling, relative.as_posix(), keeper, False, relative)
        if raw_root is not None:
            for raw_relative_text in raw_state.get("matches", {}).get(keeper, []):
                raw_relative = Path(str(raw_relative_text))
                reserve_source(
                    raw_root / raw_relative, raw_relative.as_posix(), keeper,
                    True, Path("External") / raw_relative)

    items: list[dict[str, Any]] = []
    destinations: set[str] = set()
    source_paths: set[str] = set()
    collisions: list[str] = []
    total_bytes = 0

    def add_item(
        source: Path, source_name: str, destination: Path, category: str,
        operation: str, cluster_id: str = "",
    ) -> None:
        nonlocal total_bytes
        resolved = source.expanduser().resolve()
        source_key = os.path.normcase(str(resolved))
        if source_key in source_paths:
            return
        source_paths.add(source_key)
        destination = destination.expanduser().resolve()
        destination_key = os.path.normcase(str(destination))
        if destination_key in destinations or destination.exists():
            collisions.append(str(destination))
        destinations.add(destination_key)
        size = resolved.stat().st_size
        total_bytes += size
        identity = f"{category}\0{source_key}\0{destination_key}"
        items.append({
            "id": hashlib.sha256(identity.encode()).hexdigest()[:16],
            "cluster_id": cluster_id,
            "source_name": source_name,
            "source": str(resolved),
            "destination": str(destination),
            "selected": category == "raw_reserve",
            "companion": category == "raw_reserve",
            "category": category,
            "operation": operation,
            "bytes": size,
            "status": "pending",
        })

    reserved_paths = set(reserve)
    cluster_for = {
        name: cluster["cluster_id"]
        for cluster in clusters for name in cluster.get("photos", [])
    }
    for name in report_photos:
        if name in keepers:
            continue
        try:
            source = photos.resolve(name)
        except PhotoError:
            missing.append(name)
            continue
        if str(source) in reserved_paths:
            continue
        add_item(
            source, name, layout["Rejected"] / Path(name), "rejected",
            "move", cluster_for.get(name, ""))
    for entry in reserve.values():
        add_item(
            entry["source"], entry["source_name"],
            layout["RAW Reserve"] / entry["relative"], "raw_reserve",
            "copy" if entry["external"] else "move",
            cluster_for.get(entry["anchor"], ""))

    free_bytes = min(
        (shutil.disk_usage(_nearest_existing(Path(item["destination"]))).free
         for item in items), default=0)
    errors: list[str] = []
    if missing:
        errors.append(f"{len(set(missing))} source files are missing")
    if collisions:
        errors.append(f"{len(collisions)} project destination collisions exist")
    if total_bytes > free_bytes:
        errors.append("project volume does not have enough temporary free space")
    if not items:
        errors.append("there are no reviewed rejections or RAW companions to organize")
    core = {
        "format": PLAN_FORMAT,
        "report_sha256": report.sha256,
        "project_id": project.get("id"),
        "project_path": str(project_path.expanduser().resolve()),
        "review_revision": revision,
        "created_at": _now(),
        "policy": "require_all",
        "action": "project_filter",
        "layout": "project_library",
        "destination": str(layout["root"]),
        "selection_scope": "rejected_and_raw_reserve",
        "preserve_relative": True,
        "include_companions": True,
        "items": items,
        "summary": {
            "files": len(items),
            "selected_files": sum(
                item["category"] == "raw_reserve" for item in items),
            "unselected_files": sum(
                item["category"] == "rejected" for item in items),
            "companion_files": sum(
                item["category"] == "raw_reserve" for item in items),
            "rejected_files": sum(
                item["category"] == "rejected" for item in items),
            "raw_reserve_files": sum(
                item["category"] == "raw_reserve" for item in items),
            "external_raw_copies": sum(
                item["category"] == "raw_reserve"
                and item["operation"] == "copy" for item in items),
            "bytes": total_bytes, "free_bytes": free_bytes,
            "missing": sorted(set(missing)), "collisions": collisions,
            "errors": errors, "unreviewed_clusters": 0,
        },
    }
    signature = hashlib.sha256(
        json.dumps(core, sort_keys=True).encode()).hexdigest()
    return {
        **core, "plan_id": signature[:20], "signature": signature,
        "confirmation_code": f"{int(signature[:8], 16) % 10000:04d}",
    }


def cleanup_candidates(project_path: Path) -> dict[str, Any]:
    """Return the current quarantine inventory without changing project state."""
    project = load_project(project_path)
    source = Path(project["source_folder"]).expanduser().resolve()
    layout = ensure_project_layout(source)
    rejected_root = layout["Rejected"].resolve()
    candidates = []
    for path in sorted(rejected_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(rejected_root).as_posix()
        suffix = path.suffix.lower()
        candidates.append({
            "relative_path": relative,
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "kind": (
                "raw" if suffix in RAW_EXTENSIONS
                else "jpeg" if suffix in {".jpg", ".jpeg"}
                else "photo" if suffix in BITMAP_EXTENSIONS else "other"),
        })
    return {
        "format": "darkimiya-cleanup-candidates-v1",
        "project_id": project.get("id"),
        "rejected_root": str(rejected_root),
        "candidates": candidates,
        "summary": {
            "files": len(candidates),
            "raw_files": sum(item["kind"] == "raw" for item in candidates),
            "jpeg_files": sum(item["kind"] == "jpeg" for item in candidates),
            "bytes": sum(item["bytes"] for item in candidates),
        },
    }


def build_system_cleanup_plan(
    report: ReportIndex,
    reviews: ReviewStore,
    photos: PhotoStore,
    project_path: Path,
    cleanup_policy: str = "preserve_useful_raws",
    selected_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Plan an explicit Finder-compatible cleanup from project quarantine."""
    if cleanup_policy not in CLEANUP_POLICIES:
        raise ActionError(f"unsupported final-cleanup policy: {cleanup_policy}")
    project = load_project(project_path)
    if Path(project["source_folder"]).expanduser().resolve() != photos.root:
        raise ActionError("project source folder does not match this review")
    policy_clusters(reviews, "require_all")
    layout = ensure_project_layout(photos.root)
    inventory = cleanup_candidates(project_path)
    chosen: set[str] = set()
    if cleanup_policy == "inspect":
        for value in selected_paths or []:
            relative = Path(str(value))
            if relative.is_absolute() or ".." in relative.parts:
                raise ActionError(f"unsafe cleanup selection: {value!r}")
            chosen.add(relative.as_posix())
        known = {item["relative_path"] for item in inventory["candidates"]}
        unknown = sorted(chosen - known)
        if unknown:
            raise ActionError(
                f"cleanup selection contains {len(unknown)} unavailable files")
    token_seed = "\0".join([
        str(project.get("id", "")), report.sha256, cleanup_policy,
        *sorted(chosen),
    ])
    trash_token = hashlib.sha256(token_seed.encode()).hexdigest()[:8]
    trash_batch = f"Darkimiya-{photos.root.name}-cleanup-{trash_token}"
    items: list[dict[str, Any]] = []
    destinations: set[str] = set()
    collisions: list[str] = []
    total_bytes = 0
    for candidate in inventory["candidates"]:
        relative = Path(candidate["relative_path"])
        kind = candidate["kind"]
        if cleanup_policy == "jpeg_only" and kind != "jpeg":
            continue
        if cleanup_policy == "inspect" and candidate["relative_path"] not in chosen:
            continue
        source = Path(candidate["path"])
        retain_raw = cleanup_policy == "keep_every_raw" and kind == "raw"
        destination = (
            layout["RAW Reserve"] / "Retained from Rejected" / relative
            if retain_raw else _trash_root_for(source, trash_batch) / relative)
        operation = "move" if retain_raw else "trash"
        category = "raw_reserve" if retain_raw else "system_trash"
        destination = destination.expanduser().resolve()
        destination_key = os.path.normcase(str(destination))
        if destination_key in destinations or destination.exists():
            collisions.append(str(destination))
        destinations.add(destination_key)
        total_bytes += int(candidate["bytes"])
        identity = f"{category}\0{source}\0{destination}"
        items.append({
            "id": hashlib.sha256(identity.encode()).hexdigest()[:16],
            "cluster_id": "",
            "source_name": candidate["relative_path"],
            "source": str(source), "destination": str(destination),
            "selected": False, "companion": kind == "raw",
            "category": category, "operation": operation,
            "kind": kind, "bytes": int(candidate["bytes"]),
            "status": "pending",
        })
    free_bytes = min(
        (shutil.disk_usage(_nearest_existing(Path(item["destination"]))).free
         for item in items), default=0)
    errors: list[str] = []
    if collisions:
        errors.append(f"{len(collisions)} cleanup destination collisions exist")
    if total_bytes > free_bytes:
        errors.append("destination does not have enough temporary free space")
    if not items:
        errors.append(
            "select at least one quarantined file"
            if cleanup_policy == "inspect"
            else "this cleanup policy produced no files")
    review_revision = reviews.public_state()["revision"]
    core = {
        "format": PLAN_FORMAT, "report_sha256": report.sha256,
        "project_id": project.get("id"),
        "project_path": str(project_path.expanduser().resolve()),
        "review_revision": review_revision, "created_at": _now(),
        "policy": "human_quarantine", "cleanup_policy": cleanup_policy,
        "action": "system_cleanup", "layout": "system_trash",
        "destination": "macOS System Trash",
        "selection_scope": "project_rejected", "preserve_relative": True,
        "include_companions": cleanup_policy == "keep_every_raw",
        "items": items,
        "summary": {
            "files": len(items), "selected_files": 0,
            "unselected_files": len(items),
            "companion_files": sum(item["kind"] == "raw" for item in items),
            "trashed_files": sum(
                item["category"] == "system_trash" for item in items),
            "retained_raw_files": sum(
                item["category"] == "raw_reserve" for item in items),
            "bytes": total_bytes, "free_bytes": free_bytes,
            "missing": [], "collisions": collisions, "errors": errors,
            "unreviewed_clusters": 0,
            "quarantine_files": inventory["summary"]["files"],
        },
    }
    signature = hashlib.sha256(
        json.dumps(core, sort_keys=True).encode()).hexdigest()
    return {
        **core, "plan_id": signature[:20], "signature": signature,
        "confirmation_code": f"{int(signature[:8], 16) % 10000:04d}",
    }


def default_journal_path(report: ReportIndex, plan: dict[str, Any]) -> Path:
    return report.path.with_name(
        f"{report.path.stem}.{plan['plan_id']}.operation.json")


class Operation:
    """One resumable verified copy/move operation."""

    def __init__(
        self,
        plan: dict[str, Any],
        journal_path: Path,
        on_complete: Any = None,
        on_rollback: Any = None,
    ):
        self.plan = deepcopy(plan)
        self.journal_path = journal_path.expanduser().resolve()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.on_complete = on_complete
        self.on_rollback = on_rollback
        self.journal = self._load_or_create()

    def _load_or_create(self) -> dict[str, Any]:
        if self.journal_path.exists():
            try:
                journal = json.loads(
                    self.journal_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ActionError(f"cannot resume journal: {exc}") from exc
            if (
                journal.get("format") != JOURNAL_FORMAT
                or journal.get("plan_signature") != self.plan["signature"]
            ):
                raise ActionError("journal does not match this operation plan")
            return journal
        return {
            "format": JOURNAL_FORMAT,
            "plan_signature": self.plan["signature"],
            "plan_id": self.plan["plan_id"],
            "plan": deepcopy(self.plan),
            "action": self.plan["action"],
            "destination": self.plan["destination"],
            "status": "planned",
            "created_at": _now(),
            "updated_at": _now(),
            "items": deepcopy(self.plan["items"]),
            "completed_files": 0,
            "completed_bytes": 0,
            "error": "",
            "rollback": {"status": "not-requested", "completed": 0, "error": ""},
        }

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                **deepcopy(self.journal),
                "journal_path": str(self.journal_path),
            }

    def _save(self) -> None:
        self.journal["updated_at"] = _now()
        _atomic_json(self.journal_path, self.journal)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ActionError("operation is already running")
            if self.journal["status"] == "completed":
                return
            self._cancel.clear()
            # Publish this transition before launching the worker. A resumed
            # operation must not appear paused to its first status poll.
            self.journal["status"] = "running"
            self.journal["error"] = ""
            self._save()
            self._thread = threading.Thread(
                target=self._run, name=f"opencull-operation-{self.plan['plan_id']}",
                daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            for item in self.journal["items"]:
                if item.get("status") == "completed":
                    continue
                if self._cancel.is_set():
                    with self._lock:
                        self.journal["status"] = "paused"
                        self._save()
                    return
                self._execute_item(item)
            # Integration registers this operation's journal and its moved
            # files as project artifacts. It runs before the journal reports
            # completion so that anything observing "completed" -- the polling
            # interface, or recovery reading the journal after a restart --
            # can rely on those artifacts already existing. Per-item statuses
            # are what the finalizers read, and those are already durable.
            if self.on_complete is not None:
                try:
                    self.on_complete(self.public())
                except Exception as exc:
                    with self._lock:
                        self.journal["integration_error"] = str(exc)
                        self._save()
            with self._lock:
                self.journal["status"] = "completed"
                self._save()
        except Exception as exc:
            with self._lock:
                self.journal["status"] = "failed"
                self.journal["error"] = str(exc)
                self._save()

    def _execute_item(self, item: dict[str, Any]) -> None:
        source = Path(item["source"])
        destination = Path(item["destination"])
        if destination.exists():
            raise ActionError(f"destination appeared during operation: {destination}")
        if not source.is_file():
            raise ActionError(f"source disappeared during operation: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{self.plan['plan_id']}.part")
        temporary.unlink(missing_ok=True)
        source_hash = _sha256(source)
        try:
            shutil.copy2(source, temporary)
            destination_hash = _sha256(temporary)
            if source_hash != destination_hash:
                raise ActionError(f"hash verification failed: {source.name}")
            os.replace(temporary, destination)
            operation = item.get("operation", self.plan["action"])
            if operation in {"move", "trash"}:
                source.unlink()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        with self._lock:
            item.update({
                "status": "completed",
                "sha256": source_hash,
                "completed_at": _now(),
            })
            self.journal["completed_files"] += 1
            self.journal["completed_bytes"] += int(item["bytes"])
            self._save()

    def rollback_move(self) -> None:
        with self._lock:
            if self.plan["action"] not in {
                "move", "trash", "project_filter", "system_cleanup",
            }:
                raise ActionError(
                    "rollback is available only for move or Trash operations")
            if self._thread and self._thread.is_alive():
                raise ActionError("cannot rollback while operation is running")
            self.journal["rollback"] = {
                "status": "running", "completed": 0, "error": ""}
            self._save()
        try:
            for item in reversed(self.journal["items"]):
                if item.get("status") != "completed":
                    continue
                source = Path(item["source"])
                destination = Path(item["destination"])
                operation = item.get("operation", self.plan["action"])
                if operation != "copy" and source.exists():
                    raise ActionError(
                        f"rollback source already exists: {source}")
                if not destination.is_file():
                    raise ActionError(
                        f"rollback destination is missing: {destination}")
                if _sha256(destination) != item.get("sha256"):
                    raise ActionError(
                        f"rollback hash changed: {destination}")
                if operation == "copy":
                    destination.unlink()
                else:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(destination, source)
                    except OSError as exc:
                        shutil.copy2(destination, source)
                        if _sha256(source) != item["sha256"]:
                            source.unlink(missing_ok=True)
                            raise ActionError(
                                f"rollback verification failed: {source}") from exc
                        destination.unlink()
                with self._lock:
                    item["status"] = "rolled-back"
                    self.journal["rollback"]["completed"] += 1
                    self._save()
            with self._lock:
                self.journal["rollback"]["status"] = "completed"
                self.journal["status"] = "rolled-back"
                self._save()
            if self.on_rollback is not None:
                self.on_rollback(self.public())
        except Exception as exc:
            with self._lock:
                self.journal["rollback"]["status"] = "failed"
                self.journal["rollback"]["error"] = str(exc)
                self._save()
            raise


def create_contact_sheets(
    reviews: ReviewStore,
    photos: PhotoStore,
    destination: Path,
    policy: str,
    columns: int = 4,
    rows: int = 5,
) -> list[str]:
    clusters = policy_clusters(reviews, policy)
    entries = [
        (cluster["cluster_id"], name)
        for cluster in clusters for name in cluster["keepers"]
    ]
    if not entries:
        raise ActionError("selection policy produced no contact-sheet photos")
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    columns = max(1, min(8, int(columns)))
    rows = max(1, min(10, int(rows)))
    cell_w, cell_h, label_h = 360, 260, 34
    per_page = columns * rows
    outputs = []
    for page_number, start in enumerate(
            range(0, len(entries), per_page), start=1):
        page = Image.new(
            "RGB", (columns * cell_w, rows * (cell_h + label_h)), "#151713")
        draw = ImageDraw.Draw(page)
        for offset, (cluster_id, name) in enumerate(
                entries[start:start + per_page]):
            column, row = offset % columns, offset // columns
            x, y = column * cell_w, row * (cell_h + label_h)
            try:
                image = open_preview(photos.resolve(name))
                image = ImageOps.contain(
                    image.convert("RGB"), (cell_w - 12, cell_h - 12),
                    Image.Resampling.LANCZOS)
                page.paste(
                    image, (x + (cell_w - image.width) // 2,
                            y + (cell_h - image.height) // 2))
            except Exception:
                draw.rectangle(
                    (x + 6, y + 6, x + cell_w - 6, y + cell_h - 6),
                    outline="#c75a4f", width=3)
            draw.text(
                (x + 8, y + cell_h + 8),
                f"{cluster_id} · {Path(name).name}",
                fill="#f1f0e9")
        output = destination / f"opencull-contact-sheet-{page_number:03d}.jpg"
        if output.exists():
            raise ActionError(f"contact sheet already exists: {output}")
        page.save(output, "JPEG", quality=90, optimize=True)
        outputs.append(str(output))
    return outputs


class ContactSheetOperation:
    """Background, page-journaled contact-sheet export."""

    def __init__(
        self,
        reviews: ReviewStore,
        photos: PhotoStore,
        destination: Path,
        policy: str,
        journal_path: Path,
        columns: int = 4,
        rows: int = 5,
    ):
        self.reviews = reviews
        self.photos = photos
        self.destination = destination.expanduser().resolve()
        self.policy = policy
        self.journal_path = journal_path.expanduser().resolve()
        self.columns = max(1, min(8, int(columns)))
        self.rows = max(1, min(10, int(rows)))
        self.operation_id = secrets.token_hex(10)
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        entries = [
            {"cluster_id": cluster["cluster_id"], "name": name}
            for cluster in policy_clusters(reviews, policy)
            for name in cluster["keepers"]
        ]
        if not entries:
            raise ActionError("selection policy produced no contact-sheet photos")
        self.journal = {
            "format": JOURNAL_FORMAT,
            "plan_id": self.operation_id,
            "action": "contact_sheet",
            "destination": str(self.destination),
            "policy": policy,
            "status": "planned",
            "created_at": _now(),
            "updated_at": _now(),
            "entries": entries,
            "total_files": len(entries),
            "completed_files": 0,
            "outputs": [],
            "error": "",
        }

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                **deepcopy(self.journal),
                "journal_path": str(self.journal_path),
            }

    def _save(self) -> None:
        self.journal["updated_at"] = _now()
        _atomic_json(self.journal_path, self.journal)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ActionError("contact-sheet operation is already running")
            self._thread = threading.Thread(
                target=self._run,
                name=f"opencull-contact-{self.operation_id}",
                daemon=True,
            )
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            with self._lock:
                self.journal["status"] = "running"
                self._save()
            self.destination.mkdir(parents=True, exist_ok=True)
            cell_w, cell_h, label_h = 360, 260, 34
            per_page = self.columns * self.rows
            entries = self.journal["entries"]
            for page_number, start in enumerate(
                    range(0, len(entries), per_page), start=1):
                if self._cancel.is_set():
                    with self._lock:
                        self.journal["status"] = "paused"
                        self._save()
                    return
                output = self.destination / (
                    f"opencull-contact-sheet-{page_number:03d}.jpg")
                if output.exists():
                    raise ActionError(
                        f"contact sheet already exists: {output}")
                page = Image.new(
                    "RGB",
                    (self.columns * cell_w, self.rows * (cell_h + label_h)),
                    "#151713",
                )
                draw = ImageDraw.Draw(page)
                page_entries = entries[start:start + per_page]
                for offset, entry in enumerate(page_entries):
                    column, row = offset % self.columns, offset // self.columns
                    x, y = column * cell_w, row * (cell_h + label_h)
                    image = open_preview(self.photos.resolve(entry["name"]))
                    image = ImageOps.contain(
                        image.convert("RGB"), (cell_w - 12, cell_h - 12),
                        Image.Resampling.LANCZOS)
                    page.paste(
                        image,
                        (x + (cell_w - image.width) // 2,
                         y + (cell_h - image.height) // 2),
                    )
                    draw.text(
                        (x + 8, y + cell_h + 8),
                        f"{entry['cluster_id']} · {Path(entry['name']).name}",
                        fill="#f1f0e9",
                    )
                temporary = output.with_suffix(".tmp")
                page.save(temporary, "JPEG", quality=90, optimize=True)
                os.replace(temporary, output)
                with self._lock:
                    self.journal["outputs"].append(str(output))
                    self.journal["completed_files"] += len(page_entries)
                    self._save()
            with self._lock:
                self.journal["status"] = "completed"
                self._save()
        except Exception as exc:
            with self._lock:
                self.journal["status"] = "failed"
                self.journal["error"] = str(exc)
                self._save()


class ActionController:
    """In-process plan registry and background operation coordinator."""

    def __init__(
        self,
        report: ReportIndex,
        reviews: ReviewStore,
        photos: PhotoStore,
        project_path: Path | None = None,
        raw_sources: RawSourceStore | None = None,
    ):
        self.report = report
        self.reviews = reviews
        self.photos = photos
        self.project_path = (
            project_path.expanduser().resolve() if project_path else None)
        self.raw_sources = raw_sources
        self.project_layout = (
            ensure_project_layout(photos.root) if project_path else None)
        self._lock = threading.RLock()
        self.plans: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, Operation | ContactSheetOperation] = {}

    def bind_project(self, project_path: Path) -> None:
        """Rebind future operations after an explicit project migration."""
        self.project_path = project_path.expanduser().resolve()
        self.project_layout = ensure_project_layout(self.photos.root)

    def recoverable_journals(self) -> list[dict[str, Any]]:
        """Return validated incomplete journals belonging to this report."""
        journals = []
        pattern = f"{self.report.path.stem}.*.operation.json"
        roots = [self.report.path.parent]
        if self.project_layout is not None:
            roots.append(self.project_layout["Operations"])
        candidates = list(dict.fromkeys(
            path for root in roots for path in root.glob(pattern)))
        for path in sorted(
            candidates,
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                plan = data.get("plan")
                if (
                    data.get("format") != JOURNAL_FORMAT
                    or not isinstance(plan, dict)
                    or data.get("plan_signature") != plan.get("signature")
                    or data.get("plan_id") != plan.get("plan_id")
                    or data.get("status") not in {
                        "planned", "running", "paused", "failed"}
                ):
                    continue
                plan_core = {
                    key: value for key, value in plan.items()
                    if key not in {
                        "plan_id", "signature", "confirmation_code"}
                }
                signature = hashlib.sha256(json.dumps(
                    plan_core, sort_keys=True).encode()).hexdigest()
                if (
                    signature != plan.get("signature")
                    or signature[:20] != plan.get("plan_id")
                ):
                    continue
                total = len(data.get("items", []))
                completed = sum(
                    item.get("status") == "completed"
                    for item in data.get("items", [])
                    if isinstance(item, dict)
                )
                journals.append({
                    "plan_id": plan["plan_id"],
                    "journal_path": str(path.resolve()),
                    "status": (
                        "interrupted"
                        if data.get("status") == "running"
                        else data.get("status")
                    ),
                    "action": plan.get("action"),
                    "destination": plan.get("destination"),
                    "completed_files": completed,
                    "total_files": total,
                    "remaining_files": max(0, total - completed),
                    "error": str(data.get("error", "")),
                    "updated_at": data.get("updated_at"),
                })
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return journals

    def preflight(self, **options: Any) -> dict[str, Any]:
        action = str(options.get("action", "copy"))
        destination_text = str(options.get("destination", "")).strip()
        if action == "project_filter":
            if self.project_path is None:
                raise ActionError("this review is not attached to a Darkimiya project")
            plan = build_project_filter_plan(
                self.report, self.reviews, self.photos,
                self.project_path, self.raw_sources)
            with self._lock:
                self.plans[plan["plan_id"]] = plan
            return deepcopy(plan)
        if action == "system_cleanup":
            if self.project_path is None:
                raise ActionError("this review is not attached to a Darkimiya project")
            requested = options.get("selected_cleanup_paths", [])
            plan = build_system_cleanup_plan(
                self.report, self.reviews, self.photos, self.project_path,
                str(options.get("cleanup_policy", "preserve_useful_raws")),
                requested if isinstance(requested, list) else [])
            with self._lock:
                self.plans[plan["plan_id"]] = plan
            return deepcopy(plan)
        if action != "trash" and not destination_text:
            raise ActionError("destination is required")
        plan = build_plan(
            self.report,
            self.reviews,
            self.photos,
            destination=Path(destination_text) if destination_text else None,
            policy=str(options.get("policy", "human_only")),
            action=action,
            layout=str(options.get("layout", "opensull")),
            preserve_relative=bool(options.get("preserve_relative", False)),
            include_companions=bool(options.get("include_companions", False)),
            selection_scope=str(options.get("selection_scope", "selected")),
        )
        with self._lock:
            self.plans[plan["plan_id"]] = plan
        return deepcopy(plan)

    def execute(self, plan_id: str, confirmation: str) -> dict[str, Any]:
        with self._lock:
            plan = self.plans.get(plan_id)
            if not plan:
                raise ActionError("unknown or expired operation plan")
            if plan["summary"]["errors"]:
                raise ActionError("preflight has errors; execution is disabled")
            current_revision = self.reviews.public_state()["revision"]
            if current_revision != plan["review_revision"]:
                raise ActionError(
                    "human review changed after preflight; create a new plan")
            expected = str(plan.get("confirmation_code", ""))
            if confirmation != expected:
                raise ActionError(
                    f"confirmation must be the four-digit code: {expected}")
            operation = self.operations.get(plan_id)
            if operation is None:
                journal_path = (
                    self.project_layout["Operations"] /
                    f"{self.report.path.stem}.{plan['plan_id']}.operation.json"
                    if plan.get("action") in {"project_filter", "system_cleanup"}
                    and self.project_layout is not None
                    else default_journal_path(self.report, plan))
                operation = Operation(
                    plan, journal_path,
                    on_complete=(
                        self._finalize_project_filter
                        if plan.get("action") == "project_filter" else None),
                    on_rollback=(
                        self._rollback_project_filter
                        if plan.get("action") == "project_filter" else None),
                )
                if plan.get("action") == "system_cleanup":
                    operation.on_complete = self._finalize_system_cleanup
                    operation.on_rollback = self._rollback_system_cleanup
                self.operations[plan_id] = operation
            operation.start()
            return operation.public()

    def contact_sheet(
        self,
        destination: Path,
        policy: str,
        confirmation: str,
        columns: int = 4,
        rows: int = 5,
    ) -> dict[str, Any]:
        if confirmation != "CREATE CONTACT SHEETS":
            raise ActionError(
                "confirmation must exactly equal: CREATE CONTACT SHEETS")
        journal = self.report.path.with_name(
            f"{self.report.path.stem}.{secrets.token_hex(10)}"
            ".contact.operation.json")
        operation = ContactSheetOperation(
            self.reviews, self.photos, destination, policy, journal,
            columns, rows)
        with self._lock:
            self.operations[operation.operation_id] = operation
        operation.start()
        return operation.public()

    def status(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            operation = self.operations.get(operation_id)
            if not operation:
                raise ActionError("unknown operation")
            return operation.public()

    def cancel(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            operation = self.operations.get(operation_id)
            if not operation:
                raise ActionError("unknown operation")
            operation.cancel()
            return operation.public()

    def rollback(self, operation_id: str, confirmation: str) -> dict[str, Any]:
        with self._lock:
            operation = self.operations.get(operation_id)
            if not isinstance(operation, Operation):
                raise ActionError("unknown move operation")
            if confirmation != f"ROLLBACK {operation_id}":
                raise ActionError(
                    f"confirmation must exactly equal: ROLLBACK {operation_id}")
        operation.rollback_move()
        return operation.public()

    def _finalize_project_filter(self, journal: dict[str, Any]) -> None:
        if self.project_path is None:
            return
        project = load_project(self.project_path)
        artifacts = project.get("artifacts", {})
        operation_id = str(journal.get("plan_id", ""))
        changes: dict[str, list[dict[str, Any]]] = {}
        for category, key in (
            ("rejected", "rejected"), ("raw_reserve", "raw_reserve")):
            current = list(artifacts.get(key, []) or [])
            current = [
                item for item in current
                if not isinstance(item, dict)
                or item.get("operation_id") != operation_id]
            current.extend({
                "operation_id": operation_id,
                "source_name": item.get("source_name"),
                "original_path": item.get("source"),
                "path": item.get("destination"),
                "operation": item.get("operation", "move"),
                "sha256": item.get("sha256"),
                "bytes": item.get("bytes"),
                "status": "reserved" if category == "raw_reserve" else "rejected",
                "created_at": item.get("completed_at", _now()),
            } for item in journal.get("items", [])
                if item.get("status") == "completed"
                and item.get("category") == category)
            changes[key] = current
        update_project(
            self.project_path, stage="organize", artifacts=changes)
        if changes["raw_reserve"] and self.raw_sources is not None:
            self.raw_sources.configure(self.project_layout["RAW Reserve"])
            register_file_artifact(
                self.project_path, "raw_source_map", self.raw_sources.path,
                stage="organize")
        register_file_artifact(
            self.project_path, "operations", Path(journal["journal_path"]),
            stage="organize", job_id=operation_id)

    def _rollback_project_filter(self, journal: dict[str, Any]) -> None:
        if self.project_path is None:
            return
        project = load_project(self.project_path)
        operation_id = str(journal.get("plan_id", ""))
        changes: dict[str, list[dict[str, Any]]] = {}
        for key in ("rejected", "raw_reserve"):
            current = list(project.get("artifacts", {}).get(key, []) or [])
            for item in current:
                if isinstance(item, dict) and item.get("operation_id") == operation_id:
                    item["status"] = "restored"
                    item["restored_at"] = _now()
            changes[key] = current
        update_project(
            self.project_path, stage="cull", artifacts=changes)
        if self.raw_sources is not None:
            self.raw_sources.configure(self.project_layout["RAW Reserve"])
            register_file_artifact(
                self.project_path, "raw_source_map", self.raw_sources.path,
                stage="cull")
        register_file_artifact(
            self.project_path, "operations", Path(journal["journal_path"]),
            stage="cull", job_id=operation_id)

    def cleanup_candidates(self) -> dict[str, Any]:
        if self.project_path is None:
            raise ActionError("this review is not attached to a Darkimiya project")
        return cleanup_candidates(self.project_path)

    def _finalize_system_cleanup(self, journal: dict[str, Any]) -> None:
        if self.project_path is None:
            return
        project = load_project(self.project_path)
        artifacts = project.get("artifacts", {})
        operation_id = str(journal.get("plan_id", ""))
        rejected = list(artifacts.get("rejected", []) or [])
        reserve = list(artifacts.get("raw_reserve", []) or [])
        by_current_path = {
            str(Path(str(item.get("path", ""))).expanduser().resolve()): item
            for item in rejected if isinstance(item, dict) and item.get("path")}
        for item in journal.get("items", []):
            if item.get("status") != "completed":
                continue
            source = str(Path(item["source"]).expanduser().resolve())
            record = by_current_path.get(source)
            if record is None:
                record = {
                    "source_name": item.get("source_name"),
                    "original_path": item.get("source"),
                    "created_at": item.get("completed_at", _now()),
                }
                rejected.append(record)
            record.update({
                "cleanup_operation_id": operation_id,
                "cleanup_source_path": item.get("source"),
                "path": item.get("destination"),
                "sha256": item.get("sha256"),
                "bytes": item.get("bytes"),
                "status": (
                    "retained_raw" if item.get("category") == "raw_reserve"
                    else "system_trash"),
                "cleaned_at": item.get("completed_at", _now()),
            })
            if item.get("category") == "raw_reserve":
                reserve.append({
                    "operation_id": operation_id,
                    "source_name": item.get("source_name"),
                    "original_path": item.get("source"),
                    "path": item.get("destination"),
                    "operation": "move", "sha256": item.get("sha256"),
                    "bytes": item.get("bytes"), "status": "reserved",
                    "created_at": item.get("completed_at", _now()),
                })
        update_project(
            self.project_path, stage="cleanup",
            artifacts={"rejected": rejected, "raw_reserve": reserve})
        if self.raw_sources is not None:
            self.raw_sources.configure(self.project_layout["RAW Reserve"])
            register_file_artifact(
                self.project_path, "raw_source_map", self.raw_sources.path,
                stage="cleanup")
        register_file_artifact(
            self.project_path, "operations", Path(journal["journal_path"]),
            stage="cleanup", job_id=operation_id)

    def _rollback_system_cleanup(self, journal: dict[str, Any]) -> None:
        if self.project_path is None:
            return
        project = load_project(self.project_path)
        operation_id = str(journal.get("plan_id", ""))
        rejected = list(project.get("artifacts", {}).get("rejected", []) or [])
        for item in rejected:
            if isinstance(item, dict) and item.get("cleanup_operation_id") == operation_id:
                item.update({
                    "path": item.get("cleanup_source_path")
                    or item.get("original_path"),
                    "status": "rejected",
                    "cleanup_rolled_back_at": _now(),
                })
        reserve = [
            item for item in list(
                project.get("artifacts", {}).get("raw_reserve", []) or [])
            if not isinstance(item, dict) or item.get("operation_id") != operation_id]
        update_project(
            self.project_path, stage="organize",
            artifacts={"rejected": rejected, "raw_reserve": reserve})
        if self.raw_sources is not None:
            self.raw_sources.configure(self.project_layout["RAW Reserve"])
            register_file_artifact(
                self.project_path, "raw_source_map", self.raw_sources.path,
                stage="organize")
        register_file_artifact(
            self.project_path, "operations", Path(journal["journal_path"]),
            stage="organize", job_id=operation_id)

    def resume_journal(
        self, journal_path: Path, confirmation: str
    ) -> dict[str, Any]:
        resolved = journal_path.expanduser().resolve()
        try:
            journal = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ActionError(f"cannot read operation journal: {exc}") from exc
        plan = journal.get("plan")
        if not isinstance(plan, dict):
            raise ActionError(
                "journal predates resumable embedded plans")
        plan_id = str(plan.get("plan_id", ""))
        if confirmation != f"RESUME {plan_id}":
            raise ActionError(
                f"confirmation must exactly equal: RESUME {plan_id}")
        complete_callback = (
            self._finalize_project_filter if plan.get("action") == "project_filter"
            else self._finalize_system_cleanup
            if plan.get("action") == "system_cleanup" else None)
        rollback_callback = (
            self._rollback_project_filter if plan.get("action") == "project_filter"
            else self._rollback_system_cleanup
            if plan.get("action") == "system_cleanup" else None)
        operation = Operation(
            plan, resolved, on_complete=complete_callback,
            on_rollback=rollback_callback)
        with self._lock:
            self.plans[plan_id] = plan
            self.operations[plan_id] = operation
        operation.start()
        return operation.public()
