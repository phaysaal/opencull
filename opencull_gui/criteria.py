"""What a shoot is judged by, and who chose it.

The assessment is shaped by one description threaded into its prompt.
For a long time that description was a single canned word, and no shoot
fits a single word: a family trip is people AND places; a commission is
craft AND a client's taste. So the bar is composed instead of picked:

Strictness is one choice -- a frame cannot be judged gently and harshly
at once. Lenses are many -- the kinds of merit that count can genuinely
coexist. The photographer's own words, when given, ride along verbatim.
All of it composes into one description for one run: one model call per
frame, whatever the bar says.

A bar does not change what is looked at. The same axes come back either
way. It changes what counts as strong, and it is recorded beside the
shortlist, because a tier means nothing without the bar it was measured
against.
"""

from __future__ import annotations

from typing import Any

FORMAT = "opencull-assessment-criteria-v2"

STRICTNESS: tuple[dict[str, str], ...] = (
    {
        "id": "gentle",
        "name": "Gentle",
        "bar": "Is this worth keeping?",
        "detail": "Everyone present and recognisable is usually enough. "
                  "Serious defects still count against a frame.",
    },
    {
        "id": "balanced",
        "name": "Balanced",
        "bar": "Would you show it twice?",
        "detail": "Worth developing if it earns a second look on its own, "
                  "not merely as a record that something happened.",
    },
    {
        "id": "strict",
        "name": "Strict",
        "bar": "Would a client pay for this frame?",
        "detail": "An absolute commercial bar. Technically sound is not "
                  "enough; the frame has to earn its place.",
    },
)

LENSES: tuple[dict[str, str], ...] = (
    {
        "id": "family",
        "name": "Family",
        "detail": "People and being together: expressions, gestures, "
                  "relationships, the moment between people",
    },
    {
        "id": "place",
        "name": "Place & moment",
        "detail": "Travel and documentary: where it was, what happened, "
                  "the sense of actually standing there",
    },
    {
        "id": "artistic",
        "name": "Artistic",
        "detail": "The distinctive over the correct: light, mood, an "
                  "idea -- a flawed frame that says something",
    },
    {
        "id": "craft",
        "name": "Craft",
        "detail": "Composition, handling of light, timing and technical "
                  "polish as merits in their own right",
    },
)

STRICTNESS_IDS = tuple(item["id"] for item in STRICTNESS)
LENS_IDS = tuple(item["id"] for item in LENSES)

DEFAULT_CHOICE: dict[str, Any] = {
    "strictness": "strict", "lenses": ["craft"], "words": "",
}

# The stances of the single-choice era, kept readable forever: a stored
# preference or an old shortlist's record maps onto the composed model.
LEGACY_STANCES: dict[str, dict[str, Any]] = {
    "professional": {"strictness": "strict", "lenses": ["craft"],
                     "words": ""},
    "artistic": {"strictness": "balanced", "lenses": ["artistic"],
                 "words": ""},
    "documentary": {"strictness": "balanced", "lenses": ["place"],
                    "words": ""},
    "family": {"strictness": "gentle", "lenses": ["family"], "words": ""},
}

WORDS_LIMIT = 300


class CriteriaError(ValueError):
    """A shoot cannot be judged by criteria that do not exist."""


def strictness(identifier: str) -> dict[str, str]:
    for item in STRICTNESS:
        if item["id"] == str(identifier).strip().lower():
            return item
    raise CriteriaError(f"unsupported strictness: {identifier!r}")


def lens(identifier: str) -> dict[str, str]:
    for item in LENSES:
        if item["id"] == str(identifier).strip().lower():
            return item
    raise CriteriaError(f"unsupported lens: {identifier!r}")


def normalise_choice(value: Any) -> dict[str, Any]:
    """A valid choice from whatever was remembered, without raising.

    Accepts the composed dict, a legacy single-stance id, or garbage; the
    fallback is the deliberate strict default rather than the gentlest
    bar there is.
    """
    if isinstance(value, str):
        legacy = LEGACY_STANCES.get(value.strip().lower())
        if legacy is not None:
            return {**legacy, "lenses": list(legacy["lenses"])}
        return {**DEFAULT_CHOICE, "lenses": list(DEFAULT_CHOICE["lenses"])}
    if not isinstance(value, dict):
        return {**DEFAULT_CHOICE, "lenses": list(DEFAULT_CHOICE["lenses"])}
    chosen_strictness = str(value.get("strictness") or "").strip().lower()
    if chosen_strictness not in STRICTNESS_IDS:
        chosen_strictness = DEFAULT_CHOICE["strictness"]
    lenses = [
        str(item).strip().lower()
        for item in (value.get("lenses") or [])
        if str(item).strip().lower() in LENS_IDS
    ]
    if not lenses:
        lenses = list(DEFAULT_CHOICE["lenses"])
    seen: list[str] = []
    for item in lenses:
        if item not in seen:
            seen.append(item)
    words = str(value.get("words") or "").strip()[:WORDS_LIMIT]
    return {"strictness": chosen_strictness, "lenses": seen, "words": words}


def compose(choice: Any) -> str:
    """One description for one run, from the composed parts.

    This is the text the assessment prompt reads, so it is written as
    prose for a model rather than as a machine tag.
    """
    value = normalise_choice(choice)
    chosen = strictness(value["strictness"])
    parts = [f"{chosen['name'].lower()} bar ({chosen['bar'].rstrip('?')}?)"]
    parts.append("judged through " + "; ".join(
        f"{lens(item)['name'].lower()}: {lens(item)['detail']}"
        for item in value["lenses"]))
    if value["words"]:
        parts.append(
            f"in the photographer's own words: {value['words']}")
    return " -- ".join(parts)


def describe(choice: Any) -> str:
    """One line naming the bar a run was judged against."""
    value = normalise_choice(choice)
    names = " + ".join(lens(item)["name"] for item in value["lenses"])
    return f"{strictness(value['strictness'])['name']} · {names}"


def record(choice: Any) -> dict[str, Any]:
    """What to write down beside a shortlist so its tiers can be read.

    A tier is meaningless without the bar it was measured against, and a
    photographer comparing two shoots months apart has no way to recover
    it from the tiers themselves.
    """
    value = normalise_choice(choice)
    return {
        "format": FORMAT,
        "strictness": value["strictness"],
        "lenses": list(value["lenses"]),
        "words": value["words"],
        "stance": compose(value),
    }
