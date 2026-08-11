"""Deterministic kernel for scene-specific professional edit directions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from scan import visible_photograph

FORMAT = "opencull-edit-directions-v1"
CHECKPOINT_FORMAT = "opencull-edit-directions-checkpoint-v1"
FIELDS = (
    "scene_reading", "standard_title", "standard_intent",
    "standard_instructions", "signature_title", "signature_intent",
    "signature_instructions", "creative_title", "creative_intent",
    "creative_instructions", "standard_recipe", "signature_recipe",
    "creative_recipe", "personal_title", "personal_intent",
    "personal_instructions", "personal_recipe", "personal_style_choice",
    "personal_style_reason", "guardrails", "confidence",
)
REQUIRED_FIELDS = tuple(field for field in FIELDS if field not in {
    "personal_title", "personal_intent", "personal_instructions",
    "personal_recipe", "personal_style_choice", "personal_style_reason"})

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


def _load_style_profile(path: str) -> dict[str, Any]:
    """One personal style profile, with the identity of the file it came from."""
    profile_path, profile = _load(path)
    if profile.get("format") != "opencull-personal-style-profile-v1":
        raise ValueError("unsupported personal style profile")
    profile["path"] = str(profile_path)
    profile["sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    return profile


def offered_styles(request: str) -> list[dict[str, Any]]:
    """The styles a run may speak in, in the order they are offered."""
    value = json.loads(str(request))
    offered = value.get("style_profiles")
    return offered if isinstance(offered, list) else []


# How much of each style the menu carries. Enough to tell them apart and
# to know which scenes each was learned on; not so much that eight of them
# crowd out the photograph being looked at.
STYLE_SIGNATURE = 420
STYLE_ADAPTATION = 300


def _say(body: dict[str, Any], *keys: str, limit: int = 300) -> str:
    for key in keys:
        text = str(body.get(key) or "").strip()
        if text:
            return text if len(text) <= limit else text[:limit].rstrip() + "…"
    return ""


def style_menu(profiles: list[dict[str, Any]]) -> str:
    """The styles as a numbered menu, each said in its own words.

    A name alone cannot be chosen between -- "Coastal Twilight" and
    "Heritage Street" are labels, not evidence. Each entry carries what the
    look actually is and what the profile itself says about adapting to a
    scene, because that is the part that decides whether it fits.
    """
    lines = []
    for number, profile in enumerate(profiles, start=1):
        body = profile.get("profile", {}) if isinstance(profile, dict) else {}
        signature = _say(
            body, "visual_signature", "signature", limit=STYLE_SIGNATURE)
        adaptation = _say(
            body, "scene_adaptation", limit=STYLE_ADAPTATION)
        lines.append(
            f"{number}. {body.get('profile_name', 'Untitled style')}\n"
            f"   Look: {signature}"
            + (f"\n   Suits: {adaptation}" if adaptation else ""))
    return "\n\n".join(lines)


def build_edit_request(
    shortlist_path: str, review_path: str, photos: str, style_profile: str = "",
    only_photo: str = "", only_photos: str = "[]", style_profiles: str = "[]",
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
    # A photographer has more than one taste, and which one a photograph
    # wants is a judgement about the photograph. Every style is offered and
    # the answer says which it chose; a single style is the same thing with
    # one option, so nothing here special-cases it.
    try:
        offered = json.loads(str(style_profiles or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError("style_profiles must be a JSON list") from exc
    if not isinstance(offered, list) or not all(
        isinstance(item, str) for item in offered
    ):
        raise ValueError("style_profiles must be a JSON list of file paths")
    chosen_paths = [str(item).strip() for item in offered if str(item).strip()]
    if str(style_profile).strip():
        chosen_paths.insert(0, str(style_profile).strip())
    seen: set[str] = set()
    style_bank: list[dict[str, Any]] = []
    for path in chosen_paths:
        profile = _load_style_profile(path)
        if profile["sha256"] in seen:
            continue
        seen.add(profile["sha256"])
        style_bank.append(profile)
    personal_profile = style_bank[0] if len(style_bank) == 1 else None
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
            "style_profiles": style_bank,
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
        "style_profiles": style_bank,
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
    return str(visible_photograph(
        Path(value["photos_root"]), str(candidate["photo"])))


def edit_direction_prompt(candidate: dict[str, Any], profile: str) -> str:
    raw = bool(candidate.get("raw_files"))
    bank = candidate.get("style_profiles") or []
    if bank:
        # The photographer's tastes are offered as a menu and the answer
        # names the number it used. A number cannot be a style that does
        # not exist, which a name could.
        styles = f"""
