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


def full_surface(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    """Every control the renderer has, whether this recipe used it or not.

    "Advanced" fine tuning means the whole instrument. A control whose
    operation is absent from the recipe sits at neutral and is marked
    absent; the page greys it until it is touched, and touching it is
    what inserts a real operation -- so the recipe stays the single
    truth and nothing on screen edits state the renderer cannot see.
    """
    present = controls(recipe)
    have = {item["op"] for item in present}
    for name, (low, high, unit) in RANGES.items():
        if name in have or name not in LABELS:
            continue
        neutral = _NEUTRAL.get(name, 0.0)
        present.append({
            "id": name, "op": name,
            "label": LABELS.get(name, name.split(".")[-1].title()),
            "section": _section_of(name),
            "value": neutral, "asked": neutral,
            "unit": unit, "low": low, "high": high,
            "enabled": True, "source": "",
            "absent": True,
        })
    present.sort(key=lambda item: _ORDER.get(item["op"], len(_ORDER)))
    return present


def insert(recipe: dict[str, Any], op: str, value: float) -> dict[str, Any]:
    """A new recipe with one absent operation made real.

    Inserted in the canonical section order, at delta mode from neutral,
    marked as the photographer's own ask -- the model asked for nothing.
    """
    if op not in RANGES:
        raise AdjustmentError(f"unknown operation: {op}")
    result = json.loads(json.dumps(recipe))
    operations = result.setdefault("operations", [])
    if any(isinstance(item, dict) and item.get("op") == op
           for item in operations):
        raise AdjustmentError(f"operation already present: {op}")
    low, high, unit = RANGES[op]
    made = {
        "op": op, "unit": unit, "mode": "delta",
        "value": max(low, min(high, float(value))),
        "asked_value": _NEUTRAL.get(op, 0.0),
        "source": "added by hand",
    }
    place = _ORDER.get(op, len(_ORDER))
    at = len(operations)
    for index, item in enumerate(operations):
        if not isinstance(item, dict):
            continue
        name = str(item.get("op", ""))
        if name.startswith("mask.") or _ORDER.get(name, len(_ORDER)) > place:
            at = index
            break
    operations.insert(at, made)
    result["revision"] = int(result.get("revision", 0) or 0) + 1
    return result


# Where each control does nothing. Levels and gamma are not zero-centred.
_NEUTRAL = {
    "levels.black_input": 0.0,
    "levels.white_input": 255.0,
    "levels.midpoint": 1.0,
}


# --- masks: where an adjustment lands, made movable ----------------------
#
# A mask operation's geometry lives in its anchor sentence, because the
# engine reads sentences. These two functions are the only place the
# sentence is parsed or written, so a slider moving "radius" cannot
# drift from what the renderer will do with the words.

EDGES = ("bottom", "top", "left", "right")
BANDS = ("shadows", "midtones", "highlights")

_MASK_EFFECT_RANGE = 1e-9  # effects reuse RANGES; sentinel for clarity


def _parse_anchor(shape: str, anchor: str) -> dict[str, Any]:
    import re

    text = str(anchor or "").casefold()
    found: dict[str, Any] = {"inverted": ("invert" in text
                                          or "outside" in text
                                          or "except" in text)}
    if shape == "radial":
        placed = re.search(
            r"at\s*(\d+(?:\.\d+)?)\s*%[,\s]+(\d+(?:\.\d+)?)\s*%", text)
        found["centre_x"] = float(placed.group(1)) if placed else 50.0
        found["centre_y"] = float(placed.group(2)) if placed else 50.0
        stated = re.search(r"radius\s*(\d+(?:\.\d+)?)\s*%", text)
        found["radius"] = float(stated.group(1)) if stated else 50.0
        found["on_sun"] = placed is None and (
            "bright" in text or "sun" in text)
    elif shape == "linear":
        found["edge"] = next(
            (edge for edge in EDGES if edge in text), "bottom")
        reach = re.search(r"up\s*to\s*(\d+(?:\.\d+)?)\s*%", text)
        found["reach"] = float(reach.group(1)) if reach else 100.0
    elif shape == "luma":
        found["band"] = next(
            (band for band in BANDS
             if band.rstrip("s") in text or band in text), "shadows")
    return found


def _build_anchor(shape: str, geometry: dict[str, Any]) -> str:
    parts = [f"{shape} gradient"]
    if shape == "radial":
        if geometry.get("on_sun"):
            parts.append("on the sun")
        else:
            parts.append(
                f"at {float(geometry.get('centre_x', 50)):.0f}%, "
                f"{float(geometry.get('centre_y', 50)):.0f}%")
        parts.append(f"radius {float(geometry.get('radius', 50)):.0f}%")
    elif shape == "linear":
        edge = str(geometry.get("edge", "bottom"))
        parts.append(f"from the {edge if edge in EDGES else 'bottom'}")
        reach = float(geometry.get("reach", 100))
        if 0 < reach < 100:
            parts.append(f"up to {reach:.0f}%")
    elif shape == "luma":
        band = str(geometry.get("band", "shadows"))
        parts.append(band if band in BANDS else "shadows")
    if geometry.get("inverted"):
        parts.append("inverted")
    return ", ".join(parts)


def masks(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    """Every mask in one recipe, its geometry parsed and its effects bounded.

    A mask's id is its ordinal among masks -- "mask:1" is the first --
    which stays stable when whole-frame operations are inserted above.
    """
    found = []
    ordinal = 0
    for operation in recipe.get("operations", []) or []:
        if not isinstance(operation, dict):
            continue
        name = str(operation.get("op", ""))
        if not name.startswith("mask.") or name == "mask.vignette":
            continue
        value = operation.get("value")
        if not isinstance(value, dict):
            continue
        ordinal += 1
        shape = name[5:]
        geometry = _parse_anchor(shape, str(value.get("anchor") or ""))
        geometry["feather"] = round(
            float(value.get("feather", 1.0) or 1.0) * 100)
        geometry["opacity"] = round(
            float(value.get("opacity", 1.0) or 1.0) * 100)
        effects = []
        for effect in value.get("effects", []) or []:
            if not isinstance(effect, dict):
                continue
            effect_op = str(effect.get("op", ""))
            amount = effect.get("value")
            if effect_op not in RANGES or not isinstance(
                    amount, (int, float)) or isinstance(amount, bool):
                continue
            low, high, unit = RANGES[effect_op]
            asked = effect.get("asked_value")
            effects.append({
                "id": f"mask:{ordinal}/{effect_op}",
                "op": effect_op,
                "label": LABELS.get(effect_op,
                                    effect_op.split(".")[-1].title()),
                "section": "Mask",
                "value": float(amount),
                "asked": float(asked if isinstance(asked, (int, float))
                               else amount),
                "unit": unit, "low": low, "high": high,
                "enabled": True, "source": "",
            })
        found.append({
            "id": f"mask:{ordinal}",
            "shape": shape,
            "label": str(operation.get("source") or f"mask {ordinal}"),
            "geometry": geometry,
            "enabled": operation.get("enabled", True) is not False,
            "effects": effects,
        })
    return found


def mask_surface(recipe: dict[str, Any], ordinal: int) -> list[dict[str, Any]]:
    """Every control the renderer has, pointed at one mask's inside.

    The engine renders a masked effect by running the same global
    operation and blending it through the mask, so a mask layer's
    control surface is the whole instrument, exactly like the base
    layer's: the effects the mask carries come first with their asked
    marks, and the rest sit quiet until touched. Ids are the changes
    keys apply() reads -- "mask:N/op" -- so the same Control widget
    serves both layers without knowing which one it is on.
    """
    placed = masks(recipe)
    if not 1 <= int(ordinal) <= len(placed):
        return []
    mask = placed[int(ordinal) - 1]
    have = {item["op"]: item for item in mask["effects"]}
    surface = []
    for name, (low, high, unit) in RANGES.items():
        if name not in LABELS:
            continue
        present = have.get(name)
        if present is not None:
            entry = dict(present)
            # The layer's surface reads like the base layer's: the same
            # sections, not a "Mask" bucket -- the layer IS the mask.
            entry["section"] = _section_of(name)
            surface.append(entry)
            continue
        neutral = _NEUTRAL.get(name, 0.0)
        surface.append({
            "id": f"mask:{int(ordinal)}/{name}", "op": name,
            "label": LABELS.get(name, name.split(".")[-1].title()),
            "section": _section_of(name),
            "value": neutral, "asked": neutral,
            "unit": unit, "low": low, "high": high,
            "enabled": True, "source": "", "absent": True,
        })
    surface.sort(key=lambda item: _ORDER.get(item["op"], len(_ORDER)))
    return surface


def _mask_operations(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    return [operation for operation in recipe.get("operations", []) or []
            if isinstance(operation, dict)
            and str(operation.get("op", "")).startswith("mask.")
            and str(operation.get("op", "")) != "mask.vignette"
            and isinstance(operation.get("value"), dict)]


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
    recorded = []

    # Operations the photographer asked into existence. Applied first,
    # so a later change to one of them lands on a real operation. This
    # is also what makes an added control actually render: the changes
    # dict is the only payload the render path carries.
    for item in changes.get("+insert", []) or []:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op", ""))
        if any(isinstance(existing, dict) and existing.get("op") == op
               for existing in result.get("operations", []) or []):
            continue
        result = insert(result, op, float(item.get("value", 0.0)))
        recorded.append({"id": op, "op": op,
                         "asked": _NEUTRAL.get(op, 0.0),
                         "set": float(item.get("value", 0.0)),
                         "enabled": True,
                         "unit": RANGES[op][2], "inserted": True})

    # New masks, appended after everything, which is where the engine
    # blends them anyway.
    for item in changes.get("+mask", []) or []:
        if not isinstance(item, dict):
            continue
        shape = str(item.get("shape", "radial"))
        if shape not in {"radial", "linear", "luma"}:
            shape = "radial"
        geometry = dict(item.get("geometry") or {})
        result.setdefault("operations", []).append({
            "op": f"mask.{shape}", "unit": "mask", "mode": "absolute",
            "source": str(item.get("label") or "added by hand"),
            "value": {
                "anchor": _build_anchor(shape, geometry),
                "opacity": float(geometry.get("opacity", 100)) / 100.0,
                "feather": float(geometry.get("feather", 100)) / 100.0,
                "effects": [
                    {"op": str(effect.get("op")),
                     "value": max(RANGES[str(effect.get("op"))][0],
                                  min(RANGES[str(effect.get("op"))][1],
                                      float(effect.get("value", 0.0)))),
                     "unit": RANGES[str(effect.get("op"))][2],
                     "mode": "delta", "asked_value": 0.0}
                    for effect in item.get("effects", []) or []
                    if isinstance(effect, dict)
                    and str(effect.get("op")) in RANGES
                ],
            },
        })
        recorded.append({"id": f"mask.{shape}", "op": f"mask.{shape}",
                         "asked": 0.0, "set": 0.0, "enabled": True,
                         "unit": "mask", "inserted": True})

    # Changes to the masks already there: geometry rebuilt into the
    # anchor sentence (the only thing the engine reads), effects moved
    # inside the compiler's bounds, a disabled effect removed from the
    # list because the engine applies whatever the list holds.
    placed = _mask_operations(result)
    # Structure before values: an effect asked into a mask must exist
    # before a change to it is looked up, and a dict's insertion order
    # is nobody's contract.
    ordered = sorted(changes.items(),
                     key=lambda pair: "/" in str(pair[0]))
    for key, change in ordered:
        if not str(key).startswith("mask:") or not isinstance(change, dict):
            continue
        head, _, effect_op = str(key).partition("/")
        try:
            ordinal = int(head.split(":", 1)[1])
        except ValueError:
            continue
        if not 1 <= ordinal <= len(placed):
            continue
        operation = placed[ordinal - 1]
        value = operation["value"]
        shape = str(operation["op"])[5:]
        if not effect_op:
            for item in change.get("add_effects", []) or []:
                if not isinstance(item, dict):
                    continue
                op = str(item.get("op", ""))
                if op not in RANGES or any(
                        isinstance(effect, dict)
                        and str(effect.get("op")) == op
                        for effect in value.get("effects", []) or []):
                    continue
                low, high, unit = RANGES[op]
                value.setdefault("effects", []).append({
                    "op": op, "unit": unit, "mode": "delta",
                    "value": max(low, min(high,
                                          float(item.get("value", 0.0)))),
                    "asked_value": _NEUTRAL.get(op, 0.0),
                })
                recorded.append({"id": f"{head}/{op}", "op": op,
                                 "asked": _NEUTRAL.get(op, 0.0),
                                 "set": float(item.get("value", 0.0)),
                                 "enabled": True, "unit": unit,
                                 "inserted": True})
        if effect_op:
            for effect in list(value.get("effects", []) or []):
                if not isinstance(effect, dict) or str(
                        effect.get("op")) != effect_op:
                    continue
                low, high, _unit = RANGES.get(effect_op, (0, 0, ""))
                previous = float(effect.get("value", 0.0))
                if "value" in change:
                    effect.setdefault("asked_value", previous)
                    effect["value"] = max(low, min(high,
                                                   float(change["value"])))
                if change.get("enabled") is False:
                    value["effects"].remove(effect)
                recorded.append({
                    "id": key, "op": effect_op,
                    "asked": float(effect.get("asked_value", previous)),
                    "set": float(effect.get("value", previous)),
                    "enabled": change.get("enabled", True) is not False,
                    "unit": RANGES.get(effect_op, (0, 0, ""))[2],
                })
            continue
        if "geometry" in change and isinstance(change["geometry"], dict):
            geometry = _parse_anchor(shape, str(value.get("anchor") or ""))
            geometry.update(change["geometry"])
            if "centre_x" in change["geometry"] or "radius" in \
                    change["geometry"] or "centre_y" in change["geometry"]:
                geometry["on_sun"] = False
            value["anchor"] = _build_anchor(shape, geometry)
            if "feather" in change["geometry"]:
                value["feather"] = float(
                    change["geometry"]["feather"]) / 100.0
            if "opacity" in change["geometry"]:
                value["opacity"] = float(
                    change["geometry"]["opacity"]) / 100.0
        if "enabled" in change:
            operation["enabled"] = bool(change["enabled"])
        recorded.append({"id": key, "op": operation["op"],
                         "asked": 0.0, "set": 0.0,
                         "enabled": operation.get("enabled", True)
                         is not False,
                         "unit": "mask"})

    by_id = {item["id"]: item for item in controls(result)}
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


# The sections the compiler's grammar is keyed by, in the order a phrase is
# tried against them. A phrase that compiles under an earlier section never
# reaches a later one.
_SPOKEN_SECTIONS = (
    "global_exposure",
    "hdr_levels_curves",
    "white_balance_and_color",
    "detail_and_noise",
    "finishing_and_output",
)


def compile_words(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn typed words into bounded operations, locally, for nothing.

    The same compiler that turns a model's prose into a recipe reads the
    photographer's own words, so the grammar is one grammar: whatever a
    suggestion could say, a person can type. Returns the operations that
    were heard and the phrases that were not -- and the not-heard are
    reported, never dropped, because words that silently do nothing teach
    the wrong lesson about what the box is.
    """
    from recipe_compiler import RecipeCompileError, compile_recipe

    heard: list[dict[str, Any]] = []
    unheard: list[str] = []
    phrases = [
        phrase.strip()
        for chunk in str(text).replace(";", ",").splitlines()
        for phrase in chunk.split(",")
        if phrase.strip()
    ]
    for phrase in phrases:
        found = None
        for section in _SPOKEN_SECTIONS:
            try:
                compiled = compile_recipe(
                    "spoken", "standard", "Spoken", "",
                    json.dumps({section: [phrase]}), "", "jpeg")
            except RecipeCompileError:
                continue
            operations = [
                item for item in compiled.get("operations", [])
                if isinstance(item, dict) and item.get("op") in RANGES
            ]
            if operations:
                found = operations
                break
        if found:
            heard.extend(found)
        else:
            unheard.append(phrase)
    return heard, unheard
