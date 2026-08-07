"""What a shoot is judged by, and who chose it.

The assessment has always been shaped by a single word threaded into its
prompt -- "Editing profile: family." -- and nothing in the application ever
chose that word. Every assessment anyone has run has therefore been judged
by the default, which is the gentlest of the four and the wrong one for
most of the work this application is for.

A stance does not change what is looked at. The same ten axes come back
either way. It changes what counts as strong: the same frame is a keeper to
a family editor and an ordinary one to a gallery. That is worth choosing on
purpose, and worth recording, because a tier means nothing without the bar
it was measured against.

Two further controls belong here and are not built. Choosing which axes are
judged, and asking for a plain releasable-or-not verdict, both need the
kernel's typed schema, its normalizer and its validator changed together --
and none of that can be shown to work without a live provider. The stance
alone reaches the prompt today, by the path that was already there.
"""

from __future__ import annotations

from typing import Any

FORMAT = "opencull-assessment-criteria-v1"

# The word each stance puts into the prompt is its id: the kernel lowercases
# and interpolates it, so these are the vocabulary, not labels for it.
STANCES: tuple[dict[str, str], ...] = (
    {
        "id": "professional",
        "name": "Professional",
        "bar": "Would a client pay for this frame?",
        "detail": "An absolute commercial bar. Technically sound is not "
                  "enough; the frame has to earn its place.",
    },
    {
        "id": "artistic",
        "name": "Artistic",
        "bar": "Is this frame interesting?",
        "detail": "Rewards the distinctive over the correct. A flawed frame "
                  "that says something outranks a clean one that does not.",
    },
    {
        "id": "documentary",
        "name": "Documentary",
        "bar": "Does this frame carry what happened?",
        "detail": "Moment and legibility over polish. An imperfect frame of "
                  "the real thing beats a handsome frame of nothing.",
    },
    {
        "id": "family",
        "name": "Family",
        "bar": "Is this worth keeping?",
        "detail": "The gentlest bar. Everyone present and recognisable is "
                  "usually enough.",
    },
)

DEFAULT_STANCE = "professional"

STANCE_IDS = tuple(item["id"] for item in STANCES)


class CriteriaError(ValueError):
    """A shoot cannot be judged by criteria that do not exist."""


def stance(identifier: str) -> dict[str, str]:
    """One stance, by name."""
    for item in STANCES:
        if item["id"] == str(identifier).strip().lower():
            return item
    raise CriteriaError(f"unsupported stance: {identifier!r}")


def normalise(identifier: str | None) -> str:
    """The stance to run under, falling back to the deliberate default.

    The fallback is professional rather than the kernel's own default of
    family: this application is for photographers deciding what to develop,
    and an unset stance should not quietly apply the gentlest bar there is.
    """
    value = str(identifier or "").strip().lower()
    return value if value in STANCE_IDS else DEFAULT_STANCE


def describe(identifier: str) -> str:
    """One line naming the bar a run was judged against."""
    item = stance(normalise(identifier))
    return f"{item['name']} — {item['bar']}"


def record(identifier: str) -> dict[str, Any]:
    """What to write down beside a shortlist so its tiers can be read.

    A tier is meaningless without the bar it was measured against, and a
    photographer comparing two shoots months apart has no way to recover it
    from the tiers themselves.
    """
    chosen = stance(normalise(identifier))
    return {
        "format": FORMAT,
        "stance": chosen["id"],
        "bar": chosen["bar"],
    }
