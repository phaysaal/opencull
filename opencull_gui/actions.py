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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from scan import BITMAP_EXTENSIONS, RAW_EXTENSIONS, open_preview

from .photos import PhotoError, PhotoStore
from .report import ReportIndex
from .reviews import ReviewStore


PLAN_FORMAT = "opencull-operation-plan-v1"
JOURNAL_FORMAT = "opencull-operation-journal-v1"
PAIR_EXTENSIONS = RAW_EXTENSIONS | {".jpg", ".jpeg"}
POLICIES = {
    "human_only", "effective", "require_all", "ai_only", "modified_only",
}
LAYOUTS = {"opensull", "selected_by_cluster", "cluster_inspection"}
ACTIONS = {"copy", "move"}


class ActionError(ValueError):
    """An export or filesystem operation is unsafe or invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        return cluster / "selected" / relative
    category = "selected" if selected else "not-selected"
    return cluster / category / relative


def build_plan(
    report: ReportIndex,
    reviews: ReviewStore,
    photos: PhotoStore,
    destination: Path,
    policy: str,
    action: str,
    layout: str,
    preserve_relative: bool = False,
    include_companions: bool = False,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ActionError(f"unsupported action: {action}")
    if layout not in LAYOUTS:
        raise ActionError(f"unsupported layout: {layout}")
    destination = destination.expanduser().resolve()
    if destination == photos.root:
        raise ActionError("destination cannot be the photo root itself")
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
        target = _destination_for(
            destination, layout, cluster_number, name, selected,
            preserve_relative)
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
        names = (
            cluster["photos"] if layout == "cluster_inspection"
            else cluster["keepers"]
        )
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

    free_bytes = shutil.disk_usage(_nearest_existing(destination)).free
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
        "review_revision": reviews.public_state()["revision"],
        "created_at": _now(),
        "policy": policy,
        "action": action,
        "layout": layout,
        "destination": str(destination),
        "preserve_relative": bool(preserve_relative),
        "include_companions": bool(include_companions),
        "items": items,
        "summary": {
            "files": len(items),
            "selected_files": sum(item["selected"] for item in items),
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
    return {**plan_core, "plan_id": signature[:20], "signature": signature}


def default_journal_path(report: ReportIndex, plan: dict[str, Any]) -> Path:
    return report.path.with_name(
        f"{report.path.stem}.{plan['plan_id']}.operation.json")


class Operation:
    """One resumable verified copy/move operation."""

    def __init__(
        self,
        plan: dict[str, Any],
        journal_path: Path,
    ):
        self.plan = deepcopy(plan)
        self.journal_path = journal_path.expanduser().resolve()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
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
            return deepcopy(self.journal)

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
            if self.plan["action"] == "move":
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
            if self.plan["action"] != "move":
                raise ActionError("rollback is available only for move operations")
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
                if source.exists():
                    raise ActionError(
                        f"rollback source already exists: {source}")
                if not destination.is_file():
                    raise ActionError(
                        f"rollback destination is missing: {destination}")
                if _sha256(destination) != item.get("sha256"):
                    raise ActionError(
                        f"rollback hash changed: {destination}")
                source.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(destination, source)
                except OSError:
                    shutil.copy2(destination, source)
                    if _sha256(source) != item["sha256"]:
                        source.unlink(missing_ok=True)
                        raise ActionError(
                            f"rollback verification failed: {source}")
                    destination.unlink()
                with self._lock:
                    item["status"] = "rolled-back"
                    self.journal["rollback"]["completed"] += 1
                    self._save()
            with self._lock:
                self.journal["rollback"]["status"] = "completed"
                self.journal["status"] = "rolled-back"
                self._save()
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
            return deepcopy(self.journal)

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
    ):
        self.report = report
        self.reviews = reviews
        self.photos = photos
        self._lock = threading.RLock()
        self.plans: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, Operation | ContactSheetOperation] = {}

    def preflight(self, **options: Any) -> dict[str, Any]:
        destination_text = str(options.get("destination", "")).strip()
        if not destination_text:
            raise ActionError("destination is required")
        plan = build_plan(
            self.report,
            self.reviews,
            self.photos,
            destination=Path(destination_text),
            policy=str(options.get("policy", "human_only")),
            action=str(options.get("action", "copy")),
            layout=str(options.get("layout", "opensull")),
            preserve_relative=bool(options.get("preserve_relative", False)),
            include_companions=bool(options.get("include_companions", False)),
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
            expected = (
                f"MOVE {plan_id}" if plan["action"] == "move"
                else f"COPY {plan_id}"
            )
            if confirmation != expected:
                raise ActionError(f"confirmation must exactly equal: {expected}")
            operation = self.operations.get(plan_id)
            if operation is None:
                operation = Operation(
                    plan, default_journal_path(self.report, plan))
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
        operation = Operation(plan, resolved)
        with self._lock:
            self.plans[plan_id] = plan
            self.operations[plan_id] = operation
        operation.start()
        return operation.public()
