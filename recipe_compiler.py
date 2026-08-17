"""Compile photographic edit prose into bounded, executable recipe IR."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

IR_FORMAT = "opencull-development-recipe-v1"
CORPUS_FORMAT = "opencull-development-corpus-v1"
STYLES = ("standard", "signature", "creative", "personal")
# A preset is compiled by the same compiler as a suggestion -- same
# grammar, same bounds, same diagnostics -- but it is nobody's answer
# about a photograph, so it is not one of the styles a model may claim.
PRESET_STYLE = "preset"
COMPILABLE = (*STYLES, PRESET_STYLE)
RANGES = {
    "tone.exposure": (-5.0, 5.0, "EV"),
    "tone.contrast": (-100.0, 100.0, "percent"),
    "tone.brightness": (-100.0, 100.0, "percent"),
    "color.saturation": (-100.0, 100.0, "percent"),
    "tone.highlight": (-100.0, 100.0, "percent"),
    "tone.shadow": (-100.0, 100.0, "percent"),
    "tone.white": (-100.0, 100.0, "percent"),
    "tone.black": (-100.0, 100.0, "percent"),
    "levels.white_input": (0.0, 255.0, "level-8bit"),
    "levels.black_input": (0.0, 255.0, "level-8bit"),
    "levels.midpoint": (0.1, 10.0, "gamma"),
    "color.temperature": (-5000.0, 50000.0, "kelvin"),
    "color.tint": (-100.0, 100.0, "percent"),
    "detail.clarity": (-100.0, 100.0, "percent"),
    "detail.structure": (-100.0, 100.0, "percent"),
    "detail.dehaze": (-100.0, 100.0, "percent"),
    "finish.vignette": (-100.0, 100.0, "percent"),
    "color.neutralize": (0.0, 100.0, "percent"),
    "detail.clean_colour": (0.0, 100.0, "percent"),
    # The eveners: how far each pixel in a colour mask's wedge walks
    # toward the wedge's own aim. Only meaningful inside a mask.
    "uniformity.hue": (0.0, 100.0, "percent"),
    "uniformity.saturation": (0.0, 100.0, "percent"),
    "uniformity.lightness": (0.0, 100.0, "percent"),
}

# Infrared work needs two things no ordinary photograph does: a white
# balance nobody's camera profile can supply, and the freedom to move one
# colour channel into another. Both are written in the colour section.
CHANNEL_SWAPS = {
    "red-blue": [[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
    "red-green": [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
    "green-blue": [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]],
}
_SWAP_WORDS = (
    (r"red\s+(?:and|with|<->|/)\s*blue|blue\s+(?:and|with|<->|/)\s*red", "red-blue"),
    (r"red\s+(?:and|with|<->|/)\s*green|green\s+(?:and|with|<->|/)\s*red", "red-green"),
    (r"green\s+(?:and|with|<->|/)\s*blue|blue\s+(?:and|with|<->|/)\s*green", "green-blue"),
)


class RecipeCompileError(ValueError):
    """Recipe input is structurally unsafe or unsupported."""


def _number(text: str) -> float:
    return float(text)


_AMOUNT = r"([+-]?\d+(?:\.\d+)?)"
_QUALIFIERS = (
    r"(?:\s+(?:recovery|amount|around|about|approximately|roughly|near|"
    r"of|by|at|between|from|to|is|set))*"
)


def _amount_for(text: str, token: str) -> float | None:
    """Read one slider value or range following a named control."""
    match = re.search(
        rf"(?i)\b(?:{token})\b{_QUALIFIERS}\s*:?\s*{_AMOUNT}"
        rf"(?:\s*(to|through|–|—|-)\s*{_AMOUNT})?",
        text,
    )
    if not match:
        return None
    first_text = match.group(1)
    second_text = match.group(3)
    first = float(first_text)
    if second_text is not None:
        second = float(second_text)
        # A compact unsigned range such as 5-12 means 5 through 12; the
        # separator is not a negative sign. Explicit negative ranges retain
        # their sign because they use wording such as "-5 to -12".
        value = (first + second) / 2.0
    else:
        value = first
    explicit_sign = first_text.startswith(("+", "-")) or (
        second_text is not None and second_text.startswith(("+", "-")))
    if not explicit_sign:
        context = text[max(0, match.start() - 36):match.start()].casefold()
        if re.search(r"\b(reduce|lower|decrease|deepen|cool|darken)\w*\b", context):
            value = -abs(value)
        elif re.search(r"\b(increase|raise|boost|lift|warm|brighten)\w*\b", context):
            value = abs(value)
    return value


def _operation(
    operations: list[dict[str, Any]], op: str, value: Any, unit: str,
    mode: str, source: str, section: str, **extra: Any,
) -> None:
    if op in RANGES and isinstance(value, (int, float)):
        low, high, expected_unit = RANGES[op]
        if unit != expected_unit or not low <= float(value) <= high:
            raise RecipeCompileError(
                f"{op} value {value} {unit} is outside {low}..{high} "
                f"{expected_unit}")
    operations.append({
        "id": f"op-{len(operations) + 1:03d}",
        "op": op,
        "value": value,
        "unit": unit,
        "mode": mode,
        "section": section,
        "source_instruction": source,
        "enabled": True,
        **extra,
    })


def _compile_step(
    section: str, step: str, operations: list[dict[str, Any]],
) -> str | None:
    text = step.strip()
    lower = text.casefold()

    if section == "base_and_lens":
        if "chromatic aberration" in lower:
            _operation(operations, "lens.chromatic_aberration", True,
                       "boolean", "absolute", text, section)
            return None
        if "lens profile" in lower or "lens correction" in lower:
            _operation(operations, "lens.profile", True, "boolean",
                       "absolute", text, section)
            return None

    if section == "composition":
        match = re.search(r"(?i)\bcrop\s+to\s+(\d+)\s*:\s*(\d+)", text)
        if match:
            _operation(
                operations, "geometry.crop_aspect",
                [int(match.group(1)), int(match.group(2))], "ratio",
                "absolute", text, section)
            return None
        match = re.search(r"(?i)\brotate\s+([+-]?\d+(?:\.\d+)?)", text)
        if match:
            _operation(operations, "geometry.rotation",
                       _number(match.group(1)), "degree", "delta",
                       text, section)
            return None

    scalar_maps = {
        "global_exposure": {
            "exposure": "tone.exposure", "contrast": "tone.contrast",
            "brightness": "tone.brightness", "saturation": "color.saturation",
        },
        "hdr_levels_curves": {
            "highlights?": "tone.highlight", "shadows?": "tone.shadow",
            "whites?": "tone.white", "blacks?": "tone.black",
        },
        "detail_and_noise": {
            "clarity": "detail.clarity", "structure": "detail.structure",
            "dehaze": "detail.dehaze",
        },
    }
    if section == "hdr_levels_curves":
        # "Set Levels black input around 4, white input 98, midpoint 0.98,
        # with output black near 1 and white 99." Three traps in one
        # sentence: the input points are written after their name rather
        # than before it, the output points look identical to the input
        # ones, and the numbers are percentages of full scale. Read
        # naively, that sentence set the input white point to level 99 of
        # 255 -- an eight-fold multiply that blew a frame to white.
        level_matches = [
            item for item in re.finditer(
                r"(?i)\b(?:(output|input)\s+)?(white|black)"
                r"(?:\s+(input|output|point))?"
                r"[\s:]*(?:around|near|about|approximately|~|at)?[\s:]*"
                r"(\d+(?:\.\d+)?)\s*(%)?", text)
            if _levels_scope(text, item) != "output"]
        midpoint = re.search(
            r"(?i)\bmidpoint\s*:?\s*(\d+(?:\.\d+)?)", text)
        if level_matches or midpoint:
            for item in level_matches:
                _operation(
                    operations, f"levels.{item.group(2).lower()}_input",
                    _levels_value(item.group(4), item.group(5),
                                  item.group(2).lower()),
                    "level-8bit", "absolute", text, section)
            if midpoint:
                _operation(
                    operations, "levels.midpoint", _number(midpoint.group(1)),
                    "gamma", "absolute", text, section)
            return None
        match = re.search(
            r"(?i)\b(white|black)\s+point\s*:?\s*"
            r"(\d+(?:\.\d+)?)", text)
        if match:
            _operation(
                operations, f"levels.{match.group(1).lower()}_input",
                _number(match.group(2)), "level-8bit", "absolute",
                text, section)
            return None
    scalar_found = False
    for token, op in scalar_maps.get(section, {}).items():
        value = _amount_for(text, token)
        if value is not None:
            unit = RANGES[op][2]
            _operation(operations, op, value, unit,
                       "delta", text, section)
            scalar_found = True
    if scalar_found:
        return None

    if section == "white_balance_and_color":
        # "Neutralise" asks for the channels to be brought into agreement
        # rather than for a colour temperature, which is the only white
        # balance an infrared frame can be given: its own average.
        if re.search(r"(?i)\bneutrali[sz]e\b", text):
            amount = _amount_for(text, r"neutrali[sz]e|amount|strength")
            _operation(operations, "color.neutralize",
                       100.0 if amount is None else amount, "percent",
                       "absolute", text, section)
            return None
        value = _amount_for(text, r"kelvin|temperature|white\s+balance")
        if value is not None:
            mode = "delta" if abs(value) < 2000 \
                else "absolute"
            _operation(operations, "color.temperature", value, "kelvin",
                       mode, text, section)
            return None
        value = _amount_for(text, r"tint(?:\s+towards?\s+\w+)?")
        if value is not None:
            _operation(operations, "color.tint", value,
                       "percent", "delta", text, section)
            return None

    if section == "color_editor":
        # A grey mix: how much each channel contributes to brightness,
        # which is what darktable's colour calibration calls the gray tab.
        # For infrared this is the whole conversion -- the channels differ
        # by sensor response rather than by colour, so which of them
        # carries the picture is a decision, not a default.
        if re.search(r"(?i)\b(?:monochrome|grey|gray)\s+mix\b", text):
            weights = [
                _amount_for(text, token) for token in ("red", "green", "blue")]
            if all(value is not None for value in weights):
                total = sum(abs(float(value)) for value in weights) or 1.0
                row = [float(value) / total for value in weights]
                _operation(
                    operations, "color.channel_mixer", [row, row, row],
                    "matrix", "absolute", text, section)
                return None
        if re.search(r"(?i)\bswap\b", text):
            for pattern, name in _SWAP_WORDS:
                if re.search(rf"(?i){pattern}", text):
                    _operation(
                        operations, "color.channel_mixer", CHANNEL_SWAPS[name],
                        "matrix", "absolute", text, section)
                    return None
        colour_aliases = (
            (r"blue(?:/|\s+and\s+|-)cyan|cyan(?:/|\s+and\s+|-)blue", "blue/teal"),
            (r"orange(?:/|\s+and\s+|-)red|red(?:/|\s+and\s+|-)orange", "orange/red"),
            (r"yellow(?:/|\s+and\s+|-)green", "yellow/green"),
            (r"skin(?:\s+tone)?", "orange"),
            (r"cyan", "teal"), (r"blue", "blue"),
            (r"orange", "orange"), (r"red", "red"),
            (r"yellow", "yellow"), (r"green", "green"),
            (r"purple|violet", "purple"), (r"magenta", "magenta"),
        )
        channel = "all"
        for pattern, name in colour_aliases:
            if re.search(rf"(?i)\b(?:{pattern})\b", text):
                channel = name
                break
        color_found = False
        for component in ("hue", "saturation", "luminance", "lightness"):
            value = _amount_for(text, component)
            if value is None:
                continue
            _operation(
                operations, "color.hsl_range", value, "percent",
                "delta", text, section, channel=channel.casefold(),
                component=("lightness" if component.casefold() == "luminance"
                           else component.casefold()))
            color_found = True
        if color_found:
            return None

    if section == "layers_and_masks":
        lower_shape = lower
        shape = (
            "radial" if "radial gradient" in lower_shape else
            "linear" if "linear gradient" in lower_shape else
            "luma" if "luma" in lower_shape else
            "color" if "color range" in lower_shape else None)
        if shape:
            effects = []
            effect_map = {
                "exposure": "tone.exposure", "contrast": "tone.contrast",
                "brightness": "tone.brightness", "saturation": "color.saturation",
                "clarity": "detail.clarity", "structure": "detail.structure",
                "highlight": "tone.highlight", "temperature": "color.temperature",
            }
            for token, op in effect_map.items():
                value = _amount_for(text, token)
                if value is not None:
                    unit = "kelvin" if op == "color.temperature" else (
                        "EV" if op == "tone.exposure" else "percent")
                    effects.append({
                        "op": op, "value": value,
                        "unit": unit,
                        "mode": "delta",
                    })
            # Natural-language directions often omit a slider number. Keep
            # the intent executable with a conservative, explicit default;
            # the original wording remains in the mask anchor for audit.
            if not effects:
                if re.search(r"\bdarken\b", lower):
                    effects.append({"op": "tone.exposure", "value": -0.25,
                                    "unit": "EV", "mode": "delta"})
                elif re.search(r"\b(boost|brighten|lift)\b", lower):
                    effects.append({"op": "tone.exposure", "value": 0.20,
                                    "unit": "EV", "mode": "delta"})
            opacity = re.search(r"(?i)(\d+(?:\.\d+)?)%\s*opacity", text)
            feather = re.search(r"(?i)feather\s*(\d+(?:\.\d+)?)%", text)
            if effects:
                _operation(
                    operations, f"mask.{shape}", {
                        "anchor": text, "opacity": float(opacity.group(1)) / 100
                        if opacity else 1.0,
                        "feather": float(feather.group(1)) / 100
                        if feather else 0.5,
                        "effects": effects,
                    }, "mask", "absolute", text, section)
                return None
        if "vignette" in lower:
            match = re.search(r"(?i)amount\s*:?\s*([+-]?\d+(?:\.\d+)?)", text)
            if match:
                _operation(
                    operations, "mask.vignette", _number(match.group(1)),
                    "percent", "delta", text, section)
                return None

    if section == "detail_and_noise":
        match = re.search(
            r"(?i)\bsharpening\s+amount\s*:?\s*(\d+(?:\.\d+)?)", text)
        if match:
            _operation(operations, "detail.sharpen_amount",
                       _number(match.group(1)), "percent", "absolute",
                       text, section)
            return None
        match = re.search(
            r"(?i)\b(luminance|color)\s+noise\s+reduction\s*:?\s*"
            r"(\d+(?:\.\d+)?)", text)
        if match:
            _operation(operations, f"detail.denoise_{match.group(1).lower()}",
                       _number(match.group(2)), "percent", "absolute",
                       text, section)
            return None

    if section == "finishing_and_output":
        match = re.search(
            r"(?i)\bcolor\s+space\s*:?\s*([a-z0-9 -]+)", text)
        if match:
            _operation(operations, "output.color_space",
                       match.group(1).strip().lower().replace(" ", "-"),
                       "identifier", "absolute", text, section)
            return None
        match = re.search(
            r"(?i)\bvignette(?:\s+amount)?\s*:?\s*"
            r"([+-]?\d+(?:\.\d+)?)", text)
        if match:
            _operation(operations, "finish.vignette",
                       _number(match.group(1)), "percent", "delta",
                       text, section)
            return None

    # Evaluation steps are guardrails, not pixel operations.
    if section == "evaluation_order":
        return "guardrail"
    return "unsupported"



def _levels_scope(text: str, match: re.Match[str]) -> str:
    """Whether a levels point is an input or an output one.

    "with output black near 1 and white 99" means both of them are output
    points, though only the first sits beside the word. The nearest of
    the two words before the number decides, which is how the sentence
    reads aloud.
    """
    for group in (1, 3):
        word = (match.group(group) or "").lower()
        if word in {"input", "output"}:
            return word
    before = text[:match.start()].lower()
    at_input, at_output = before.rfind("input"), before.rfind("output")
    if at_output > at_input:
        return "output"
    return "input"


def _levels_value(number: str, percent: str | None, point: str) -> float:
    """A levels point as an 8-bit level, whatever scale it was written in.

    Develop programs show these as 0-255, but "white input 98" means 98
    percent of full scale far more often than it means level 98 -- which
    on any normally exposed frame clips a third of it to white, and did:
    one frame came back at eight times its own brightness.

    Only the white point is read that way. A black input of 12 is an
    ordinary level and an unremarkable percentage both, so the reading
    that changes the photograph least is the one taken; a white input of
    12 has no sane reading as a level at all.
    """
    value = _number(number)
    if value is None:
        return 0.0
    if percent or (point == "white" and value <= 100.0):
        return round(min(value, 100.0) * 2.55, 2)
    return value


def compile_recipe(
    photo: str, style: str, title: str, intent: str, recipe: Any,
    guardrails: str, source_kind: str = "raw",
) -> dict[str, Any]:
    if style not in COMPILABLE or source_kind not in {"raw", "jpeg"}:
        raise RecipeCompileError("invalid style or source kind")
    if isinstance(recipe, str):
        try:
            recipe = json.loads(recipe)
        except json.JSONDecodeError as exc:
            raise RecipeCompileError(f"recipe is not valid JSON: {exc}") from exc
    if not isinstance(recipe, dict):
        raise RecipeCompileError("recipe must be an object")
    operations: list[dict[str, Any]] = []
    checks: list[str] = []
    diagnostics: list[dict[str, str]] = []
    total = 0
    for section, steps in recipe.items():
        if not isinstance(steps, list):
            raise RecipeCompileError(f"{section} must contain a list")
        for step in steps:
            if not isinstance(step, str) or not step.strip():
                raise RecipeCompileError(f"{section} contains an invalid step")
            total += 1
            result = _compile_step(section, step, operations)
            if result == "guardrail":
                checks.append(step.strip())
            elif result == "unsupported":
                diagnostics.append({
                    "severity": "unsupported",
                    "section": section,
                    "instruction": step.strip(),
                })
    return {
        "format": IR_FORMAT,
        "source_photo": photo,
        "source_kind": source_kind,
        "style": style,
        "title": title,
        "intent": intent,
        "working_space": "scene-linear-rec2020-d65",
        "operations": operations,
        "guardrails": [guardrails.strip(), *checks] if guardrails.strip() else checks,
        "diagnostics": diagnostics,
        "coverage": {
            "instructions": total,
            "executable": len(operations),
            "guardrails": len(checks),
            "unsupported": len(diagnostics),
        },
    }


def compile_checkpoint(
    checkpoint_path: Path, source_kind: str = "raw",
) -> dict[str, Any]:
    path = checkpoint_path.expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise RecipeCompileError("checkpoint has no entries")
    recipes = []
    for entry in entries:
        for style in STYLES:
            recipe_value = entry.get(f"{style}_recipe")
            # Older direction reports have only three treatments, while a
            # rejected generation can intentionally have no recipes at all.
            # Compile every treatment that is actually present.
            if not recipe_value:
                continue
            recipes.append(compile_recipe(
                str(entry.get("photo", "")), style,
                str(entry.get(f"{style}_title", "")),
                str(entry.get(f"{style}_intent", "")),
                recipe_value,
                str(entry.get("guardrails", "")),
                source_kind,
            ))
    raw = path.read_bytes()
    return {
        "format": CORPUS_FORMAT,
        "source_checkpoint": str(path),
        "source_checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
        "recipe_count": len(recipes),
        "recipes": recipes,
        "summary": {
            key: sum(item["coverage"][key] for item in recipes)
            for key in ("instructions", "executable", "guardrails", "unsupported")
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--output", "-o", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("raw", "jpeg"), default="raw")
    parser.add_argument(
        "--strict", action="store_true",
        help="exit nonzero if any instruction is not executable")
    args = parser.parse_args(argv)
    corpus = compile_checkpoint(args.checkpoint, args.source_kind)
    args.output.expanduser().resolve().write_text(
        json.dumps(corpus, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(json.dumps(corpus["summary"], sort_keys=True))
    return int(args.strict and corpus["summary"]["unsupported"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
