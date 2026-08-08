"""Deterministic kernel for scene-specific professional edit directions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FORMAT = "opencull-edit-directions-v1"
CHECKPOINT_FORMAT = "opencull-edit-directions-checkpoint-v1"
FIELDS = (
    "scene_reading", "standard_title", "standard_intent",
    "standard_instructions", "signature_title", "signature_intent",
    "signature_instructions", "creative_title", "creative_intent",
    "creative_instructions", "standard_recipe", "signature_recipe",
    "creative_recipe", "personal_title", "personal_intent",
    "personal_instructions", "personal_recipe", "guardrails", "confidence",
)
REQUIRED_FIELDS = tuple(field for field in FIELDS if field not in {
    "personal_title", "personal_intent", "personal_instructions", "personal_recipe"})

RECIPE_SECTIONS = (
    "base_and_lens", "composition", "global_exposure", "hdr_levels_curves",
    "white_balance_and_color", "color_editor", "layers_and_masks",
    "detail_and_noise", "finishing_and_output", "evaluation_order",
)


def _load(path: str) -> tuple[Path, dict[str, Any]]:
    resolved = Path(str(path)).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {resolved}")
    return resolved, value


def build_edit_request(
    shortlist_path: str, review_path: str, photos: str, style_profile: str = "",
    only_photo: str = "", only_photos: str = "[]",
) -> str:
    shortlist_file, shortlist = _load(shortlist_path)
    review_file, review = _load(review_path)
    if shortlist.get("format") != "opencull-professional-shortlist-v1":
        raise ValueError("unsupported professional shortlist")
    if review.get("format") != "opencull-professional-review-v1":
        raise ValueError("unsupported professional review")
    if (
        review.get("source_report_sha256")
        != shortlist.get("source_report_sha256")
        or review.get("candidate_signature")
        != shortlist.get("candidate_signature")
    ):
        raise ValueError("professional review does not match shortlist")
    root = Path(str(photos)).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"photo folder is unavailable: {root}")
    by_photo = {
        entry.get("photo"): entry
        for entry in shortlist.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("photo"), str)
    }
    personal_profile = None
    if str(style_profile).strip():
        profile_path, personal_profile = _load(style_profile)
        if personal_profile.get("format") != "opencull-personal-style-profile-v1":
            raise ValueError("unsupported personal style profile")
        personal_profile["path"] = str(profile_path)
        personal_profile["sha256"] = hashlib.sha256(
            profile_path.read_bytes()).hexdigest()
    target_photo = str(only_photo).strip()
    try:
        requested_photos = json.loads(str(only_photos or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError("only_photos must be a JSON list") from exc
    if not isinstance(requested_photos, list) or not all(
        isinstance(item, str) and item.strip() for item in requested_photos
    ):
        raise ValueError("only_photos must be a JSON list of photograph names")
    targets = {item.strip() for item in requested_photos}
    if target_photo:
        targets = {target_photo}
    candidates = []
    for photo, human in review.get("entries", {}).items():
        if not isinstance(human, dict) or human.get("interesting") is not True:
            continue
        if targets and photo not in targets:
            continue
        source = (root / Path(photo)).resolve()
        if root != source and root not in source.parents:
            raise ValueError(f"unsafe selected photo path: {photo}")
        if photo not in by_photo or not source.is_file():
            raise ValueError(f"selected photograph is unavailable: {photo}")
        entry = by_photo[photo]
        candidates.append({
            "photo": photo,
            "rank": entry.get("rank"),
            "tier": human.get("tier", entry.get("tier")),
            "score": entry.get("score"),
            "rationale": entry.get("rationale", ""),
            "assessment": entry.get("assessment", {}),
            "raw_files": entry.get("raw_files", []),
            "human_note": human.get("note", ""),
            "style_profile": personal_profile,
        })
    candidates.sort(key=lambda item: (item.get("rank", 10**9), item["photo"]))
    if targets and {item["photo"] for item in candidates} != targets:
        raise ValueError(
            "one or more target photographs are unavailable or no longer selected for edit ideas")
    signature = {
        "shortlist_sha256": hashlib.sha256(
            shortlist_file.read_bytes()).hexdigest(),
        "review_revision": review.get("revision"),
        "photos_root": str(root),
        "photos": [item["photo"] for item in candidates],
        "only_photo": target_photo,
        "only_photos": sorted(targets),
        "style_profile": personal_profile,
    }
    return json.dumps({
        "format": "opencull-edit-direction-request-v1",
        **signature,
        "signature": hashlib.sha256(
            json.dumps(signature, sort_keys=True).encode()).hexdigest(),
        "shortlist_path": str(shortlist_file),
        "review_path": str(review_file),
        "candidates": candidates,
    }, indent=2, sort_keys=True)


def parse_edit_candidates(request: str) -> list[dict[str, Any]]:
    value = json.loads(str(request))
    candidates = value.get("candidates", [])
    return candidates if isinstance(candidates, list) else []


def edit_candidate_path(request: str, candidate: dict[str, Any]) -> str:
    value = json.loads(str(request))
    return str(Path(value["photos_root"]) / Path(candidate["photo"]))


def edit_direction_prompt(candidate: dict[str, Any], profile: str) -> str:
    raw = bool(candidate.get("raw_files"))
    personal = candidate.get("style_profile")
    return f"""You are a senior photographic editor and colorist. Inspect the
