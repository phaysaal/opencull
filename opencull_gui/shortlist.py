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
TIER_STARS = {
    "exceptional": 5, "strong": 4, "promising": 3, "ordinary": 2,
    "reject": 1,
}


def tier_rank(tier) -> int:
    """How high a tier stands, five for the best and one for the worst."""
    return TIER_STARS.get(str(tier or "").strip().lower(), 0)


def score_disagreements(entries) -> dict[str, str]:
    """Frames whose score contradicts the verdict beside it.

    A model asked for both a named tier and a number rarely keeps them
    in step: live shoots show a `strong` frame scoring 50 while several
    `ordinary` ones reach 61. Neither reading is wrong on its own, but
    where they disagree the photographer deserves to know before
    trusting either.

    The measure is disagreement of order, not of value, because scores
    compress differently in every shoot and absolute thresholds would
    be invented. Each frame gets two positions -- where its tier puts
    it, and where its score puts it -- and a frame is flagged when
    those positions are further apart than a third of the shoot. That
    names the genuine outlier (one highly rated frame with a middling
    number) instead of the crowd it sits under.
    """
    rows = []
    for entry in entries:
        photo = str(entry.get("photo", ""))
        rank = tier_rank(entry.get("tier"))
        try:
            score = float(entry.get("score", 0))
        except (TypeError, ValueError):
            continue
        if photo and rank:
            rows.append((photo, str(entry.get("tier", "")).strip().lower(),
                         rank, score))
    if len(rows) < 4:
        return {}
    by_verdict = [row[0] for row in
                  sorted(rows, key=lambda r: (-r[2], -r[3], r[0]))]
    by_score = [row[0] for row in
                sorted(rows, key=lambda r: (-r[3], -r[2], r[0]))]
    verdict_place = {photo: index for index, photo in enumerate(by_verdict)}
    score_place = {photo: index for index, photo in enumerate(by_score)}
    limit = max(3, len(rows) // 3)
    flagged: dict[str, str] = {}
    for photo, tier, _rank, score in rows:
        gap = verdict_place[photo] - score_place[photo]
        if abs(gap) <= limit:
            continue
        if gap < 0:
            flagged[photo] = (
                f"called {tier}, but its score of {score:.0f} places it "
                f"{abs(gap)} frames lower than its verdict does")
        else:
            flagged[photo] = (
                f"called {tier}, but its score of {score:.0f} places it "
                f"{gap} frames higher than its verdict does")
    return flagged


def tier_stars(tier) -> str:
    """A tier as stars, or nothing for a tier that is not one."""
    filled = TIER_STARS.get(str(tier or "").strip().lower(), 0)
    return "★" * filled + "☆" * (5 - filled) if filled else ""
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


# What a frame is worth before anybody has said otherwise. A locally written
# shortlist has to put something here, and the middle of the scale is the only
# honest placeholder: it asserts nothing.
UNRATED_TIER = "promising"

# The loader refuses an entry with no rationale, and it is right to: every
# row in a shortlist has to account for itself. So the row says what it
# actually is instead of asserting something about the photograph.
UNRATED_RATIONALE = "Not assessed. Laid out for rating by hand."

# Every axis has to carry text too. Rather than weaken the validation that
# protects a real shortlist, each axis says the same true thing, and the
# interface hides them when it knows no model spoke.
UNRATED_AXIS = "Not assessed."


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


def write_manual_shortlist(
    report: ReportIndex, photos: list[str], destination: Path,
    stance: str = "",
) -> Path:
    """Write a shortlist nobody was paid to produce.

    A photographer who wants to rate their own work should not have to buy a
    model's opinion first in order to disagree with it. This lays out the
    same shortlist the assessment would produce -- the same frames, in the
    same order, in the same format -- with every judgement left blank and
    every tier at the middle of the scale.

    Nothing here asserts anything about a photograph. The frames arrive
    unrated, in the order the cull left them, and every rating on top of this
    is the photographer's own.
    """
    destination = Path(destination).expanduser()
    cluster_of = {
        name: cluster_id
        for cluster_id, cluster in report.cluster_by_id.items()
        for name in cluster.get("photos", [])
    }
    entries = []
    for rank, photo in enumerate(photos, start=1):
        cluster_id = cluster_of.get(photo)
        if cluster_id is None:
            continue
        entries.append({
            "rank": rank,
            "photo": photo,
            "cluster_id": cluster_id,
            "tier": UNRATED_TIER,
            "score": 0,
            "confidence": 0,
            "rationale": UNRATED_RATIONALE,
            "raw_files": [],
            "assessment": dict.fromkeys(ASSESSMENT_FIELDS, UNRATED_AXIS),
            "warnings": [],
        })
    value = {
        "format": SHORTLIST_FORMAT,
        "source_report_sha256": report.sha256,
        "candidate_policy": "effective",
        "candidate_signature": "manual",
        # Read by the interface so it never presents a blank row as a
        # judgement, and never shows a model's confidence that no model gave.
        "rated_by": "photographer",
        "stance": str(stance or ""),
        "entries": entries,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)
    return destination


def rated_by_hand(shortlist: ShortlistIndex) -> bool:
    """Whether this shortlist was laid out locally rather than assessed."""
    return str(getattr(shortlist, "data", {}).get("rated_by", "")) == "photographer"
