"""Validation and indexing for immutable OpenCull result reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ReportError(ValueError):
    """The supplied report cannot be reviewed safely."""


def _safe_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ReportError("photo names must be non-empty text")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ReportError(f"unsafe photo path in report: {name!r}")
    return path.as_posix()


@dataclass(frozen=True)
class ReportIndex:
    path: Path
    sha256: str
    data: dict[str, Any]
    cluster_by_id: dict[str, dict[str, Any]]
    decision_by_id: dict[str, dict[str, Any]]
    photo_names: tuple[str, ...]

    def public_payload(self, photos_root: Path) -> dict[str, Any]:
        missing = [
            name for name in self.photo_names
            if not (photos_root / Path(name)).is_file()
        ]
        selected = sum(
            len(decision.get("photos", []))
            for decision in self.decision_by_id.values()
        )
        return {
            "report": self.data,
            "report_path": str(self.path),
            "report_sha256": self.sha256,
            "photos_root": str(photos_root),
            "missing_photos": missing,
            "summary": {
                "clusters": len(self.cluster_by_id),
                "photos": len(self.photo_names),
                "selected": selected,
                "not_selected": len(self.photo_names) - selected,
                "warnings": len(self.data.get("warnings", [])),
                "fallback_clusters": sum(
                    bool(item.get("fallback"))
                    for item in self.decision_by_id.values()
                ),
                "empty_clusters": sum(
                    not item.get("photos")
                    for item in self.decision_by_id.values()
                ),
                "missing_photos": len(missing),
            },
        }


def load_report(path: Path) -> ReportIndex:
    path = path.expanduser().resolve()
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except OSError as exc:
        raise ReportError(f"cannot read report {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportError(f"invalid JSON report {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportError("report root must be a JSON object")
    if data.get("format") != "opencull-report-v2":
        raise ReportError(
            f"unsupported report format: {data.get('format')!r}")
    clusters = data.get("clusters")
    decisions = data.get("keep")
    if not isinstance(clusters, list) or not isinstance(decisions, list):
        raise ReportError("report must contain 'clusters' and 'keep' lists")
    if len(clusters) != len(decisions):
        raise ReportError("cluster and decision counts do not match")

    cluster_by_id: dict[str, dict[str, Any]] = {}
    decision_by_id: dict[str, dict[str, Any]] = {}
    photo_names: list[str] = []
    seen_photos: set[str] = set()
    for position, (cluster, decision) in enumerate(
            zip(clusters, decisions), start=1):
        if not isinstance(cluster, dict) or not isinstance(decision, dict):
            raise ReportError(f"cluster {position} is not an object")
        cluster_id = cluster.get("cluster_id")
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ReportError(f"cluster {position} has no valid ID")
        if cluster_id in cluster_by_id:
            raise ReportError(f"duplicate cluster ID: {cluster_id}")
        if decision.get("cluster_id") != cluster_id:
            raise ReportError(
                f"decision order mismatch at cluster {cluster_id}")
        photos = cluster.get("photos")
        keepers = decision.get("photos")
        if not isinstance(photos, list) or not isinstance(keepers, list):
            raise ReportError(
                f"cluster {cluster_id} must contain photo lists")
        safe_photos = [_safe_name(name) for name in photos]
        safe_keepers = [_safe_name(name) for name in keepers]
        if len(safe_photos) != len(set(safe_photos)):
            raise ReportError(f"duplicate photo in cluster {cluster_id}")
        if not set(safe_keepers).issubset(safe_photos):
            raise ReportError(
                f"unknown selected photo in cluster {cluster_id}")
        overlap = seen_photos.intersection(safe_photos)
        if overlap:
            raise ReportError(
                f"photo occurs in multiple clusters: {sorted(overlap)[0]}")
        seen_photos.update(safe_photos)
        cluster_copy = dict(cluster)
        cluster_copy["photos"] = safe_photos
        decision_copy = dict(decision)
        decision_copy["photos"] = safe_keepers
        cluster_by_id[cluster_id] = cluster_copy
        decision_by_id[cluster_id] = decision_copy
        photo_names.extend(safe_photos)

    normalized = dict(data)
    normalized["clusters"] = list(cluster_by_id.values())
    normalized["keep"] = list(decision_by_id.values())
    return ReportIndex(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        data=normalized,
        cluster_by_id=cluster_by_id,
        decision_by_id=decision_by_id,
        photo_names=tuple(photo_names),
    )