actual supplied photograph, not merely its metadata. Propose three genuinely
different, tasteful edit directions appropriate to this exact scene.

Photo: {candidate.get('photo')}
Existing assessment: {json.dumps(candidate.get('assessment', {}))}
Existing rationale: {candidate.get('rationale', '')}
Human note: {candidate.get('human_note', '')}
RAW companion known: {'yes' if raw else 'no'}
Context profile: {profile}
Personal style profile (use only for option 4): {json.dumps(personal or {})}

Return:
1. Standard professional: polished, natural, durable, publication-quality.
2. Signature style: distinctive authorship while respecting skin, place,
   moment, and believable light.
3. Creative: a bold reinterpretation that remains aesthetically coherent and
   never becomes gimmicky, ugly, or destructive to the subject.
4. Personal style, professionally refined: apply the user's learned style
   profile while correcting technical weaknesses and adapting it to this scene.

For each option give a short title, artistic intent, and actionable editing
instructions: crop/aspect/geometry, exposure and tonal hierarchy, white
balance/color palette, local masks, subject/background separation, texture,
noise/sharpness, and finishing. Use parameter ranges only as starting points,
not fake measurements. Do not prescribe irreversible body/face alteration.
If no RAW is known, clearly limit recovery claims to what a JPEG can support.
The guardrails field must state what must not be damaged in this scene.
Each *_recipe field must be a JSON object encoded as text with exactly these
keys: {', '.join(RECIPE_SECTIONS)}. Each value is an ordered list of concise
steps. Make it Capture One oriented. Name relevant tools and give conservative
starting ranges where meaningful: Exposure, Contrast, Brightness, Saturation;
HDR Highlight/Shadow/White/Black; Levels input/output and midpoint; Luma/RGB
Curves; White Balance Kelvin shift and Tint direction; Clarity method/amount,
Structure and Dehaze; Advanced/Skin Tone Color Editor hue-saturation-lightness
and uniformity; Color Balance; lens profile, distortion, diffraction, light
falloff, crop/rotation/keystone; sharpening Amount/Radius/Threshold, halo
suppression, luminance/color noise, moire and grain. Describe named adjustment,
heal or clone layers and whether each mask is brush, subject/background,
linear/radial gradient, color range, or luma range, including opacity and
feathering guidance. Include an evaluation order and histogram/clipping/skin
checks. Use JSON only inside each recipe string. Return confidence strictly as
a number from 0 through 1. Before responding, verify that every recipe string
is complete JSON and that its final section closes both the list and object."""


def edit_direction_repair_prompt(value: Any) -> str:
    """Request a format-only repair after deterministic validation fails."""
    return f"""You are repairing structured data, not re-editing a photograph.
The JSON-compatible object below was produced by another photographic model,
but it failed OpenCull's deterministic schema validation. Return the complete
corrected object and preserve its photographic meaning, titles, instructions,
parameter guidance, and guardrails. Do not summarize, omit, embellish, or
replace its recommendations.