The photographer has {len(bank)} personal style
{'profile' if len(bank) == 1 else 'profiles'}, each learned from their own
finished work:

{style_menu(bank)}

For option 4, choose the ONE of these that this particular photograph asks
for -- the light it was taken in, its subject, and its place -- rather than
the one that is listed first. Set personal_style_choice to that number, and
say in personal_style_reason what in this photograph made it the right one
and what would have made another better. Judge the photograph, not the
prose: a style whose signature does not fit this scene is the wrong answer
even when it is the photographer's favourite."""
    else:
        styles = """
The photographer has no personal style profile, so option 4 is your own
reading of what a personal treatment of this photograph would be. Set
personal_style_choice to 0 and leave personal_style_reason empty."""
    return f"""You are a senior photographic editor and colorist. Inspect the
actual supplied photograph, not merely its metadata. Propose three genuinely
different, tasteful edit directions appropriate to this exact scene.

Photo: {candidate.get('photo')}
Existing assessment: {json.dumps(candidate.get('assessment', {}))}
Existing rationale: {candidate.get('rationale', '')}
Human note: {candidate.get('human_note', '')}
RAW companion known: {'yes' if raw else 'no'}
Context profile: {profile}
{styles}

Return:
1. Standard professional: polished, natural, durable, publication-quality.
2. Signature style: distinctive authorship while respecting skin, place,
   moment, and believable light.
3. Creative: a bold reinterpretation that remains aesthetically coherent and
   never becomes gimmicky, ugly, or destructive to the subject.
4. Personal style, professionally refined: apply the chosen learned style
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


def edit_direction_repair_prompt(value: Any, candidate: Any = None) -> str:
    """Request a format-only repair after deterministic validation fails."""
    offered = len((candidate or {}).get("style_profiles") or []) if isinstance(
        candidate, dict) else 0
    style_rule = (
        f"personal_style_choice must be a whole number from 1 through "
        f"{offered}, naming which of the photographer's styles the personal "
        f"treatment speaks in. Keep the one the malformed object used if it "
        f"is in range."
        if offered else
        "personal_style_choice must be 0: no personal style was offered.")
    return f"""You are repairing structured data, not re-editing a photograph.
The JSON-compatible object below was produced by another photographic model,
but it failed OpenCull's deterministic schema validation. Return the complete
corrected object and preserve its photographic meaning, titles, instructions,
parameter guidance, and guardrails. Do not summarize, omit, embellish, or
replace its recommendations.

Required top-level fields: {', '.join(FIELDS)}.
Confidence must be a number from 0 through 1. {style_rule} Each of standard_recipe,
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
    clean: dict[str, Any] = {
        field: str(value.get(field, "")).strip()
        for field in FIELDS
        if field not in {"confidence", "personal_style_choice"}
    }
    # Which style was used is a number into the menu the prompt offered. A
    # number that cannot be read is no choice at all rather than the first.
    try:
        clean["personal_style_choice"] = int(
            float(str(value.get("personal_style_choice", 0)).strip() or 0))
    except (TypeError, ValueError):
        clean["personal_style_choice"] = -1
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


def valid_style_choice(value: Any, candidate: Any = None) -> bool:
    """Whether the style it says it used is one it was actually offered.

    A treatment attributed to a style nobody offered is a treatment whose
    provenance is invented, so this is checked as strictly as the recipes
    are. With no styles offered the only honest answer is none.
    """
    if not isinstance(value, dict):
        return False
    offered = len((candidate or {}).get("style_profiles") or []) if isinstance(
        candidate, dict) else 0
    choice = value.get("personal_style_choice")
    if not isinstance(choice, int) or isinstance(choice, bool):
        return False
    if not offered:
        return choice == 0
    return 1 <= choice <= offered


def valid_edit_direction(value: Any, candidate: Any = None) -> bool:
    if not valid_style_choice(value, candidate):
        return False
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


def chosen_style(
    direction: dict[str, Any], candidate: dict[str, Any],
) -> dict[str, Any] | None:
    """The style this answer says it spoke in, resolved to the file itself.

    The number is turned back into a path and a hash here, so a treatment
    can be read months later against the profile that produced it rather
    than against a name that may since have been changed.
    """
    bank = candidate.get("style_profiles") or []
    choice = direction.get("personal_style_choice")
    if not isinstance(choice, int) or not 1 <= choice <= len(bank):
        return None
    profile = bank[choice - 1]
    body = profile.get("profile", {}) if isinstance(profile, dict) else {}
    return {
        "path": profile.get("path", ""),
        "sha256": profile.get("sha256", ""),
        "profile_name": body.get("profile_name", ""),
        "reason": str(direction.get("personal_style_reason", "")).strip(),
        "offered": len(bank),
    }


