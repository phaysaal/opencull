"""Moving the numbers a treatment compiled into, without leaving its bounds.

A treatment arrives as prose -- "lift the shadows a little, cool the sky,
keep skin honest" -- and the compiler turns each sentence into a typed,
bounded operation. That operation list is what the renderer executes, so it
is also the only honest place to disagree with the model: everything on
screen came from one of those numbers, and nothing else can change what is
rendered.

So fine tuning edits the operations rather than offering a second set of
sliders beside them. Each control carries the sentence that produced it,
what the model asked for, and what the photographer set instead. A
photographer can move a value inside the range the compiler would have
accepted, or switch an operation off entirely, and both are recorded.

Two things are deliberately not editable.

Guardrails are not controls. "Keep skin believable" is a promise the
treatment made and the verification pass checks; quietly switching it off
would leave a certificate judging a rendering against a claim the rendering
no longer makes. They are shown, and they stay.

Ranges are not negotiable. The bounds here are the compiler's own, so an
adjusted recipe is exactly as executable as the one it came from, and a
value that would have been refused coming from a model is refused coming
from a person.
"""

from __future__ import annotations

import json
from typing import Any

from recipe_compiler import RANGES

FORMAT = "opencull-development-adjustment-v1"

# The controls, grouped the way a photographer reaches for them rather than
# the way the operation names sort.
SECTIONS = (
    ("Tone", ("tone.exposure", "tone.brightness", "tone.contrast",
              "tone.highlight", "tone.shadow", "tone.white", "tone.black")),
    ("Levels", ("levels.black_input", "levels.white_input", "levels.midpoint")),
    ("Colour", ("color.temperature", "color.tint", "color.saturation")),
    ("Detail", ("detail.clarity", "detail.structure", "detail.dehaze")),
    ("Finish", ("finish.vignette",)),
)

LABELS = {
    "tone.exposure": "Exposure",
    "tone.brightness": "Brightness",
    "tone.contrast": "Contrast",
    "tone.highlight": "Highlights",
    "tone.shadow": "Shadows",
    "tone.white": "Whites",
    "tone.black": "Blacks",
    "levels.black_input": "Black point",
    "levels.white_input": "White point",
    "levels.midpoint": "Midpoint",
    "color.temperature": "Temperature",
    "color.tint": "Tint",
    "color.saturation": "Saturation",
    "detail.clarity": "Clarity",
    "detail.structure": "Structure",
    "detail.dehaze": "Dehaze",
    "finish.vignette": "Vignette",
}

# How a value is written where it is read. The unit is the compiler's; only
# the presentation is decided here.
UNITS = {
    "EV": ("{value:+.2f} EV", 0.01),
    "percent": ("{value:+.0f}%", 1.0),
    "kelvin": ("{value:,.0f} K", 10.0),
    "level-8bit": ("{value:.0f}", 1.0),
    "gamma": ("{value:.2f}", 0.01),
}

_ORDER = {name: index for index, (_section, names) in enumerate(SECTIONS)
          for name in names}


class AdjustmentError(ValueError):
    """An adjustment cannot be applied to this recipe."""


def _section_of(operation: str) -> str:
    for name, members in SECTIONS:
        if operation in members:
            return name
    return "Other"


def written(value: float, unit: str) -> str:
    """One control's value, in the words its unit is read in."""
    pattern, _step = UNITS.get(unit, ("{value:g}", 1.0))
    return pattern.format(value=value)


def step_for(unit: str) -> float:
    """The smallest move worth making in one unit."""
    return UNITS.get(unit, ("", 1.0))[1]


def controls(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    """The operations of one recipe that a photographer can move.

    Only bounded numeric operations appear. A guardrail is not a control, and
    an operation the compiler could not bound is not one either: there would
    be no range to keep the photographer inside.
    """
    found = []
    for operation in recipe.get("operations", []) or []:
        if not isinstance(operation, dict):
            continue
        name = str(operation.get("op", ""))
        value = operation.get("value")
        if name not in RANGES or not isinstance(value, (int, float)):
            continue
        if isinstance(value, bool):
            continue
        low, high, unit = RANGES[name]
        if str(operation.get("unit")) != unit:
            continue
        asked = operation.get("asked_value")
        found.append({
            "id": str(operation.get("id") or name),
            "op": name,
            "label": LABELS.get(name, name.split(".")[-1].title()),
            "section": _section_of(name),
            "value": float(value),
            # What the model asked for, kept from the first adjustment on, so
            # the comparison survives being moved twice.
            "asked": float(asked if isinstance(asked, (int, float)) else value),
            "unit": unit,
            "low": low,
            "high": high,
            "enabled": operation.get("enabled", True) is not False,
            "source": str(operation.get("source_instruction") or ""),
        })
    found.sort(key=lambda item: _ORDER.get(item["op"], len(_ORDER)))
    return found


def guardrails(recipe: dict[str, Any]) -> list[str]:
    """What the treatment promised not to do, which is not up for adjustment."""
    found = []
    for item in recipe.get("guardrails", []) or []:
        text = item if isinstance(item, str) else str(
            (item or {}).get("source_instruction") or "")
        if text.strip():
            found.append(text.strip())
    for operation in recipe.get("operations", []) or []:
        if isinstance(operation, dict) and str(
                operation.get("op", "")).startswith("guardrail."):
            text = str(operation.get("source_instruction") or "").strip()
            if text:
                found.append(text)
    return list(dict.fromkeys(found))


def clamp(control: dict[str, Any], value: float) -> float:
    return max(float(control["low"]), min(float(control["high"]), float(value)))


def apply(recipe: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """A new recipe with the photographer's changes folded in.

    The original is never mutated: a preview of an adjustment must not be
    able to alter the treatment it came from. Every changed operation keeps
    what the model asked for beside what was set, so the page can always
    show both and a later reader can tell them apart.
    """
    if not isinstance(recipe, dict):
        raise AdjustmentError("recipe must be an object")
    result = json.loads(json.dumps(recipe))
    by_id = {item["id"]: item for item in controls(result)}
    recorded = []
    for operation in result.get("operations", []) or []:
        if not isinstance(operation, dict):
            continue
        key = str(operation.get("id") or operation.get("op", ""))
        change = changes.get(key)
        control = by_id.get(key)
        if change is None or control is None:
            continue
        if "value" in change:
            operation.setdefault("asked_value", control["asked"])
            operation["value"] = clamp(control, change["value"])
        if "enabled" in change:
            operation.setdefault("asked_value", control["asked"])
            operation["enabled"] = bool(change["enabled"])
        recorded.append({
            "id": key,
            "op": control["op"],
            "asked": control["asked"],
            "set": float(operation.get("value", control["value"])),
            "enabled": operation.get("enabled", True) is not False,
            "unit": control["unit"],
        })
    if not recorded:
        return result
    result["revision"] = int(result.get("revision", 0) or 0) + 1
    result["adjustments"] = {
        "format": FORMAT,
        "changes": recorded,
    }
    return result


def moved(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    """The controls that no longer say what the model asked for."""
    return [
        control for control in controls(recipe)
        if not control["enabled"]
        or abs(control["value"] - control["asked"]) > 1e-9
    ]


def describe(control: dict[str, Any]) -> str:
    """One line saying what was asked for and what was set instead."""
    if not control["enabled"]:
        return (f"model asked {written(control['asked'], control['unit'])} · "
                "you switched it off")
    if abs(control["value"] - control["asked"]) <= 1e-9:
        return f"as asked: {written(control['value'], control['unit'])}"
    return (f"model asked {written(control['asked'], control['unit'])} · "
            f"you set {written(control['value'], control['unit'])}")