Required top-level fields: {', '.join(FIELDS)}.
Confidence must be a number from 0 through 1. Each of standard_recipe,
signature_recipe, creative_recipe, and personal_recipe must be a JSON object
encoded as text. Every encoded recipe must contain exactly these keys:
{', '.join(RECIPE_SECTIONS)}. Every recipe value must be a non-empty ordered
list of non-empty instruction strings. Repair quoting, escaping, commas,
brackets, braces, types, and missing required structure only.

Malformed object:
{json.dumps(value, ensure_ascii=False, sort_keys=True)}"""


def normalize_edit_direction(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    clean = {
        field: str(value.get(field, "")).strip()
        for field in FIELDS if field != "confidence"
    }
    confidence = value.get("confidence", 0)
    try:
        clean["confidence"] = round(float(confidence), 4)
    except (TypeError, ValueError):
        wording = str(confidence).strip().casefold()
        clean["confidence"] = (
            0.9 if "high confidence" in wording
            else 0.7 if "moderate confidence" in wording
            else 0.4 if "low confidence" in wording
            else 0
        )
    for field in (
        "standard_recipe", "signature_recipe", "creative_recipe", "personal_recipe"
    ):
        recipe_text = clean.get(field, "")
        if not recipe_text:
            continue
        parsed = None
        try:
            parsed = json.loads(recipe_text)
        except json.JSONDecodeError:
            # Vision models occasionally close the final JSON object but omit
            # the final recipe-section array bracket. Repair only that narrow,
            # unambiguous truncation; the strict validator below still checks
            # every section and step before accepting the response.
            if recipe_text.endswith('"}'):
                repaired = f"{recipe_text[:-1]}]}}"
                try:
                    parsed = json.loads(repaired)
                except json.JSONDecodeError:
                    parsed = None
        if isinstance(parsed, dict):
            # A section the model honestly had nothing to say for arrives
            # missing; it becomes an explicit empty list rather than a
            # reason to reject the whole treatment. The first live run
            # rejected every direction twice over exactly this.
            for section in RECIPE_SECTIONS:
                parsed.setdefault(section, [])
            clean[field] = json.dumps(parsed, sort_keys=True)
    return clean


def valid_edit_direction(value: Any) -> bool:
    recipes_valid = True
    for field in ("standard_recipe", "signature_recipe", "creative_recipe", "personal_recipe"):
        if not value.get(field):
            if field == "personal_recipe":
                continue
        try:
            recipe = json.loads(value.get(field, ""))
        except (TypeError, json.JSONDecodeError):
            recipes_valid = False
            continue
        recipes_valid = recipes_valid and (
            isinstance(recipe, dict)
            and set(recipe) == set(RECIPE_SECTIONS)
            and all(
                isinstance(recipe[section], list)
                and all(isinstance(step, str) and step.strip()
                        for step in recipe[section])
                for section in RECIPE_SECTIONS
            )
            # Empty sections are honest; an empty recipe is not.
            and any(recipe[section] for section in RECIPE_SECTIONS)
        )
    return (
        isinstance(value, dict)
        and all(isinstance(value.get(field), str) and value[field].strip()
                for field in REQUIRED_FIELDS if field != "confidence")
        and isinstance(value.get("confidence"), (int, float))
        and 0 <= value["confidence"] <= 1
        and len({
            value["standard_title"].casefold(),
            value["signature_title"].casefold(),
            value["creative_title"].casefold(),
        }) == 3
        and recipes_valid
    )


def edit_direction_json(
    direction: dict[str, Any], candidate: dict[str, Any],
    format_repaired: bool = False,
) -> dict[str, Any]:
    return {
        "photo": candidate["photo"],
        "format_repaired": bool(format_repaired),
        **direction,
    }


def mark_edit_direction_validation(
    entry: dict[str, Any], accepted: bool
) -> dict[str, Any]:
    marked = dict(entry)
    marked["kimiya_validation"] = {
        "status": "accepted" if accepted else "rejected",
        "system": "kimiya",
        "message": (
            "Kimiya's independent quality panel accepted these directions."
            if accepted else
            "Kimiya's independent quality panel rejected these directions. "
            "The photograph can be regenerated independently."
        ),
    }
    return marked


def rejected_edit_direction_json(
    candidate: dict[str, Any], format_repaired: bool = False
) -> dict[str, Any]:
    """Keep a failed photograph auditable without fabricating edit advice."""
    return {
        "photo": candidate["photo"],
        "format_repaired": bool(format_repaired),
        "kimiya_validation": {
            "status": "rejected",
            "system": "kimiya",
            "message": (
                "Kimiya could not validate the model's structured editing "
                "directions. Regenerate this photograph independently."
            ),
        },
    }


def edit_direction_needs_validation(entry: dict[str, Any]) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("kimiya_validation", {}).get("status") != "rejected"
        and valid_edit_direction(entry)
    )


def valid_edit_report_entry(entry: dict[str, Any]) -> bool:
    if valid_edit_direction(entry):
        return True
    validation = entry.get("kimiya_validation", {}) if isinstance(entry, dict) else {}
    return (
        isinstance(entry.get("photo"), str)
        and bool(entry["photo"].strip())
        and validation.get("status") == "rejected"
        and validation.get("system") == "kimiya"
        and isinstance(validation.get("message"), str)
        and bool(validation["message"].strip())
    )


def edit_checkpoint_path(output: str, checkpoint: str = "") -> str:
    return str(
        Path(checkpoint).expanduser().resolve()
        if str(checkpoint).strip()
        else Path(str(output) + ".checkpoint.json").expanduser().resolve())


def load_edit_checkpoint(
    path: str, request: str, resume: bool = True
) -> list[dict[str, Any]]:
    if not resume or not Path(path).is_file():
        return []
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        request_value = json.loads(request)
    except (OSError, json.JSONDecodeError):
        return []
    if (
        value.get("format") != CHECKPOINT_FORMAT
        or value.get("request_signature") != request_value.get("signature")
    ):
        return []
    entries = value.get("entries", [])
    return entries if isinstance(entries, list) else []


def remaining_edit_candidates(
    candidates: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    done = {entry.get("photo") for entry in entries}
    return [candidate for candidate in candidates
            if candidate.get("photo") not in done]


def build_edit_checkpoint(
    request: str, entries: list[dict[str, Any]], complete: bool
) -> str:
    value = json.loads(request)
    return json.dumps({
        "format": CHECKPOINT_FORMAT,
        "request_signature": value["signature"],
        "candidate_photos": value["photos"],
        "entries": entries,
        "complete": bool(complete),
    }, indent=2, sort_keys=True)


def build_edit_report(
    request: str, entries: list[dict[str, Any]], profile: str
) -> str:
    value = json.loads(request)
    personal_style = value.get("style_profile")
    return json.dumps({
        "format": FORMAT,
        "request_signature": value["signature"],
        "shortlist_sha256": value["shortlist_sha256"],
        "review_revision": value["review_revision"],
        "photos_root": value["photos_root"],
        "profile": profile,
        "style_profile": ({
            "path": personal_style.get("path", ""),
            "sha256": personal_style.get("sha256", ""),
            "profile_name": personal_style.get("profile", {}).get(
                "profile_name", ""),
        } if isinstance(personal_style, dict) else None),
        "style_profile_sha256": (
            personal_style.get("sha256", "")
            if isinstance(personal_style, dict) else ""
        ),
        "entries": entries,
        "notice": (
            "These are non-destructive creative directions, not edits. "
            "Inspect each image while applying them."),
    }, indent=2, sort_keys=True)


def valid_edit_report(
    report: str, request: str, candidates: list[dict[str, Any]]
) -> bool:
    try:
        value = json.loads(report)
        requested = json.loads(request)
    except (TypeError, json.JSONDecodeError):
        return False
    entries = value.get("entries", [])
    return (
        value.get("format") == FORMAT
        and value.get("request_signature") == requested.get("signature")
        and [entry.get("photo") for entry in entries]
        == [candidate.get("photo") for candidate in candidates]
        and all(valid_edit_report_entry(entry) for entry in entries)
    )


def edit_direction_policy() -> str:
    return (
        "Approve when this one photograph has three distinct, scene-specific, "
        "technically actionable and tasteful editing directions. The creative "
        "option remains coherent and flattering; advice avoids invented "
        "certainty, irreversible subject alteration, and damage to important "
        "scene or family-memory qualities."
    )


def edit_report_policy() -> str:
    return (
        "Approve only if every selected photograph has three distinct, "
        "scene-specific, technically actionable, tasteful directions; "
        "the creative option must remain coherent and flattering, instructions "
        "must avoid invented certainty, and originals remain untouched."
    )
