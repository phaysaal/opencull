"""Atomic, report-bound human review state."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .report import ReportIndex


REVIEW_FORMAT = "opencull-review-v1"
EXPORT_FORMAT = "opencull-reviewed-result-v1"
PHOTO_FLAGS = {"unrated", "keep", "maybe", "reject"}
PHOTO_LABELS = {"", "red", "yellow", "green", "blue", "purple"}
MAX_HISTORY = 2000


class ReviewError(ValueError):
    """Review state is invalid, stale, or conflicts with another writer."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_review_path(report_path: Path) -> Path:
    return report_path.with_suffix(".review.json")


class ReviewStore:
    def __init__(
        self,
        path: Path,
        report: ReportIndex,
        photos_root: Path,
    ):
        self.path = path.expanduser().resolve()
        self.report = report
        self.photos_root = photos_root.expanduser().resolve()
        self._lock = threading.RLock()
        self.stale_reason = ""
        self._state = self._empty_state()
        self._load()

    def _empty_state(self) -> dict[str, Any]:
        timestamp = _now()
        return {
            "format": REVIEW_FORMAT,
            "report_sha256": self.report.sha256,
            "report_path": str(self.report.path),
            "photos_root": str(self.photos_root),
            "revision": 0,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_cluster_id": None,
            "clusters": {},
            "history": [],
        }

    @staticmethod
    def _validate_photo_annotation(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ReviewError("Photo annotation must be an object.")
        try:
            rating = int(value.get("rating", 0))
        except (TypeError, ValueError) as exc:
            raise ReviewError("Photo rating must be an integer.") from exc
        flag = value.get("flag", "unrated")
        label = value.get("label", "")
        if rating < 0 or rating > 5:
            raise ReviewError("Photo rating must be between 0 and 5.")
        if flag not in PHOTO_FLAGS:
            raise ReviewError(f"Unsupported photo flag: {flag!r}")
        if label not in PHOTO_LABELS:
            raise ReviewError(f"Unsupported color label: {label!r}")
        return {"rating": rating, "flag": flag, "label": label}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.stale_reason = f"Review file cannot be read: {exc}"
            return
        if not isinstance(data, dict) or data.get("format") != REVIEW_FORMAT:
            self.stale_reason = "Review file has an unsupported format."
            return
        if data.get("report_sha256") != self.report.sha256:
            self.stale_reason = (
                "Review file belongs to a different version of this report."
            )
            return
        try:
            self._state = self._validate_state(data)
        except ReviewError as exc:
            self.stale_reason = str(exc)

    def _validate_cluster(
        self, cluster_id: str, value: Any
    ) -> dict[str, Any]:
        cluster = self.report.cluster_by_id.get(cluster_id)
        if cluster is None or not isinstance(value, dict):
            raise ReviewError(f"Invalid reviewed cluster: {cluster_id}")
        keepers = value.get("keepers", [])
        note = value.get("note", "")
        reviewed = value.get("reviewed", False)
        annotations = value.get("photo_annotations", {})
        if not isinstance(keepers, list):
            raise ReviewError(f"Human keepers must be a list: {cluster_id}")
        if not isinstance(note, str) or len(note) > 5000:
            raise ReviewError(f"Invalid human note: {cluster_id}")
        if not isinstance(reviewed, bool):
            raise ReviewError(f"Invalid review status: {cluster_id}")
        if not isinstance(annotations, dict):
            raise ReviewError(f"Invalid photo annotations: {cluster_id}")
        allowed = set(cluster["photos"])
        if (
            len(keepers) != len(set(keepers))
            or any(not isinstance(name, str) or name not in allowed
                   for name in keepers)
        ):
            raise ReviewError(f"Unknown human keeper: {cluster_id}")
        if any(name not in allowed for name in annotations):
            raise ReviewError(f"Unknown annotated photo: {cluster_id}")
        return {
            "keepers": keepers,
            "note": note,
            "reviewed": reviewed,
            "photo_annotations": {
                name: self._validate_photo_annotation(annotation)
                for name, annotation in annotations.items()
            },
            "updated_at": str(value.get("updated_at") or _now()),
        }

    def _validate_state(self, data: dict[str, Any]) -> dict[str, Any]:
        clusters = data.get("clusters", {})
        if not isinstance(clusters, dict):
            raise ReviewError("Review clusters must be an object.")
        valid_clusters = {
            cluster_id: self._validate_cluster(cluster_id, value)
            for cluster_id, value in clusters.items()
        }
        last = data.get("last_cluster_id")
        if last is not None and last not in self.report.cluster_by_id:
            raise ReviewError("Review position names an unknown cluster.")
        try:
            revision = max(0, int(data.get("revision", 0)))
        except (TypeError, ValueError) as exc:
            raise ReviewError("Review revision is invalid.") from exc
        history = data.get("history", [])
        if not isinstance(history, list):
            raise ReviewError("Review history must be a list.")
        valid_history = []
        for item in history[-MAX_HISTORY:]:
            if not isinstance(item, dict):
                raise ReviewError("Review history entry must be an object.")
            cluster_id = item.get("cluster_id")
            if cluster_id not in self.report.cluster_by_id:
                raise ReviewError("Review history names an unknown cluster.")
            valid_history.append({
                "revision": max(0, int(item.get("revision", 0))),
                "cluster_id": cluster_id,
                "action": str(item.get("action", "review")),
                "timestamp": str(item.get("timestamp") or _now()),
                "before": (
                    self._validate_cluster(cluster_id, item["before"])
                    if isinstance(item.get("before"), dict) else None
                ),
                "after": (
                    self._validate_cluster(cluster_id, item["after"])
                    if isinstance(item.get("after"), dict) else None
                ),
            })
        return {
            "format": REVIEW_FORMAT,
            "report_sha256": self.report.sha256,
            "report_path": str(self.report.path),
            "photos_root": str(self.photos_root),
            "revision": revision,
            "created_at": str(data.get("created_at") or _now()),
            "updated_at": str(data.get("updated_at") or _now()),
            "last_cluster_id": last,
            "clusters": valid_clusters,
            "history": valid_history,
        }

    def public_state(self) -> dict[str, Any]:
        with self._lock:
            state = deepcopy(self._state)
            reviewed = sum(
                bool(item.get("reviewed"))
                for item in state["clusters"].values()
            )
            modified = sum(
                bool(item.get("reviewed"))
                and set(item.get("keepers", []))
                != set(self.report.decision_by_id[cluster_id].get("photos", []))
                for cluster_id, item in state["clusters"].items()
            )
            state["status"] = {
                "compatible": not self.stale_reason,
                "stale_reason": self.stale_reason,
                "path": str(self.path),
                "exists": self.path.exists(),
                "reviewed_clusters": reviewed,
                "modified_clusters": modified,
                "total_clusters": len(self.report.cluster_by_id),
                **self._session_statistics(state),
            }
            return state

    def _session_statistics(self, state: dict[str, Any]) -> dict[str, Any]:
        history = state.get("history", [])
        timestamps = []
        for item in history:
            try:
                timestamps.append(datetime.fromisoformat(item["timestamp"]))
            except (TypeError, ValueError):
                continue
        elapsed = (
            max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())
            if len(timestamps) > 1 else 0.0
        )
        reviewed = sum(
            bool(item.get("reviewed"))
            for item in state["clusters"].values()
        )
        total = len(self.report.cluster_by_id)
        seconds_per_cluster = elapsed / max(1, reviewed) if elapsed else 0.0
        return {
            "history_events": len(history),
            "session_elapsed_seconds": round(elapsed, 1),
            "seconds_per_reviewed_cluster": round(seconds_per_cluster, 1),
            "estimated_remaining_seconds": round(
                seconds_per_cluster * max(0, total - reviewed), 1),
        }

    def _require_writable(self, expected_revision: Any) -> None:
        if self.stale_reason:
            raise ReviewError(self.stale_reason)
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise ReviewError("Missing or invalid expected revision.") from exc
        if expected != self._state["revision"]:
            raise ReviewError(
                "Review changed in another browser session; reload before saving."
            )

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(
                self.path.suffix + ".bak"))
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(self._state, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def update_cluster(
        self,
        cluster_id: str,
        keepers: Any,
        note: Any,
        reviewed: Any,
        expected_revision: Any,
        photo_annotations: Any = None,
        action: str = "review",
    ) -> dict[str, Any]:
        with self._lock:
            self._require_writable(expected_revision)
            before = deepcopy(self._state["clusters"].get(cluster_id))
            value = self._validate_cluster(cluster_id, {
                "keepers": keepers,
                "note": note,
                "reviewed": reviewed,
                "photo_annotations": (
                    photo_annotations if photo_annotations is not None
                    else (before or {}).get("photo_annotations", {})
                ),
                "updated_at": _now(),
            })
            self._state["clusters"][cluster_id] = value
            self._state["last_cluster_id"] = cluster_id
            self._state["revision"] += 1
            self._state["updated_at"] = _now()
            self._state["history"].append({
                "revision": self._state["revision"],
                "cluster_id": cluster_id,
                "action": str(action)[:100],
                "timestamp": self._state["updated_at"],
                "before": before,
                "after": deepcopy(value),
            })
            self._state["history"] = self._state["history"][-MAX_HISTORY:]
            self._write()
            return self.public_state()

    def undo(self, expected_revision: Any) -> dict[str, Any]:
        with self._lock:
            self._require_writable(expected_revision)
            if not self._state["history"]:
                raise ReviewError("There is no review change to undo.")
            event = self._state["history"].pop()
            cluster_id = event["cluster_id"]
            before = event.get("before")
            if before is None:
                self._state["clusters"].pop(cluster_id, None)
            else:
                self._state["clusters"][cluster_id] = self._validate_cluster(
                    cluster_id, before)
            self._state["last_cluster_id"] = cluster_id
            self._state["revision"] += 1
            self._state["updated_at"] = _now()
            self._write()
            return self.public_state()

    def update_position(
        self, cluster_id: str, expected_revision: Any
    ) -> dict[str, Any]:
        with self._lock:
            self._require_writable(expected_revision)
            if cluster_id not in self.report.cluster_by_id:
                raise ReviewError(f"Unknown cluster: {cluster_id}")
            self._state["last_cluster_id"] = cluster_id
            self._state["revision"] += 1
            self._state["updated_at"] = _now()
            self._write()
            return self.public_state()

    def export(self) -> dict[str, Any]:
        with self._lock:
            clusters = []
            for cluster_id, cluster in self.report.cluster_by_id.items():
                ai = self.report.decision_by_id[cluster_id]
                human = self._state["clusters"].get(cluster_id)
                human_reviewed = bool(human and human.get("reviewed"))
                keepers = (
                    human["keepers"] if human_reviewed else ai.get("photos", [])
                )
                clusters.append({
                    "cluster_id": cluster_id,
                    "photos": cluster["photos"],
                    "keepers": keepers,
                    "decision_source": (
                        "human" if human_reviewed else "ai_unreviewed"
                    ),
                    "human_note": human.get("note", "") if human else "",
                    "photo_annotations": (
                        deepcopy(human.get("photo_annotations", {}))
                        if human else {}
                    ),
                    "ai_keepers": ai.get("photos", []),
                    "human_reviewed": human_reviewed,
                })
            return {
                "format": EXPORT_FORMAT,
                "source_report_sha256": self.report.sha256,
                "source_report": str(self.report.path),
                "review_file": str(self.path),
                "exported_at": _now(),
                "review_revision": self._state["revision"],
                "reviewed_clusters": sum(
                    item["human_reviewed"] for item in clusters),
                "total_clusters": len(clusters),
                "clusters": clusters,
                "history": deepcopy(self._state["history"]),
                "selected_photos": [
                    name for cluster in clusters
                    for name in cluster["keepers"]
                ],
                "notice": (
                    "Human-reviewed clusters use human decisions; unreviewed "
                    "clusters retain clearly labelled AI recommendations."
                ),
            }