def edit_direction_json(
    direction: dict[str, Any], candidate: dict[str, Any],
    format_repaired: bool = False,
) -> dict[str, Any]:
    style = chosen_style(direction, candidate)
    return {
        "photo": candidate["photo"],
        "format_repaired": bool(format_repaired),
        **direction,
        **({"personal_style": style} if style else {}),
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


def validation_failures(value: Any, candidate: Any = None) -> list[str]:
    """Name every reason a direction fails validation, for the record."""
    if not isinstance(value, dict):
        return [f"direction is {type(value).__name__}, not a mapping"]
    failures = []
    for field in REQUIRED_FIELDS:
        if field == "confidence":
            continue
        text = value.get(field)
        if not (isinstance(text, str) and text.strip()):
            failures.append(f"{field} is missing or blank")
    confidence = value.get("confidence")
    if not (isinstance(confidence, (int, float))
            and 0 <= confidence <= 1):
        failures.append("confidence is not a number between 0 and 1")
    if not valid_style_choice(value, candidate):
        failures.append(
            "personal_style_choice does not name one of the styles offered")
    titles = {
        str(value.get(f"{kind}_title", "")).casefold()
        for kind in ("standard", "signature", "creative")
    }
    if len(titles) != 3:
        failures.append("the three treatment titles are not distinct")
    for field in (
        "standard_recipe", "signature_recipe",
        "creative_recipe", "personal_recipe",
    ):
        raw = value.get(field)
        if not raw:
            continue
        try:
            recipe = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            failures.append(f"{field} is not valid JSON")
            continue
        if not isinstance(recipe, dict):
            failures.append(f"{field} is not a JSON object")
            continue
        missing = set(RECIPE_SECTIONS) - set(recipe)
        extra = set(recipe) - set(RECIPE_SECTIONS)
        if missing:
            failures.append(
                f"{field} lacks sections: {', '.join(sorted(missing))}")
        if extra:
            failures.append(
                f"{field} has unknown sections: {', '.join(sorted(extra))}")
        malformed = [
            section for section in RECIPE_SECTIONS
            if section in recipe and not (
                isinstance(recipe[section], list)
                and all(isinstance(step, str) and step.strip()
                        for step in recipe[section]))
        ]
        if malformed:
            failures.append(
                f"{field} sections are not lists of steps: "
                + ", ".join(malformed))
        if not any(recipe.get(section) for section in RECIPE_SECTIONS):
            failures.append(f"{field} has no steps at all")
    return failures


def rejected_edit_direction_json(
    candidate: dict[str, Any], format_repaired: bool = False,
    direction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep a failed photograph auditable without fabricating edit advice."""
    entry = {
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
    if direction is not None:
        entry["kimiya_validation"]["failures"] = (
            validation_failures(direction, candidate)[:8])
    return entry


def as_offered(entry: Any) -> dict[str, Any]:
    """A stored entry read back as the menu it was chosen from.

    An entry carries the style it resolved to rather than the list it
    picked from, so re-validating it later has to reconstruct how many
    were on the table. Only the count matters to the check.
    """
    style = (entry.get("personal_style") or {}) if isinstance(
        entry, dict) else {}
    try:
        offered = int(style.get("offered", 0) or 0)
    except (TypeError, ValueError):
        offered = 0
    return {"style_profiles": [None] * max(0, offered)}


def edit_direction_needs_validation(entry: dict[str, Any]) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("kimiya_validation", {}).get("status") != "rejected"
        and valid_edit_direction(entry, as_offered(entry))
    )


def valid_edit_report_entry(entry: dict[str, Any]) -> bool:
    if valid_edit_direction(entry, as_offered(entry)):
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
    bank = value.get("style_profiles") or []
    return json.dumps({
        "format": FORMAT,
        # Every style the run could have spoken in. Which one each frame
        # actually used is on the frame, because that is where the choice
        # was made.
        "style_profiles": [
            {"path": item.get("path", ""),
             "sha256": item.get("sha256", ""),
             "profile_name": item.get("profile", {}).get("profile_name", "")}
            for item in bank if isinstance(item, dict)],
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
