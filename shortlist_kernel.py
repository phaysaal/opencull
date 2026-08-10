"""Audited deterministic kernel for OpenCull's professional RAW shortlist."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from opencull_gui.assets import index_asset_families
from scan import (  # noqa: F401
    BITMAP_EXTENSIONS,
    RAW_EXTENSIONS,
    measure,
    visible_photograph,
)

SHORTLIST_CHECKPOINT_FORMAT = "opencull-professional-checkpoint-v1"
ASSESSMENT_FIELDS = (
    "composition", "angle_and_perspective", "subject_presentation",
    "pose_and_expression", "moment_and_emotion", "light_and_tonality",
    "surroundings", "irrecoverable_defects", "raw_editing_opportunities",
    "distinctiveness",
)
TIERS = ("exceptional", "strong", "promising", "ordinary", "reject")


def _load_json(path: str) -> dict[str, Any]:
    try:
        value = json.loads(
            Path(str(path)).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def _safe_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    safe = []
    for value in values:
        if not isinstance(value, str):
            continue
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            continue
        safe.append(path.as_posix())
    return safe


def _chosen_names(
    report: dict[str, Any], review: dict[str, Any] | None, policy: str
) -> tuple[list[tuple[str, str]], int | None]:
    clusters = report.get("clusters", [])
    decisions = report.get("keep", [])
    review_clusters = (
        review.get("clusters", {}) if isinstance(review, dict) else {})
    chosen: list[tuple[str, str]] = []
    for cluster, decision in zip(clusters, decisions):
        cluster_id = str(cluster.get("cluster_id", ""))
        all_names = _safe_names(cluster.get("photos"))
        ai_names = _safe_names(decision.get("photos"))
        human = review_clusters.get(cluster_id, {})
        reviewed = isinstance(human, dict) and human.get("reviewed") is True
        human_names = _safe_names(
            human.get("keepers")) if reviewed else []
        if policy == "all":
            names = all_names
        elif policy == "ai_only":
            names = ai_names
        elif policy == "human_only":
            names = human_names
        else:
            names = human_names if reviewed else ai_names
        allowed = set(all_names)
        chosen.extend(
            (cluster_id, name) for name in names if name in allowed)
    revision = (
        review.get("revision") if isinstance(review, dict)
        and isinstance(review.get("revision"), int) else None)
    return chosen, revision


def chosen_photographs(
    report: dict[str, Any], review: dict[str, Any] | None,
    policy: str = "effective",
) -> list[str]:
    """The photographs an assessment under this policy would read.

    Public because an interface has to be able to show what a run is about
    to cost before it starts, and the only honest answer is the one this
    module already computes for the run itself. A second copy of the rule
    in the window would be a copy that drifts.
    """
    chosen, _revision = _chosen_names(report, review, policy)
    return [name for _cluster, name in chosen]


def build_professional_candidates(
    report_path: str,
    photos: str,
    review_path: str = "",
    policy: str = "effective",
    minimum_dimension: float = 640,
) -> str:
    """Select keepers, pair RAW assets, and conservatively screen locally."""
    policy = str(policy).strip().lower()
    if policy not in {"human_only", "effective", "ai_only", "all"}:
        raise ValueError(f"unsupported candidate policy: {policy}")
    report_file = Path(str(report_path)).expanduser().resolve()
    report = _load_json(str(report_file))
    if report.get("format") != "opencull-report-v2":
        raise ValueError("unsupported culling report")
    report_sha = hashlib.sha256(report_file.read_bytes()).hexdigest()
    review = None
    chosen_review = str(review_path).strip()
    if chosen_review:
        review = _load_json(chosen_review)
        if (
            review.get("format") != "opencull-review-v1"
            or review.get("report_sha256") != report_sha
        ):
            raise ValueError("review file does not match the culling report")
    root = Path(str(photos)).expanduser().resolve()
    assets = index_asset_families(root)
    chosen, revision = _chosen_names(report, review, policy)
    selected_names = {name for _, name in chosen}
    cluster_for = {name: cluster for cluster, name in chosen}

    # One visual judgment per physical capture family. Prefer a selected bitmap
    # over its RAW preview; never promote an unselected JPEG into the candidate
    # set merely because it shares a stem with a selected RAW.
    family_choices: dict[str, str] = {}
    for _, name in chosen:
        family = assets.family_for(name)
        identity = family.asset_id if family else f"unpaired:{name}"
        current = family_choices.get(identity)
        if current is None:
            family_choices[identity] = name
        elif (
            Path(name).suffix.lower() in BITMAP_EXTENSIONS
            and Path(current).suffix.lower() in RAW_EXTENSIONS
        ):
            family_choices[identity] = name

    candidates = []
    excluded = []
    for name in family_choices.values():
        source = root / Path(name)
        family = assets.family_for(name)
        if not source.is_file():
            excluded.append({
                "photo": name, "reason": "selected source file is missing"})
            continue
        try:
            measured = measure(source, root).manifest_record()
        except Exception as exc:
            excluded.append({
                "photo": name,
                "reason": f"preview could not be decoded: {type(exc).__name__}",
            })
            continue
        if min(measured["width"], measured["height"]) < int(minimum_dimension):
            excluded.append({
                "photo": name,
                "reason": "image dimensions are below the configured minimum",
            })
            continue
        warnings = []
        if measured["sharpness"] < 18:
            warnings.append("local measurement indicates severe softness or blur")
        if measured["clipping"] > 25:
            warnings.append("local measurement indicates extensive clipping")
        if measured["exposure"] < 20:
            warnings.append("local measurement indicates extreme exposure")
        raw_files = list(family.raw_files if family else ())
        candidates.append({
            **measured,
            "photo": name,
            "name": name,
            "cluster_id": cluster_for[name],
            "asset_id": family.asset_id if family else "",
            "raw_files": raw_files,
            "asset_ambiguous": bool(family and family.ambiguous),
            "asset_warning": family.ambiguity if family else "",
            "local_warnings": warnings,
            "selected_companions": sorted(
                selected_names.intersection(family.files)
                if family else {name}),
        })
    candidates.sort(key=lambda item: (
        item["cluster_id"], item["captured"], item["photo"].casefold()))
    signature = {
        "source_report_sha256": report_sha,
        "source_review_revision": revision,
        "candidate_policy": policy,
        "photos_root": str(root),
        "candidate_ids": [
            (item["photo"], item["sha256_prefix"]) for item in candidates],
    }
    return json.dumps({
        "format": "opencull-professional-candidates-v1",
        **signature,
        "signature": hashlib.sha256(json.dumps(
            signature, sort_keys=True).encode()).hexdigest(),
        "candidates": candidates,
        "excluded": excluded,
        "notice": "Local screening was read-only and made no artistic rejection.",
    }, indent=2, sort_keys=True)


def parse_professional_candidates(bundle: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(str(bundle))
    except (TypeError, json.JSONDecodeError):
        return []
    candidates = data.get("candidates", []) if isinstance(data, dict) else []
    return candidates if isinstance(candidates, list) else []


def professional_candidate_path(
    bundle: str, candidate: dict[str, Any]
) -> str:
    data = json.loads(str(bundle))
    return str(visible_photograph(
        Path(data["photos_root"]), str(candidate["photo"])))


def professional_assessment_prompt(
    candidate: dict[str, Any], profile: str
) -> str:
    evidence = {
        key: candidate.get(key) for key in (
            "photo", "width", "height", "technical_score", "sharpness",
            "exposure", "contrast", "clipping", "composition_proxy",
            "local_warnings", "raw_files", "asset_warning")
    }
    return "\n".join([
        "Act as a critical professional photo editor assessing one already-culled frame.",
        f"Editing profile: {str(profile).strip().lower() or 'family'}.",
        "Judge whether this frame deserves professional RAW editing. This is "
        "stricter than deciding whether a family memory should be kept.",
        "Prioritize photographic angle, composition, subject presentation, pose, "
        "expression, moment, light, surroundings, and visual impact.",
        "Separate irreversible defects (missed subject focus, destructive motion "
        "blur, bad timing, awkward pose/expression, blocked subject) from RAW-"
        "recoverable exposure, white balance, highlight, shadow, noise, and crop.",
        "A local blur warning is fallible: inspect whether softness, shallow depth "
        "of field, or motion is intentional before treating it as a defect.",
        "Use tiers exceptional, strong, promising, ordinary, or reject. Apply an "
        "absolute professional bar; do not award a high tier merely because this "
        "was the best frame in its cluster.",
        "Score from 0 to 100, state uncertainty, and cite only visible evidence.",
        "LOCAL SECONDARY EVIDENCE:",
        json.dumps(evidence, sort_keys=True),
    ])


def _field(record: Any, name: str, default: Any = "") -> Any:
    return record.get(name, default) if isinstance(record, dict) else default


def valid_professional_assessment(record: Any) -> bool:
    if not isinstance(record, dict) or any(
        not str(record.get(field, "")).strip()
        for field in ASSESSMENT_FIELDS
    ):
        return False
    if str(record.get("tier", "")).strip().lower() not in TIERS:
        return False
    if not str(record.get("rationale", "")).strip():
        return False
    try:
        score = float(record.get("score"))
        confidence = float(record.get("confidence"))
    except (TypeError, ValueError):
        return False
    return 0 <= score <= 100 and 0 <= confidence <= 1


def normalize_professional_assessment(record: Any) -> dict[str, Any]:
    source = record if isinstance(record, dict) else {}
    normalized = {}
    for field in ASSESSMENT_FIELDS:
        normalized[field] = str(source.get(field, "")).strip() or (
            f"Uncertain: {field.replace('_', ' ')} was not reliably assessed.")
    tier = str(source.get("tier", "")).strip().lower()
    normalized["tier"] = tier if tier in TIERS else "promising"
    try:
        normalized["score"] = max(
            0.0, min(100.0, float(source.get("score", 50))))
        normalized["confidence"] = max(
            0.0, min(1.0, float(source.get("confidence", 0))))
    except (TypeError, ValueError):
        normalized["score"], normalized["confidence"] = 50.0, 0.0
    normalized["rationale"] = str(source.get("rationale", "")).strip() or (
        "The model response was incomplete; retain for cautious human review.")
    return normalized


def professional_assessment_json(
    record: dict[str, Any], candidate: dict[str, Any]
) -> str:
    return json.dumps({
        "photo": candidate["photo"],
        "cluster_id": candidate["cluster_id"],
        "raw_files": candidate.get("raw_files", []),
        "local_warnings": candidate.get("local_warnings", []),
        "asset_warning": candidate.get("asset_warning", ""),
        **{field: _field(record, field) for field in ASSESSMENT_FIELDS},
        "tier": _field(record, "tier"),
        "score": _field(record, "score"),
        "confidence": _field(record, "confidence"),
        "rationale": _field(record, "rationale"),
    }, sort_keys=True)


def professional_batches(
    candidates: list[dict[str, Any]], limit: float = 8
) -> list[list[dict[str, Any]]]:
    size = max(1, min(8, int(limit)))
    return [
        candidates[index:index + size]
        for index in range(0, len(candidates), size)
    ]


def assessments_for_candidates(
    assessments: list[str], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    names = {candidate.get("photo") for candidate in candidates}
    parsed = []
    for value in assessments:
        try:
            item = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("photo") in names:
            parsed.append(item)
    return parsed


def professional_calibration_prompt(
    candidates: list[dict[str, Any]], assessments: list[dict[str, Any]],
    profile: str,
) -> str:
    names = [candidate["photo"] for candidate in candidates]
    return "\n".join([
        "Act as the comparative senior photo editor. Calibrate these already-"
        "selected photographs against an absolute professional-editing standard.",
        f"Profile: {str(profile).strip().lower() or 'family'}.",
        "Reinspect the attached images. Return every filename exactly once in "
        "`ranking`, best first, as comma-separated text.",
        "Also place every filename exactly once into one comma-separated tier "
        "field: exceptional, strong, promising, ordinary, or reject.",
        "Do not impose a quota. Similar frames may both rank highly only when "
        "their pose, expression, moment, or visual result is meaningfully distinct.",
        "Correct over-generous or over-harsh first-pass assessments and explain "
        "the decisive comparisons.",
        "FILES: " + ", ".join(names),
        "FIRST-PASS ASSESSMENTS:",
        json.dumps(assessments, sort_keys=True),
    ])


def _csv_names(value: Any) -> list[str]:
    return [
        part.strip() for part in str(value or "").split(",") if part.strip()]


def valid_professional_calibration(
    record: Any, candidates: list[dict[str, Any]]
) -> bool:
    if not isinstance(record, dict):
        return False
    allowed = [candidate["photo"] for candidate in candidates]
    ranking = _csv_names(record.get("ranking"))
    tiered = [
        name for tier in TIERS for name in _csv_names(record.get(tier))]
    try:
        confidence = float(record.get("confidence"))
    except (TypeError, ValueError):
        return False
    return (
        len(ranking) == len(set(ranking))
        and set(ranking) == set(allowed)
        and len(tiered) == len(set(tiered))
        and set(tiered) == set(allowed)
        and bool(str(record.get("rationale", "")).strip())
        and 0 <= confidence <= 1
    )


def professional_calibration_json(
    record: dict[str, Any], candidates: list[dict[str, Any]]
) -> str:
    return json.dumps({
        "ranking": _csv_names(record.get("ranking")),
        "tiers": {
            tier: _csv_names(record.get(tier)) for tier in TIERS},
        "rationale": str(record.get("rationale", "")).strip(),
        "confidence": float(record.get("confidence", 0)),
        "candidate_count": len(candidates),
    }, sort_keys=True)


def professional_checkpoint_path(output: str, checkpoint: str = "") -> str:
    chosen = str(checkpoint).strip()
    return chosen if chosen else f"{output}.checkpoint.json"


def _valid_saved_assessment(
    value: Any, candidate: dict[str, Any]
) -> bool:
    return (
        isinstance(value, dict)
        and value.get("photo") == candidate.get("photo")
        and valid_professional_assessment(value)
    )


def build_professional_checkpoint(
    bundle: str,
    candidates: list[dict[str, Any]],
    assessments: list[str],
    profile: str,
    completed: bool = False,
) -> str:
    data = json.loads(str(bundle))
    parsed = []
    for candidate, value in zip(candidates, assessments):
        try:
            assessment = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            break
        if not _valid_saved_assessment(assessment, candidate):
            break
        parsed.append(assessment)
    signature = {
        "candidate_signature": data.get("signature"),
        "candidate_count": len(candidates),
        "profile": str(profile).strip().lower() or "family",
    }
    return json.dumps({
        "format": SHORTLIST_CHECKPOINT_FORMAT,
        "signature": signature,
        "completed": bool(completed) and len(parsed) == len(candidates),
        "assessments": parsed,
    }, indent=2, sort_keys=True)


def load_professional_checkpoint(
    path: str,
    bundle: str,
    candidates: list[dict[str, Any]],
    profile: str,
    enabled: bool = True,
) -> str:
    empty = build_professional_checkpoint(
        bundle, candidates, [], profile, False)
    if not enabled:
        return empty
    try:
        data = _load_json(path)
        bundle_data = json.loads(str(bundle))
    except (ValueError, TypeError, json.JSONDecodeError):
        return empty
    expected = {
        "candidate_signature": bundle_data.get("signature"),
        "candidate_count": len(candidates),
        "profile": str(profile).strip().lower() or "family",
    }
    if (
        data.get("format") != SHORTLIST_CHECKPOINT_FORMAT
        or data.get("signature") != expected
        or not isinstance(data.get("assessments"), list)
    ):
        return empty
    valid = []
    for candidate, assessment in zip(candidates, data["assessments"]):
        if not _valid_saved_assessment(assessment, candidate):
            break
        valid.append(assessment)
    return json.dumps({
        "format": SHORTLIST_CHECKPOINT_FORMAT,
        "signature": expected,
        "completed": bool(data.get("completed"))
        and len(valid) == len(candidates),
        "assessments": valid,
    }, indent=2, sort_keys=True)


def checkpoint_professional_assessments(
    checkpoint: str, candidates: list[dict[str, Any]]
) -> list[str]:
    try:
        data = json.loads(str(checkpoint))
    except (TypeError, json.JSONDecodeError):
        return []
    values = data.get("assessments", []) if isinstance(data, dict) else []
    result = []
    for candidate, assessment in zip(candidates, values):
        if not _valid_saved_assessment(assessment, candidate):
            break
        result.append(json.dumps(assessment, sort_keys=True))
    return result


def remaining_professional_candidates(
    candidates: list[dict[str, Any]], assessments: list[str]
) -> list[dict[str, Any]]:
    return candidates[min(len(candidates), len(assessments)):]


def build_professional_shortlist(
    bundle: str,
    assessments: list[str],
    calibrations: list[str],
    profile: str,
) -> str:
    source = json.loads(str(bundle))
    assessment_by_photo = {}
    for value in assessments:
        item = json.loads(str(value))
        assessment_by_photo[item["photo"]] = item
    calibration_values = [json.loads(str(value)) for value in calibrations]
    tier_by_photo = {}
    batch_rank = {}
    calibration_by_photo = {}
    for batch_number, calibration in enumerate(calibration_values):
        for tier, names in calibration.get("tiers", {}).items():
            for name in names:
                tier_by_photo[name] = tier
                calibration_by_photo[name] = {
                    "rationale": calibration.get("rationale", ""),
                    "confidence": calibration.get("confidence", 0),
                }
        for position, name in enumerate(calibration.get("ranking", [])):
            batch_rank[name] = (batch_number, position)
    tier_order = {tier: number for number, tier in enumerate(TIERS)}
    ordered = sorted(
        assessment_by_photo.values(),
        key=lambda item: (
            tier_order.get(tier_by_photo.get(
                item["photo"], item.get("tier")), 99),
            -float(item.get("score", 0)),
            batch_rank.get(item["photo"], (999999, 999999)),
            item["photo"].casefold(),
        ),
    )
    entries = []
    for rank, assessment in enumerate(ordered, start=1):
        photo = assessment["photo"]
        tier = tier_by_photo.get(photo, assessment["tier"])
        warnings = list(assessment.get("local_warnings", []))
        if assessment.get("asset_warning"):
            warnings.append(assessment["asset_warning"])
        entries.append({
            "rank": rank,
            "photo": photo,
            "raw_files": assessment.get("raw_files", []),
            "cluster_id": assessment["cluster_id"],
            "tier": tier,
            "score": float(assessment["score"]),
            "confidence": min(
                float(assessment["confidence"]),
                float(calibration_by_photo.get(
                    photo, {}).get("confidence", 1))),
            "rationale": assessment["rationale"],
            "assessment": {
                field: assessment[field] for field in ASSESSMENT_FIELDS},
            "calibration": calibration_by_photo.get(photo, {}),
            "warnings": warnings,
        })
    return json.dumps({
        "format": "opencull-professional-shortlist-v1",
        "source_report_sha256": source["source_report_sha256"],
        "source_review_revision": source.get("source_review_revision"),
        "candidate_policy": source["candidate_policy"],
        "profile": str(profile).strip().lower() or "family",
        "candidate_signature": source["signature"],
        "entries": entries,
        "excluded": source.get("excluded", []),
        "statistics": {
            "evaluated": len(entries),
            "excluded_locally": len(source.get("excluded", [])),
            **{
                tier: sum(entry["tier"] == tier for entry in entries)
                for tier in TIERS
            },
            "raw_available": sum(
                bool(entry["raw_files"]) for entry in entries),
        },
        "notice": (
            "Recommendations only. No photograph was modified. Review every "
            "professional-editing recommendation before operating on files."
        ),
    }, indent=2, sort_keys=True)


def professional_report_valid(
    report: str, bundle: str, candidates: list[dict[str, Any]]
) -> bool:
    try:
        value = json.loads(str(report))
        source = json.loads(str(bundle))
    except (TypeError, json.JSONDecodeError):
        return False
    entries = value.get("entries", []) if isinstance(value, dict) else []
    expected = {candidate["photo"] for candidate in candidates}
    actual = {entry.get("photo") for entry in entries}
    return (
        value.get("format") == "opencull-professional-shortlist-v1"
        and value.get("source_report_sha256")
        == source.get("source_report_sha256")
        and len(entries) == len(expected)
        and actual == expected
        and [entry.get("rank") for entry in entries]
        == list(range(1, len(entries) + 1))
        and all(entry.get("tier") in TIERS for entry in entries)
    )


def professional_report_evidence(report: str) -> str:
    try:
        value = json.loads(str(report))
    except (TypeError, json.JSONDecodeError):
        return "INVALID PROFESSIONAL SHORTLIST"
    entries = value.get("entries", [])
    projection = {
        "format": value.get("format"),
        "candidate_policy": value.get("candidate_policy"),
        "entry_count": len(entries),
        "ranks_contiguous": [entry.get("rank") for entry in entries]
        == list(range(1, len(entries) + 1)),
        "tier_counts": {
            tier: sum(entry.get("tier") == tier for entry in entries)
            for tier in TIERS
        },
        "all_rationales_present": all(
            bool(str(entry.get("rationale", "")).strip())
            for entry in entries),
        "all_raw_paths_relative": all(
            not Path(name).is_absolute() and ".." not in Path(name).parts
            for entry in entries for name in entry.get("raw_files", [])),
        # Bounded to stay inside the judge's 6000-character window with
        # headroom: a projection that overflows is silently truncated, and
        # json.dumps(sort_keys=True) puts tier_counts after sample -- the
        # live panel was once asked to verify a field the truncation had
        # removed, and rightly refused five times.
        "sample": [{
            "rank": entry.get("rank"),
            "photo": entry.get("photo"),
            "tier": entry.get("tier"),
            "score": entry.get("score"),
            "confidence": entry.get("confidence"),
            "warnings": entry.get("warnings", []),
            "rationale": str(entry.get("rationale", ""))[:200],
        } for entry in entries[:6]],
        "sampled_entries": min(len(entries), 6),
    }
    return "DETERMINISTIC PROFESSIONAL-SHORTLIST PROJECTION:\n" + json.dumps(
        projection, indent=2, sort_keys=True)


def professional_report_policy() -> str:
    # The judge's claim covers only what the judge is for. Tier legality,
    # entry structure and rank arithmetic are deterministically checked
    # before this judgment runs, and a live panel proved that re-asking a
    # verifier to confirm pre-verified mechanics only invites a fumble --
    # one refused a satisfied membership check with a non-sequitur. What
    # remains is the boolean flags a verifier reads cleanly, and the
    # semantic question models exist to answer.
    return (
        "a read-only professional-editing shortlist: the projection's "
        "flags ranks_contiguous, all_rationales_present and "
        "all_raw_paths_relative are all true, and the sampled rationales "
        "describe visible photographic qualities -- composition, light, "
        "subject, moment -- rather than file metadata, applying a strict "
        "professional bar"
    )
