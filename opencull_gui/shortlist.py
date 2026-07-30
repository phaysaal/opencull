"""Validation and indexing for immutable professional-shortlist artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scan import RAW_EXTENSIONS

from .assets import AssetIndex, index_asset_families
from .report import ReportIndex


SHORTLIST_FORMAT = "opencull-professional-shortlist-v1"
TIERS = {"exceptional", "strong", "promising", "ordinary", "reject"}
CANDIDATE_POLICIES = {"human_only", "effective", "ai_only", "all"}
ASSESSMENT_FIELDS = (
    "composition",
    "angle_and_perspective",
    "subject_presentation",
    "pose_and_expression",
    "moment_and_emotion",
    "light_and_tonality",
    "surroundings",
    "irrecoverable_defects",
    "raw_editing_opportunities",
    "distinctiveness",
)


class ShortlistError(ValueError):
    """A professional shortlist is invalid or belongs to another report."""


def _safe_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShortlistError("shortlist filenames must be non-empty text")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ShortlistError(f"unsafe shortlist path: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class ShortlistIndex:
    path: Path
    data: dict[str, Any]
    entries: tuple[dict[str, Any], ...]
    entry_by_photo: dict[str, dict[str, Any]]
    assets: AssetIndex


def load_shortlist(
    path: Path,
    report: ReportIndex,
    photos_root: Path,
) -> ShortlistIndex:
    resolved = path.expanduser().resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ShortlistError(f"cannot read shortlist {resolved}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ShortlistError(f"invalid shortlist JSON {resolved}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != SHORTLIST_FORMAT:
        raise ShortlistError("unsupported professional-shortlist format")
    if data.get("source_report_sha256") != report.sha256:
        raise ShortlistError("shortlist belongs to a different culling report")
    if data.get("candidate_policy") not in CANDIDATE_POLICIES:
        raise ShortlistError("shortlist has an unsupported candidate policy")
    revision = data.get("source_review_revision")
    if (
        revision is not None
        and (not isinstance(revision, int) or revision < 0)
    ):
        raise ShortlistError("shortlist has an invalid review revision")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ShortlistError("shortlist entries must be a list")

    assets = index_asset_families(photos_root)
    normalized = []
    seen_photos: set[str] = set()
    seen_ranks: set[int] = set()
    for position, value in enumerate(raw_entries, start=1):
        if not isinstance(value, dict):
            raise ShortlistError(f"shortlist entry {position} is not an object")
        photo = _safe_name(value.get("photo"))
        if photo not in report.photo_names:
            raise ShortlistError(f"unknown shortlist photo: {photo}")
        if photo in seen_photos:
            raise ShortlistError(f"duplicate shortlist photo: {photo}")
        cluster_id = value.get("cluster_id")
        cluster = report.cluster_by_id.get(cluster_id)
        if not cluster or photo not in cluster["photos"]:
            raise ShortlistError(
                f"photo {photo} does not belong to cluster {cluster_id}")
        try:
            rank = int(value.get("rank"))
            score = float(value.get("score"))
            confidence = float(value.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ShortlistError(
                f"entry {position} has invalid numeric evidence") from exc
        if rank < 1 or rank in seen_ranks:
            raise ShortlistError(f"invalid or duplicate shortlist rank: {rank}")
        tier = str(value.get("tier", "")).strip().lower()
        if tier not in TIERS:
            raise ShortlistError(f"unsupported shortlist tier: {tier!r}")
        if not 0 <= score <= 100 or not 0 <= confidence <= 1:
            raise ShortlistError(
                f"entry {position} score or confidence is out of range")
        raw_values = value.get("raw_files", [])
        if not isinstance(raw_values, list):
            raise ShortlistError(
                f"entry {position} RAW companions must be a list")
        raw_files = tuple(_safe_name(name) for name in raw_values)
        family = assets.family_for(photo)
        allowed_raws = set(family.raw_files if family else ())
        if any(
            name not in allowed_raws
            or Path(name).suffix.lower() not in RAW_EXTENSIONS
            for name in raw_files
        ):
            raise ShortlistError(
                f"entry {position} contains an unrelated RAW companion")
        if len(raw_files) != len(set(raw_files)):
            raise ShortlistError(
                f"entry {position} repeats a RAW companion")
        rationale = value.get("rationale")
        assessment = value.get("assessment")
        warnings = value.get("warnings", [])
        if not isinstance(rationale, str) or not rationale.strip():
            raise ShortlistError(
                f"entry {position} has no photographic rationale")
        if not isinstance(assessment, dict) or any(
            not isinstance(assessment.get(field), str)
            or not assessment[field].strip()
            for field in ASSESSMENT_FIELDS
        ):
            raise ShortlistError(
                f"entry {position} has an incomplete photographic assessment")
        if not isinstance(warnings, list) or any(
            not isinstance(warning, str) for warning in warnings
        ):
            raise ShortlistError(f"entry {position} warnings must be text")
        normalized.append({
            **value,
            "rank": rank,
            "photo": photo,
            "raw_files": list(raw_files),
            "cluster_id": cluster_id,
            "tier": tier,
            "score": score,
            "confidence": confidence,
        })
        seen_photos.add(photo)
        seen_ranks.add(rank)
    normalized.sort(key=lambda item: item["rank"])
    if [entry["rank"] for entry in normalized] != list(
            range(1, len(normalized) + 1)):
        raise ShortlistError("shortlist ranks must be contiguous from 1")
    normalized_data = dict(data)
    normalized_data["entries"] = normalized
    return ShortlistIndex(
        resolved,
        normalized_data,
        tuple(normalized),
        {entry["photo"]: entry for entry in normalized},
        assets,
    )
