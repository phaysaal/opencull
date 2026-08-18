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
              "tone.highlight", "tone.shadow", "tone.white", "tone.black",
              "tone.stretch")),
    ("Levels", ("levels.black_input", "levels.white_input", "levels.midpoint")),
    ("Colour", ("color.temperature", "color.tint", "color.saturation")),
    ("Detail", ("detail.clarity", "detail.structure", "detail.dehaze",
                "detail.clean_colour", "detail.night_clean")),
    ("Finish", ("finish.vignette", "finish.grain")),
)

LABELS = {
    "tone.exposure": "Exposure",
    "tone.brightness": "Brightness",
    "tone.contrast": "Contrast",
    "tone.highlight": "Highlights",
    "tone.shadow": "Shadows",
    "tone.white": "Whites",
    "tone.black": "Blacks",
    "tone.stretch": "Night stretch",
    "levels.black_input": "Black point",
    "levels.white_input": "White point",
    "levels.midpoint": "Midpoint",
    "color.temperature": "Temperature",
    "color.tint": "Tint",
    "color.saturation": "Saturation",
    "detail.clarity": "Clarity",
    "detail.structure": "Structure",
    "detail.dehaze": "Dehaze",
    "detail.clean_colour": "Clean colour",
    "detail.night_clean": "Quiet the sky",
    "finish.vignette": "Vignette",
    "finish.grain": "Grain",
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
            "mode": str(operation.get("mode") or "delta"),
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
    elif shape == "brush":
        pass                                  # the map is the geometry
    elif shape == "color":
        def stated(name: str, default: float) -> float:
            match = re.search(rf"\b{name}\s*(-?\d+(?:\.\d+)?)", text)
            return float(match.group(1)) if match else default

        found["hue"] = stated("hue", 0.0) % 360
        found["range"] = stated("range", 30.0)
        found["softness"] = stated("softness", 20.0)
        found["sat_floor"] = stated("above", 10.0)
        found["sat_ceiling"] = stated("below", 100.0)
        found["light_floor"] = stated("brighter than", 0.0)
        found["light_ceiling"] = stated("darker than", 100.0)
        aim_sat = stated("target saturation", -1.0)
        aim_light = stated("target light", -1.0)
        if aim_sat >= 0:
            found["aim_sat"] = aim_sat
        if aim_light >= 0:
            found["aim_light"] = aim_light
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
    elif shape == "brush":
        parts = ["painted by hand"]
    elif shape == "color":
        parts = ["colour mask"]
        parts.append(f"hue {float(geometry.get('hue', 0)):.0f}")
        parts.append(f"range {float(geometry.get('range', 30)):.0f}")
        parts.append(f"softness {float(geometry.get('softness', 20)):.0f}")
        parts.append(f"above {float(geometry.get('sat_floor', 10)):.0f}% "
                     "saturation")
        ceiling = float(geometry.get("sat_ceiling", 100))
        if ceiling < 100:
            parts.append(f"below {ceiling:.0f}% saturation")
        lit = float(geometry.get("light_floor", 0))
        if lit > 0:
            parts.append(f"brighter than {lit:.0f}%")
        dim = float(geometry.get("light_ceiling", 100))
        if dim < 100:
            parts.append(f"darker than {dim:.0f}%")
        if geometry.get("aim_sat") is not None:
            parts.append(
                f"target saturation {float(geometry['aim_sat']):.0f}")
        if geometry.get("aim_light") is not None:
            parts.append(f"target light {float(geometry['aim_light']):.0f}")
    if geometry.get("inverted"):
        parts.append("inverted")
    return ", ".join(parts)


HSL_BANDS = ("red", "orange", "yellow", "green",
             "teal", "blue", "purple", "magenta")
HSL_COMPONENTS = ("hue", "saturation", "lightness")


def hsl_state(recipe: dict[str, Any]) -> dict[tuple, dict[str, float]]:
    """Every colour-band move the recipe holds, keyed (band, component)."""
    held: dict[tuple, dict[str, float]] = {}
    for op in recipe.get("operations", []) or []:
        if not isinstance(op, dict) or op.get("op") != "color.hsl_range":
            continue
        channel = str(op.get("channel", "")).casefold()
        component = str(op.get("component", "")).casefold()
        if channel not in HSL_BANDS or component not in HSL_COMPONENTS:
            continue
        value = op.get("value")
        if not isinstance(value, (int, float)):
            continue
        asked = op.get("asked_value")
        held[(channel, component)] = {
            "value": float(value),
            "asked": float(asked if isinstance(asked, (int, float))
                           else value)}
    return held


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
        if operation.get("erased"):
            # Erased, not removed: the operation stays in the recipe as a
            # disabled record -- nothing here is destroyed -- and it keeps
            # its ordinal so every other layer's edits stay addressed.
            continue
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
            "ordinal": ordinal,
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
    mask = next((item for item in placed
                 if item["ordinal"] == int(ordinal)), None)
    if mask is None:
        return []
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


def _unpacked(text: str) -> list[str]:
    """A guardrail that is secretly a list, opened into its sentences.

    Treatment programs sometimes hand back their whole promise set as
    one stringified Python list; shown raw it reads as a wall of
    quoted debris. If the string parses as a list of strings, those
    are the guardrails; anything else is one guardrail, as written.
    """
    told = text.strip()
    if told.startswith("[") and told.endswith("]"):
        import ast

        try:
            value = ast.literal_eval(told)
        except (ValueError, SyntaxError):
            return [told]
        if isinstance(value, (list, tuple)) and all(
                isinstance(item, str) for item in value):
            return [item.strip() for item in value if item.strip()]
    return [told]


def guardrails(recipe: dict[str, Any]) -> list[str]:
    """What the treatment promised not to do, which is not up for adjustment."""
    found = []
    for item in recipe.get("guardrails", []) or []:
        if isinstance(item, (list, tuple)):
            for said in item:
                if isinstance(said, str) and said.strip():
                    found.extend(_unpacked(said))
            continue
        text = item if isinstance(item, str) else str(
            (item or {}).get("source_instruction") or "")
        if text.strip():
            found.extend(_unpacked(text))
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

    # Operations a measurement produced rather than a slider: the
    # night start's sky offset and its stretch, say. They are ordinary
    # operations once they land -- movable, switchable, portable --
    # but nothing on the page could have typed them, so they arrive
    # whole. Asking again replaces what was asked before.
    made = changes.get("+ops")
    if isinstance(made, list):
        result["operations"] = [
            item for item in result.get("operations", []) or []
            if not (isinstance(item, dict) and item.get("measured"))]
        at = next(
            (index for index, item in enumerate(result["operations"])
             if isinstance(item, dict)
             and str(item.get("op", "")).startswith("mask.")),
            len(result["operations"]))
        placed = []
        for item in made:
            if not isinstance(item, dict) or not item.get("op"):
                continue
            held = json.loads(json.dumps(item))
            held["measured"] = True
            held.setdefault("enabled", True)
            placed.append(held)
            recorded.append({"id": str(held["op"]), "op": str(held["op"]),
                             "asked": 0.0, "set": 0.0, "enabled": True,
                             "unit": str(held.get("unit", "")),
                             "inserted": True})
        result["operations"][at:at] = placed

    # The curve the photographer drew. One per recipe: drawing again
    # replaces it, and clearing the change removes nothing the model
    # asked for, because a model cannot ask for one.
    curve = changes.get("curve")
    if isinstance(curve, dict) and isinstance(curve.get("points"), list):
        points = [[max(0.0, min(255.0, float(x))),
                   max(0.0, min(255.0, float(y)))]
                  for x, y in curve["points"]][:16]
        # How faithfully colour rides the curve: 0 is the classic RGB
        # curve (contrast saturates and bends hue, the film trade),
        # 100 lifts luminance only and leaves the colour alone.
        drawn = {"points": points}
        preserve = curve.get("preserve")
        if preserve is not None:
            drawn["preserve"] = max(0.0, min(100.0, float(preserve)))
        existing = next(
            (item for item in result.get("operations", []) or []
             if isinstance(item, dict) and item.get("op") == "tone.curve"),
            None)
        if existing is not None:
            existing["value"] = drawn
        else:
            operations = result.setdefault("operations", [])
            at = next(
                (index for index, item in enumerate(operations)
                 if isinstance(item, dict)
                 and str(item.get("op", "")).startswith("mask.")),
                len(operations))
            operations.insert(at, {
                "op": "tone.curve", "unit": "curve", "mode": "absolute",
                "value": drawn,
                "source": "curve drawn by hand", "enabled": True})
        recorded.append({"id": "tone.curve", "op": "tone.curve",
                         "asked": 0.0, "set": 0.0, "enabled": True,
                         "unit": "curve", "inserted": True})

    # The photographer's frame: a crop rectangle in fractions of the
    # straightened picture, and the straightening itself. Update the
    # operations the treatment already has, insert them where it does
    # not; a full-frame, level ask removes what the hand added earlier
    # rather than recording a crop of everything.
    framing = changes.get("crop")
    if isinstance(framing, dict):
        left = min(max(float(framing.get("left", 0.0)), 0.0), 0.95)
        top = min(max(float(framing.get("top", 0.0)), 0.0), 0.95)
        wide = min(max(float(framing.get("width", 1.0)), 0.05), 1.0 - left)
        tall = min(max(float(framing.get("height", 1.0)), 0.05), 1.0 - top)
        angle = max(-15.0, min(15.0, float(framing.get("angle", 0.0))))
        operations = result.setdefault("operations", [])
        whole = (left <= 0.001 and top <= 0.001
                 and wide >= 0.998 - left and tall >= 0.998 - top)

        def settle(op_name: str, value, meaningless: bool) -> None:
            existing = next(
                (op for op in operations
                 if isinstance(op, dict) and op.get("op") == op_name), None)
            if existing is not None:
                if meaningless and existing.get(
                        "source") == "framed by hand":
                    operations.remove(existing)
                else:
                    existing["value"] = value
                    existing["enabled"] = not meaningless or bool(
                        existing.get("source") != "framed by hand")
            elif not meaningless:
                operations.append({
                    "op": op_name, "value": value,
                    "unit": "crop" if op_name == "geometry.crop" else
                    "degrees", "mode": "absolute",
                    "source": "framed by hand", "enabled": True})

        settle("geometry.crop",
               {"left": round(left, 4), "top": round(top, 4),
                "width": round(wide, 4), "height": round(tall, 4)},
               whole)
        settle("geometry.rotation", round(angle, 2), abs(angle) < 0.01)
        recorded.append({"id": "crop", "op": "geometry.crop",
                         "asked": 0.0, "set": 0.0, "enabled": True,
                         "unit": "crop", "inserted": True})

    # The colour bands: eight hues, each with a hue shift, a saturation
    # move and a lightness move. One op per (band, component), updated in
    # place where the treatment already has it -- what the model asked is
    # kept beside what was set -- and inserted before the masks where it
    # has not.
    for item in changes.get("+hsl", []) or []:
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel", "")).casefold()
        component = str(item.get("component", "")).casefold()
        if channel not in HSL_BANDS or component not in HSL_COMPONENTS:
            continue
        bound = 45.0 if component == "hue" else 100.0
        value = max(-bound, min(bound, float(item.get("value", 0.0))))
        existing = next(
            (op for op in result.get("operations", []) or []
             if isinstance(op, dict) and op.get("op") == "color.hsl_range"
             and str(op.get("channel", "")).casefold() == channel
             and str(op.get("component", "")).casefold() == component),
            None)
        if existing is not None:
            existing.setdefault(
                "asked_value", float(existing.get("value", 0.0)))
            existing["value"] = value
            existing["enabled"] = value != 0.0 or bool(
                existing.get("asked_value"))
        elif value != 0.0:
            operations = result.setdefault("operations", [])
            at = next(
                (index for index, op in enumerate(operations)
                 if isinstance(op, dict)
                 and str(op.get("op", "")).startswith("mask.")),
                len(operations))
            operations.insert(at, {
                "op": "color.hsl_range", "channel": channel,
                "component": component, "value": value,
                "unit": "percent", "mode": "delta",
                "source": "colour band moved by hand",
                "asked_value": 0.0, "enabled": True})
        recorded.append({"id": f"hsl:{channel}/{component}",
                         "op": "color.hsl_range", "asked": 0.0,
                         "set": value, "enabled": True, "unit": "percent",
                         "inserted": existing is None})

    # New masks, appended after everything, which is where the engine
    # blends them anyway.
    for item in changes.get("+mask", []) or []:
        if not isinstance(item, dict):
            continue
        shape = str(item.get("shape", "radial"))
        if shape not in {"radial", "linear", "luma", "color", "brush"}:
            shape = "radial"
        geometry = dict(item.get("geometry") or {})
        result.setdefault("operations", []).append({
            "op": f"mask.{shape}", "unit": "mask", "mode": "absolute",
            "source": str(item.get("label") or "added by hand"),
            **({"erased": True, "enabled": False}
               if item.get("erased") else {}),
            "value": {
                "anchor": _build_anchor(shape, geometry),
                "opacity": float(geometry.get("opacity", 100)) / 100.0,
                "feather": float(geometry.get("feather", 100)) / 100.0,
                # A painted mask carries its strokes with it: a small
                # greyscale map inside the operation, so the recipe
                # remains one self-contained file.
                **({"map": str(item.get("map"))}
                   if item.get("map") else {}),
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
            if "map" in change:
                value["map"] = str(change["map"])
            if "label" in change:
                operation["source"] = str(change["label"])[:80]
            if change.get("deleted") is True:
                operation["enabled"] = False
                operation["erased"] = True
            for item in change.get("add_effects", []) or []:
                if not isinstance(item, dict):
                    continue
                op = str(item.get("op", ""))
                if op not in RANGES:
                    continue
                low, high, unit = RANGES[op]
                already = next(
                    (effect for effect in value.get("effects", []) or []
                     if isinstance(effect, dict)
                     and str(effect.get("op")) == op), None)
                if already is not None:
                    # Asking again moves the same effect: the change set
                    # carries the latest value, and a live re-application
                    # must land it rather than shrug.
                    already["value"] = max(
                        low, min(high, float(item.get("value", 0.0))))
                    continue
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


def transplant(changes: dict[str, Any], source: dict[str, Any],
               target: dict[str, Any]) -> dict[str, Any]:
    """The same moves, re-addressed to another photograph's recipe.

    A change that moved a compiled control references that control by
    its id, and ids belong to one compile: "op-003" is Exposure on this
    frame and Contrast on the next. So a move travels by what it DOES:
    the source control's operation name is looked up in the target, and
    the change lands on the target's own id for it. An operation the
    target never compiled is inserted instead, so the move still lands.
    Structural changes -- masks, curves, colour bands, added controls --
    carry no foreign ids and travel verbatim; only a hand-move of a
    model-made mask rides by position, the one address masks have.
    """
    settled = json.loads(json.dumps(changes))
    from_source = {item["id"]: item for item in controls(source)}
    to_target: dict[str, str] = {}
    for item in controls(target):
        to_target.setdefault(item["op"], item["id"])
    moved: dict[str, Any] = {}
    inserts = list(settled.pop("+insert", []) or [])
    for key, change in settled.items():
        told = from_source.get(key)
        if told is None or not isinstance(change, dict):
            moved[key] = change
            continue
        landing = to_target.get(told["op"])
        if landing is not None:
            held = moved.get(landing)
            moved[landing] = (dict(change) if held is None
                              else {**held, **change})
        elif "value" in change and change.get("enabled", True) is not False:
            inserts.append({"op": told["op"],
                            "value": float(change["value"])})
    if inserts:
        moved["+insert"] = inserts
    return moved
