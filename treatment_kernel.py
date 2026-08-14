"""Protect then reveal: a developed photograph arrived at in steps.

Every model call in this application so far has been a single question.
Look at this frame and rate it. Look at this frame and write a recipe.
That is the right shape for a judgement and the wrong shape for a
development, because developing a photograph is not one decision -- it
is a diagnosis, a strategy that follows from the diagnosis, a structure
that follows from the strategy, and then numbers, and then looking at
what the numbers did and changing your mind.

The evidence for that is an evening's work on one eclipse frame. A model
wrote a careful recipe that read "HDR Highlight -20 to -40" for a sun
with no recoverable highlight in it at all; the recipe rendered darker
and flatter than the camera's own JPEG, and the thing that fixed it was
not a better prompt. It was rendering fourteen times and measuring. Two
facts only the renders could teach: contrast pivots at middle grey, so
on a subject sitting at seven percent brightness it buries what it was
meant to reveal; and a shadow mask lifts the sky, because in a night
frame the sky *is* shadow.

So this program renders its own attempts and looks at them, with the
measurements beside the picture. That is the whole difference.

THE METHOD

"Protect then reveal" is the strategy for a frame containing something
irrecoverable -- a clipped light source, a blown highlight, a subject
the sensor simply did not record. Its published ancestor is the
darkroom practice of printing for the highlights and dodging the rest:
decide first what cannot be saved, refuse to spend the photograph
trying, wall it off, and do the work everywhere else. Adams' Zone
System is the same instinct stated as measurement -- place the tone you
care about, accept where the others fall.

Stated as an algorithm:

  1. Read the frame. Locally, for nothing: where the light is, what is
     clipped, how much of the frame is already black.
  2. Diagnose. What is this photograph of, what is wrong with it, and
     what about it is beyond recovery.
  3. Decide. What must be protected, what must be revealed, in order.
  4. Structure. Which regions need separate treatment -- and therefore
     which masks, because a mask is the answer to "these two parts of
     the frame need opposite things".
  5. Write it, in the grammar the compiler executes.
  6. Render it and measure it.
  7. Look at what happened. Name what improved, name what regressed,
     and say what to change -- or say it is finished, and why.
  8. Repeat 5 to 7 within a budget, because a photographer does not
     give one frame infinite time.

Every round is kept: the recipe, the render, the measurements and the
critique. Partly as evidence that the process did what it claims, and
partly because round two is sometimes the better photograph and nobody
finds that out by keeping only the last one.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

from recipe_compiler import PRESET_STYLE, RecipeCompileError, compile_recipe

TREATMENT_FORMAT = "darkimiya-treatment-v1"
STRATEGY = "protect-then-reveal"

# What a photographer would give one frame. Each round past the first
# costs a critique and a rewrite, and the returns fall off fast: on the
# frame this was built against, round two found the real fault and round
# three tuned it.
DEFAULT_ROUNDS = 3


# --- what a photograph is, before anybody is asked ----------------------

def measure_frame(path: str) -> dict[str, Any]:
    """The frame's own condition, measured rather than described.

    A model handed a JPEG can see that a photograph is dark. It cannot
    see that 22% of it is already at black, that a fifth of a percent is
    clipped past recovery, or that the brightest thing in the frame
    stands 105 levels above what surrounds it. Those decide the
    strategy, and they cost nothing.
    """
    with Image.open(str(path)) as opened:
        image = opened.convert("RGB")
        image.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
    rgb = np.asarray(image, dtype=np.float32)
    grey = np.asarray(image.convert("L"), dtype=np.float32)
    height, width = grey.shape
    smooth = np.asarray(
        image.convert("L").filter(ImageFilter.MedianFilter(5)), dtype=np.float32)

    # Where the light is, and how far it stands above its surroundings --
    # the measure that decides whether a bright subject reads at all.
    #
    # The centre of the bright region, not the first pixel that happens
    # to hit maximum. A clipped sun is a plateau, and argmax returns its
    # top-left corner; the mask that will be aimed here centres on the
    # region, so the number reported has to mean the same thing or the
    # model is reasoning about a point nothing will act on.
    bright = np.nonzero(grey >= 0.5 * float(grey.max()))
    if bright[0].size:
        yy = float(bright[0].mean())
        xx = float(bright[1].mean())
    else:
        yy, xx = height / 2.0, width / 2.0
    ys, xs = np.ogrid[:height, :width]
    radius = np.sqrt((ys - yy) ** 2 + (xs - xx) ** 2)
    unit = max(height, width) / 100.0
    core = grey[radius < 2.2 * unit]
    ring = grey[(radius > 5 * unit) & (radius < 9 * unit)]

    hsv = np.asarray(image.convert("HSV"), dtype=np.float32)
    saturation = hsv[..., 1] / 255.0
    coloured = saturation > 0.25
    return {
        "size": [width, height],
        "mean": round(float(grey.mean()), 2),
        "median": round(float(np.median(grey)), 2),
        "clipped_percent": round(float((grey >= 250).mean() * 100), 3),
        "black_percent": round(float((grey <= 6).mean() * 100), 2),
        "silhouette_percent": round(float((grey < 26).mean() * 100), 2),
        "veil_percent": round(float(((grey > 25) & (grey < 60)).mean() * 100), 2),
        "grain": round(float(np.abs(grey - smooth).mean()), 3),
        "subject_contrast": round(
            float(core.mean() - ring.mean()) if core.size and ring.size else 0.0, 1),
        "brightest_at": [round(float(xx / max(width - 1, 1)), 3),
                         round(float(yy / max(height - 1, 1)), 3)],
        "saturation_percent": round(float(saturation.mean() * 100), 1),
        "hue_degrees": (
            round(float(np.median(
                (hsv[..., 0] * 360 / 255)[coloured])), 0)
            if coloured.any() else None),
        "channel_means": [round(float(rgb[..., index].mean()), 1)
                          for index in range(3)],
    }


def measure_frame_json(path: str) -> str:
    """The same measurements, as the text a program passes around."""
    return json.dumps(measure_frame(path), sort_keys=True)


def evidence_json(photo: str, baseline: str, spectrum: str = "visible",
                  cutoff_nm: float = 0, about: str = "") -> str:
    """Everything known locally about one frame, as the program's ground truth."""
    return json.dumps({
        "photo": photo,
        "spectrum": spectrum,
        "cutoff_nm": float(cutoff_nm) if spectrum == "infrared" else 0.0,
        "about": " ".join(str(about).split())[:600],
        "baseline": measure_frame(baseline),
    }, sort_keys=True, indent=2)


# --- one plan, then a branch --------------------------------------------
#
# The first shape of this asked four questions in the same order however
# the photograph answered them: diagnose, decide, structure, write. That
# is a chain, not a decision. A frame needing no mask paid for the mask
# reasoning anyway; a frame needing three got one paragraph describing
# all of them, which the recipe step then had to re-read.
#
# So the questions that always apply are asked together, and one of the
# answers is how many masks this photograph needs. Zero is a real
# answer and the common one. Where it is not zero, each mask is asked
# for on its own, in numbers -- what it is around, where its centre is,
# how wide, and what happens inside it -- and the recipe is assembled
# here from those numbers rather than written as prose and hoped to
# compile.

# The moves, as numbers rather than as sentences. A model that answers
# with these cannot half-compile: there is no prose to parse, and the
# recipe is assembled here.
MOVES = {
    "exposure": ("tone.exposure", -5.0, 5.0, "EV"),
    "contrast": ("tone.contrast", -100.0, 100.0, "percent"),
    "brightness": ("tone.brightness", -100.0, 100.0, "percent"),
    "saturation": ("color.saturation", -100.0, 100.0, "percent"),
    "highlights": ("tone.highlight", -100.0, 100.0, "percent"),
    "shadows": ("tone.shadow", -100.0, 100.0, "percent"),
    "whites": ("tone.white", -100.0, 100.0, "percent"),
    "blacks": ("tone.black", -100.0, 100.0, "percent"),
    "clarity": ("detail.clarity", -100.0, 100.0, "percent"),
    "structure": ("detail.structure", -100.0, 100.0, "percent"),
    "dehaze": ("detail.dehaze", -100.0, 100.0, "percent"),
    "temperature": ("color.temperature", -5000.0, 5000.0, "kelvin"),
    "denoise": ("detail.denoise_luminance", 0.0, 100.0, "percent"),
    "denoise_colour": ("detail.denoise_color", 0.0, 100.0, "percent"),
    "vignette": ("finish.vignette", -100.0, 100.0, "percent"),
}
# Not every move can live inside a mask: the engine applies a mask by
# rendering its effects and blending, and only these are meaningful
# there.
MASK_MOVES = ("exposure", "contrast", "brightness", "saturation",
              "clarity", "structure", "highlights", "temperature")

_GLOBAL_MOVES = (
    "The whole-frame moves, each a number or left out:\n  "
    + ", ".join(sorted(MOVES)))
_MASK_MOVES = (
    "The moves available inside a mask, each a number or left out:\n  "
    + ", ".join(MASK_MOVES))

# What rendering taught, which no amount of looking at a JPEG will.
_LEARNED = """Three things this renderer has been measured doing, which
are not obvious:

  • Contrast pivots at middle grey. On a subject sitting at seven
    percent brightness, positive contrast drives it toward black rather
    than away from it. To lift something dark, use exposure.
  • A luma mask on the shadows selects the sky in a night frame, because
    the sky is the shadow. To reach the ground, use a linear gradient
    from the bottom.
  • To make a clipped subject stand out, take the WHOLE FRAME DOWN
    rather than lifting around it. A clipped core is far above white and
    stays white however far you drop the exposure, while everything
    around it darkens. Lifting instead raises the veil with the subject
    and the separation is lost -- measured on one frame: subject
    contrast 164.8 before, 52.6 after a lift, and it never recovered
    across three rounds of trying."""

_VOICE = (
    "You are developing one photograph, the way a professional would: by "
    "deciding what the picture is for before touching a slider. Answer only "
    "the question asked at this step. Do not write a recipe until you are "
    "asked for one."
)


def _context(evidence: dict[str, Any]) -> str:
    lines = [f"THE FRAME, MEASURED LOCALLY:\n{json.dumps(evidence['baseline'], indent=2)}"]
    if evidence.get("about"):
        lines.append(
            "WHAT THE PHOTOGRAPHER SAYS THIS SHOOT IS, in their own words: "
            f"“{evidence['about']}” Treat this as context about the "
            "subject, which you cannot see and they can.")
    if evidence.get("spectrum") == "infrared":
        cutoff = float(evidence.get("cutoff_nm") or 0)
        where = f"a {cutoff:.0f}nm cut-off filter" if cutoff else "an infrared filter"
        lines.append(
            f"THIS IS AN INFRARED PHOTOGRAPH, taken through {where}. Past the "
            "cut-off the sensor's three channels record almost the same "
            "light, so there is no white balance to find and no natural "
            "palette to restore. Bright foliage and dark skies are correct.")
    return "\n\n".join(lines)


def keep_mask(directory: str, masks: list[str], mask: Any) -> bool:
    """Write one mask down as it is settled, and say whether it says anything."""
    return keep_reasoning(directory, f"mask-{len(masks) + 1}", mask)


def plan_prompt(evidence_text: str, critique: Any = "") -> str:
    """Everything that always applies, asked at once -- including the branch."""
    evidence = data(evidence_text) or {}
    revision = ([
        "THE LAST ATTEMPT WAS RENDERED AND MEASURED. What it showed, and "
        f"what you said to change:\n{said(data(critique))}",
        "Revise the plan. Change what the critique named -- including the "
        "number of masks, if the structure was wrong -- and leave the rest "
        "alone. A rewrite that moves everything cannot be judged.",
    ] if critique else [])
    return "\n\n".join([
        _VOICE,
        "PLAN THIS DEVELOPMENT. Answer all of the following together.",
        "1. DIAGNOSE. What is this photograph of, what is wrong with it, "
        "and -- the part that decides everything after it -- what about it "
        "is beyond recovery. A clipped light source is not a fault to be "
        "fixed: no highlight recovery invents detail in channels that all "
        "saturated, and attempting it turns a shape into a smudge.",
        "2. DECIDE. What must be PROTECTED, left alone because touching it "
        "can only cost, and what must be REVEALED. Two or three of each; a "
        "photograph with six priorities has none.",
        "3. SEPARATE. Do two parts of this frame need opposite things? If "
        "they do, that is a mask, and you will be asked about each one "
        "separately afterwards. Say how many -- 0, 1, 2 or 3. Zero is a "
        "real answer and the usual one: most photographs want one set of "
        "adjustments applied to all of them. Only ask for a mask where you "
        "can name the region and say what it needs that the rest does not.",
        "4. THE WHOLE FRAME. The adjustments that apply everywhere, before "
        "any mask. Give them as numbers.",
        _GLOBAL_MOVES,
        _LEARNED,
        *revision,
        _context(evidence),
    ])


def mask_prompt(evidence_text: str, plan: Any, index: float, total: float,
                already: list[str]) -> str:
    """One mask, in numbers: where it is, how big, and what happens inside."""
    evidence = data(evidence_text) or {}
    at = (evidence.get("baseline") or {}).get("brightest_at") or [0.5, 0.5]
    done = "\n".join(
        f"  already placed: {said(data(item))}" for item in already)
    return "\n\n".join([
        _VOICE,
        f"MASK {int(index) + 1} OF {int(total)}. You said this photograph "
        "needs its regions treated separately. Describe this one in "
        "numbers.",
        "shape: 'radial' for a region around a point; 'linear' for a "
        "gradient from an edge; 'luma' for a band of brightness.",
        "For a radial mask give centre_x and centre_y as percentages "
        "across and down the frame, and radius as a percentage of the "
        "frame's longer side. For a linear mask, say which edge in "
        "'anchor' -- bottom, top, left or right. For a luma mask, say "
        "shadows, midtones or highlights in 'anchor'.",
        "Set 'inverted' true where the adjustment belongs everywhere "
        "EXCEPT the region -- protecting something is usually this.",
        f"The brightest point of this frame is at "
        f"{at[0] * 100:.0f}% across, {at[1] * 100:.0f}% down.",
        _MASK_MOVES,
        _LEARNED,
        f"YOUR PLAN:\n{said(plan)}",
        done or "  nothing placed yet.",
    ])


def strategy_prompt(evidence_text: str, diagnosis: str) -> str:
    return "\n\n".join([
        _VOICE,
        "STEP 2 of 4 -- DECIDE. Given that diagnosis, say what must be "
        "PROTECTED (left alone, because touching it can only cost) and what "
        "must be REVEALED (worth spending the development on), in priority "
        "order. Two or three of each at most; a photograph with six "
        "priorities has none.",
        f"YOUR DIAGNOSIS:\n{said(diagnosis)}",
        _context(data(evidence_text) or {}),
    ])


def structure_prompt(evidence_text: str, strategy: str) -> str:
    evidence = data(evidence_text) or {}
    at = evidence["baseline"]["brightest_at"]
    return "\n\n".join([
        _VOICE,
        "STEP 3 of 4 -- STRUCTURE. Two parts of a frame that need opposite "
        "things need a mask between them. Say which regions this "
        "photograph must be worked on separately, and therefore which "
        "masks. Where something must be protected while everything around "
        "it is revealed, that is one mask and its inverse.",
        "The masks available, written as instructions:\n"
        "  • Radial gradient on the sun, inverted, radius 18%, feather 90%\n"
        "      -- finds the brightest region itself and centres on it; "
        "'inverted' means everywhere except it\n"
        "  • Linear gradient from the bottom, feather 60%  -- the ground, "
        "not the sky\n"
        "  • Luma mask on the shadows / midtones / highlights  -- by "
        "brightness band\n"
        "  • Colour range masks by colour family",
        f"The brightest point sits at {at[0]:.2f} across and {at[1]:.2f} down.",
        f"YOUR STRATEGY:\n{said(strategy)}",
    ])


# The grammar, stated once, because a recipe that does not compile is a
# round of the budget spent on nothing.
_GRAMMAR = """
Write instructions in these sections. Only these compile; anything else
is recorded as unsupported and does nothing.

  global_exposure      Exposure +0.35 | Contrast +12 | Brightness -4 | Saturation -25
  hdr_levels_curves    Highlights -20 | Shadows +8 | Whites +5 | Blacks -30
  white_balance_and_color   Temperature +400 kelvin | Tint +4 | Neutralise
  color_editor         Blue saturation +10 | Green lightness -6 |
                       Monochrome mix: red 50, green 40, blue 10 |
                       Swap red and blue channels
  detail_and_noise     Clarity +8 | Structure +5 | Dehaze +10 |
                       Sharpening amount 40 | Luminance noise reduction 60 |
                       Color noise reduction 60
  layers_and_masks     <mask description>, feather 80%: <effects>
                       effects may be exposure, contrast, brightness,
                       saturation, clarity, structure, highlight, temperature
  finishing_and_output Vignette -10
  composition          Crop to 4:3 | Rotate -0.5

Return the recipe as a JSON object: section name to a list of
instruction strings. Sections you have nothing to say about, leave out.

Two things learned by rendering, which the numbers alone will not tell you:

  • Contrast pivots at middle grey. On a subject sitting at seven
    percent brightness, positive contrast drives it toward black. To
    lift something dark, use exposure, which multiplies.
  • A luma mask on the shadows selects the sky in a night frame,
    because the sky is the shadow. To reach the ground, use a linear
    gradient from the bottom.
"""


def recipe_prompt(evidence_text: str, strategy: str, structure: str,
                  critique: str = "", previous: str = "") -> str:
    parts = [
        _VOICE,
        "STEP 4 of 4 -- WRITE IT. Produce the instructions that carry out "
        "the plan. Protect what you said to protect: do not apply "
        "highlight recovery, exposure reduction or contrast to a region "
        "you have called irrecoverable.",
        f"YOUR STRATEGY:\n{said(strategy)}",
        f"YOUR STRUCTURE:\n{said(structure)}",
        _GRAMMAR,
    ]
    if previous:
        parts.append(f"WHAT YOU WROTE LAST ROUND:\n{said(previous)}")
    if critique:
        parts.append(
            "WHAT THE RENDER SHOWED, AND WHAT YOU SAID TO CHANGE:\n"
            f"{said(critique)}\n\nRevise. Change what the critique named and "
            "leave the rest alone -- a rewrite that moves everything "
            "cannot be judged.")
    parts.append(_context(data(evidence_text) or {}))
    return "\n\n".join(parts)


def critique_prompt(evidence_text: str, strategy: str, before: str,
                    after: str, round_number: int, rounds: int) -> str:
    """Ask what the render actually did -- with the numbers beside it."""
    evidence = data(evidence_text) or {}
    subject = (f" It is an infrared frame at {evidence['cutoff_nm']:.0f}nm."
               if evidence.get("spectrum") == "infrared" else "")
    return "\n\n".join([
        _VOICE + subject,
        f"LOOK AT WHAT YOU DID. Round {round_number} of at most {rounds}.",
        "The photograph below is your own rendering. Beside it are the "
        "measurements of the frame before and after your recipe.",
        "Say what improved, and name any regression with the number that "
        "shows it. Then either state the single most valuable change to "
        "make next, or say the treatment is finished and why. Finishing "
        "early is a good answer; a round spent agreeing with yourself is "
        "not.",
        "Judge against your own strategy, not against a general idea of a "
        "good photograph:\n" + said(strategy),
        "BEFORE:\n" + json.dumps(data(before) or {}, indent=2),
        "AFTER:\n" + json.dumps(data(after) or {}, indent=2),
    ])


# --- turning an answer into something that renders ----------------------

def _bounded(name: str, value: Any) -> dict[str, Any] | None:
    """One named move as an operation, or nothing where it is not one."""
    if name not in MOVES or value in (None, ""):
        return None
    op, low, high, unit = MOVES[name]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if abs(number) < 1e-9:
        return None
    return {"op": op, "value": max(low, min(high, number)), "unit": unit,
            "mode": "absolute" if unit == "percent" and op.startswith(
                "detail.denoise") else "delta",
            "source": f"{name} {number:g}"}


def _anchor_for(mask: dict[str, Any]) -> str:
    """The sentence the engine's mask reader understands, built from numbers."""
    shape = str(mask.get("shape") or "radial").strip().lower()
    parts = [f"{shape} gradient"]
    if shape == "radial":
        if mask.get("centre_x") is not None and mask.get("centre_y") is not None:
            parts.append(
                f"at {float(mask['centre_x']):.0f}%, {float(mask['centre_y']):.0f}%")
        else:
            parts.append("on the sun")
        parts.append(f"radius {float(mask.get('radius') or 18):.0f}%")
    else:
        parts.append(f"from the {str(mask.get('anchor') or 'bottom').strip()}")
    if mask.get("inverted"):
        parts.append("inverted")
    parts.append(f"feather {float(mask.get('feather') or 80):.0f}%")
    return ", ".join(parts)


def assemble_recipe(photo: str, plan: Any, masks: list[str],
                    source_kind: str = "raw") -> str:
    """Build the recipe from what was answered, rather than from prose.

    The first shape of this asked a model to write instructions in the
    compiler's grammar, and then compiled them: a live round produced
    two operations out of a page of intent, and three rounds produced
    none at all. Numbers cannot half-compile. What arrives here is
    bounded to the ranges the renderer accepts and assembled in a fixed
    order -- whole frame first, then each mask -- so the same answers
    always make the same photograph.
    """
    settled = data(plan) or {}
    operations: list[dict[str, Any]] = []
    whole = data(settled.get("global_adjustments")) or {}
    if isinstance(whole, dict):
        for name in MOVES:
            made = _bounded(name, whole.get(name))
            if made:
                operations.append(made)
    for item in masks:
        mask = data(item) or {}
        if not isinstance(mask, dict):
            continue
        effects = data(mask.get("adjustments")) or {}
        inside = [made for made in
                  (_bounded(name, effects.get(name)) for name in MASK_MOVES)
                  if made]
        if not inside:
            continue
        shape = str(mask.get("shape") or "radial").strip().lower()
        if shape not in {"radial", "linear", "luma"}:
            shape = "radial"
        operations.append({
            "op": f"mask.{shape}",
            "value": {"anchor": _anchor_for(mask), "opacity": 1.0,
                      "feather": float(mask.get("feather") or 80) / 100.0,
                      "effects": [{k: v for k, v in made.items()
                                   if k != "source"} for made in inside]},
            "unit": "mask", "mode": "absolute",
            "source": str(mask.get("region") or "a masked region"),
        })
    return json.dumps({
        "format": "opencull-development-recipe-v1",
        "source_photo": str(photo), "source_kind": source_kind,
        "style": PRESET_STYLE,
        "title": str(settled.get("title") or "Protect then reveal")[:120],
        "intent": said(settled.get("strategy") or settled.get("reveal") or "")[:600],
        "working_space": "scene-linear-rec2020-d65",
        "operations": operations, "guardrails": [], "diagnostics": [],
        "coverage": {"instructions": len(operations),
                     "executable": len(operations),
                     "guardrails": 0, "unsupported": 0},
    }, sort_keys=True)


def mask_count(plan: Any) -> int:
    """How many regions this photograph asked to be treated separately."""
    settled = data(plan) or {}
    try:
        return max(0, min(3, int(float(settled.get("mask_count") or 0))))
    except (TypeError, ValueError):
        return 0


# The sections the compiler reads. Anything else a model invents is
# dropped here rather than carried to the compiler to be ignored there.
SECTIONS = (
    "base_and_lens", "composition", "global_exposure", "hdr_levels_curves",
    "white_balance_and_color", "color_editor", "detail_and_noise",
    "layers_and_masks", "finishing_and_output", "evaluation_order",
)


def recipe_sections(answer: Any) -> str:
    """The instruction sections out of a model's answer, as recipe JSON.

    The answer arrives as text holding JSON, the way edit directions do.
    A model that returns something else -- a bare list, a section that is
    one string rather than a list of them -- is met halfway rather than
    failed, because the round is already paid for.
    """
    # The instructions arrive as text holding JSON, in a field of the
    # answer -- or, when a model has ignored that, as the answer itself.
    record = as_record(answer)
    # The field may hold the JSON text the schema asks for, or the object
    # itself where the runtime has already parsed it. Stringifying a
    # mapping before trying to read it is how three rounds of a live run
    # came back empty: repr is not JSON.
    carried = record.get("recipe")
    if carried:
        inner = as_record(carried)
        if inner:
            record = inner
    sections: dict[str, list[str]] = {}
    for key, value in record.items():
        name = str(key).strip().lower()
        if name not in SECTIONS:
            continue
        steps = ([value] if isinstance(value, str)
                 else list(value) if isinstance(value, list) else [])
        kept = [str(item).strip() for item in steps if str(item).strip()]
        if kept:
            sections[name] = kept
    return json.dumps(sections, sort_keys=True)


def compiled_treatment(photo: str, answer: Any, sections: str,
                       source_kind: str = "raw") -> str:
    """Compile a round's instructions, or say why they will not compile."""
    try:
        recipe = compile_recipe(
            photo, PRESET_STYLE, field(answer, "title", "Treatment")[:120],
            field(answer, "intent")[:600],
            data(sections) or {}, "", source_kind)
    except (RecipeCompileError, ValueError) as exc:
        return json.dumps({"error": str(exc), "operations": []})
    return json.dumps(recipe, sort_keys=True)


def treatment_usable(recipe_text: str) -> bool:
    """A round is usable when something in it will actually happen."""
    try:
        recipe = data(recipe_text)
    except ValueError:
        return False
    return bool(recipe.get("operations")) and not recipe.get("error")


def unsupported_note(recipe_text: str) -> str:  # noqa: D401
    """What the compiler could not execute, for the next round to hear."""
    recipe = data(recipe_text) or {}
    missed = [item.get("instruction", "") for item in
              recipe.get("diagnostics", []) if isinstance(item, dict)]
    if not missed:
        return ""
    return ("These instructions did not compile and did nothing; either "
            "write them in the grammar or drop them: "
            + "; ".join(missed[:8]))


# --- rendering the attempt, which is the point ---------------------------

def render_round(photos: str, photo: str, recipe_text: str, directory: str,
                 records: list[str], maximum: float = 1400) -> str:
    """Render one round's recipe and file the picture beside its round.

    This is what makes the program a development rather than a
    suggestion: the next question is asked of the answer to the last
    one. Rendered at proof size, because the loop pays for it several
    times and nothing here is judged on resolution.
    """
    from opencull_gui.development import DevelopmentWorkspace
    from opencull_gui.project import (
        ensure_project_layout,
        load_or_create_folder_project,
    )
    from opencull_gui.raw_sources import RawSourceStore
    from opencull_gui.report import load_report

    root = Path(str(photos)).expanduser().resolve()
    layout = ensure_project_layout(root)
    # The folder's own cull, which is what pairs a frame with its raw and
    # tells the workspace what it is looking at.
    reports = sorted(layout["Reports"].glob("*-results.json"))
    if not reports:
        raise ValueError(f"no culling report under {layout['Reports']}")
    report = load_report(reports[-1])
    project_path, _project = load_or_create_folder_project(
        root, report.path.stem,
        report.path.with_suffix(".opencull-project.json"))
    workspace = DevelopmentWorkspace(
        project_path, layout,
        RawSourceStore(
            layout["Reports"] / f"{report.path.stem}.raw-source.json", report))
    rendered = workspace.render_prepared(
        str(photo), data(recipe_text) or {}, int(maximum))
    destination = Path(str(directory)) / f"round-{round_number(records)}.jpg"
    destination.write_bytes(Path(rendered).read_bytes())
    return str(destination)


# --- keeping the working -------------------------------------------------

def treatment_directory(photos: str, photo: str, stamp: str) -> str:
    """Where one treatment's rounds are kept, beside the photographs."""
    root = (Path(str(photos)).expanduser().resolve() / ".darkimiya"
            / "Treatments" / f"{Path(str(photo)).stem}.{STRATEGY}.{stamp}")
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def as_record(answer: Any) -> dict[str, Any]:
    """A model's answer as a mapping, however it arrived.

    Three shapes have turned up: a mapping, the JSON text of one, and an
    object with attributes. Which one depends on the runtime and on
    whether the value came fresh or from a memo, and a treatment that
    dies on the difference has thrown away a paid-for answer. So it is
    read defensively in one place instead of everywhere.
    """
    if isinstance(answer, dict):
        return dict(answer)
    if isinstance(answer, str):
        try:
            parsed = json.loads(answer)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    fields = getattr(answer, "__dict__", None)
    if isinstance(fields, dict) and fields:
        return dict(fields)
    return {}


def said(value: Any) -> str:
    """One step's answer, written out for the next step to read.

    A previous answer arrives as a mapping, or a list, or text, and a
    prompt that concatenates it has to survive all three. Rendered as
    lines rather than as JSON because the reader is a model being shown
    its own reasoning, not a parser.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            lines.append(f"{str(key).replace('_', ' ')}: {said(item)}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        return "\n".join(f"  - {said(item)}" for item in value)
    return str(value)


def data(value: Any) -> Any:
    """A value from the program, as data, whatever it arrived as.

    Values cross this boundary as mappings, as the JSON text of them, or
    as objects, and which one depends on the runtime and on whether the
    value came fresh or from a memo. Three live runs died one call site
    at a time on that difference, each having already paid for the
    answer it then threw away. So nothing here assumes.
    """
    if isinstance(value, str):
        try:
            return json.loads(value or "null")
        except ValueError:
            return None
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return value
    return as_record(value)


def field(answer: Any, name: str, default: str = "") -> str:
    """One field of a model's answer, or the default where it said nothing."""
    record = as_record(answer)
    if name in record:
        return str(record.get(name) or default)
    return str(getattr(answer, name, default) or default)


def keep_reasoning(directory: str, name: str, answer: Any) -> bool:
    """Write one reasoning step down, and say whether it said anything.

    Written before it is judged, and by the kernel rather than by an
    act, because the alternative is what happened the first time this
    ran: a model answered, the answer failed a check, and the run died
    without keeping the thing that had just been paid for.
    """
    record = as_record(answer) or {
        key: field(answer, key)
        for key in ("subject", "condition", "irrecoverable", "protect",
                    "reveal", "order", "regions", "masks", "rationale")}
    stated = any(str(value).strip() for value in record.values())
    if not stated:
        # Nothing readable. Keep what did arrive, so the next run has
        # something to look at rather than an empty file and a guess.
        record = {"unreadable": repr(answer)[:2000],
                  "type": type(answer).__name__}
    path = Path(str(directory)) / f"{name}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True),
                    encoding="utf-8")
    return stated


def now_stamp() -> str:
    """A name for this run that sorts by when it happened."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def round_number(records: list[str]) -> int:
    return len(records) + 1


def keep_going(records: list[str], rounds: float) -> bool:
    """Whether to spend another round.

    Two ways to stop, and the cheaper one is the critique saying so. A
    photographer stops when the print is right, not when the timer ends;
    the budget is there for the case where it never is.
    """
    if len(records) >= int(rounds):
        return False
    if not records:
        return True
    last = data(records[-1]) or {}
    critique = last.get("critique") or {}
    return not bool(critique.get("finished"))


def latest_critique(records: list[str]) -> str:
    """What the last round's look at itself concluded, for the next write."""
    if not records:
        return ""
    last = data(records[-1]) or {}
    critique = last.get("critique") or {}
    if not critique:
        return ""
    parts = [f"Improved: {critique.get('improved', '')}",
             f"Regressed: {critique.get('regressed', '')}",
             f"Change next: {critique.get('next_change', '')}"]
    missed = last.get("unsupported") or ""
    if missed:
        parts.append(missed)
    return "\n".join(part for part in parts if part.strip(" :"))


def latest_sections(records: list[str]) -> str:
    if not records:
        return ""
    return str((data(records[-1]) or {}).get("sections") or "")


def round_record(directory: str, records: list[str], recipe_text: str,
                 sections: str, render: str = "", measurements: str = "",
                 critique: str = "", answer: Any = None) -> str:
    """One round, written down whole.

    Kept because the process should be inspectable, and because round two
    is sometimes the better photograph -- which nobody discovers if only
    the last one survives.
    """
    number = round_number(records)
    record = {
        "round": number,
        "sections": str(sections),
        # What the model actually said, kept whenever the sections come
        # back empty. A round that produced nothing is the round most
        # worth being able to read afterwards.
        "answer": (repr(answer)[:4000]
                   if answer is not None and not json.loads(sections or "{}")
                   else ""),
        "recipe": data(recipe_text) or {},
        "unsupported": unsupported_note(recipe_text),
        "render": str(render),
        "measurements": data(measurements) or {},
        "critique": as_record(critique) or None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    path = Path(str(directory)) / f"round-{number}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True),
                    encoding="utf-8")
    return json.dumps(record, sort_keys=True)


def treatment_report(directory: str, photo: str, evidence_text: str,
                     diagnosis: str, strategy: str, structure: str,
                     rounds: list[str], chosen: int) -> str:
    """The finished treatment: the reasoning, every round, and the choice."""
    kept = [data(item) or {} for item in rounds]
    return json.dumps({
        "format": TREATMENT_FORMAT,
        "strategy": STRATEGY,
        "photo": str(photo),
        "created_at": datetime.now(UTC).isoformat(),
        "evidence": data(evidence_text) or {},
        "reasoning": {
            "diagnosis": said(diagnosis),
            "strategy": said(strategy),
            "structure": said(structure),
        },
        "rounds": kept,
        "chosen_round": int(chosen),
        "directory": str(directory),
        "notice": (
            "Every round is kept. The chosen one is the treatment's own "
            "answer; an earlier round may still be the better photograph."),
    }, indent=2, sort_keys=True)


def best_round(rounds: list[str], target: str = "subject_contrast") -> int:
    """Which round to offer first.

    The critique decides when to stop; this decides which of the kept
    rounds leads. It follows the measurement the strategy is about --
    how far the subject stands above what surrounds it -- and breaks
    ties towards the later round, which has heard more criticism.
    """
    best, score = 0, -math.inf
    for item in rounds:
        record = data(item) or {}
        value = float(record.get("measurements", {}).get(target, 0) or 0)
        if value >= score:
            best, score = int(record.get("round", 0)), value
    return best


# --- what the panel is asked to warrant ---------------------------------

def treatment_valid(report_text: str) -> bool:
    """Deterministic checks, before anybody is asked to vouch for it."""
    try:
        report = data(report_text)
    except ValueError:
        return False
    if not isinstance(report, dict):
        return False
    rounds = report.get("rounds") or []
    if report.get("format") != TREATMENT_FORMAT or not rounds:
        return False
    if not any(item.get("render") for item in rounds):
        return False
    numbers = [int(item.get("round", 0)) for item in rounds]
    if numbers != list(range(1, len(rounds) + 1)):
        return False
    # The plan is kept on each round rather than once at the top: a
    # revision may change the structure, and a treatment that says only
    # what it first thought is not a record of what it did.
    return any(str(item.get("sections", "")).strip() for item in rounds)


def treatment_evidence(report_text: str) -> str:
    """The finished treatment as a judge reads it: the plan and the numbers.

    Deliberately not the pictures. A panel asked to look at a photograph
    will say whether it likes it; a panel given the stated strategy, the
    measurements before and after, and what the treatment claims it did
    can say whether those agree -- which is the only question a warrant
    can honestly answer.
    """
    report = data(report_text) or {}
    rounds = report.get("rounds") or []
    chosen = int(report.get("chosen_round", 0))
    lines = [
        "DETERMINISTIC TREATMENT PROJECTION:",
        json.dumps({
            "strategy": STRATEGY,
            "photo": report.get("photo"),
            "rounds_spent": len(rounds),
            "chosen_round": chosen,
            "reasoning": report.get("reasoning"),
            "before": (report.get("evidence") or {}).get("baseline"),
            "after": next((item.get("measurements") for item in rounds
                           if int(item.get("round", 0)) == chosen), {}),
            "each_round": [
                {"round": item.get("round"),
                 "operations": len((item.get("recipe") or {}).get("operations", [])),
                 "unsupported": item.get("unsupported", ""),
                 "subject_contrast": (item.get("measurements") or {}).get(
                     "subject_contrast"),
                 "clipped_percent": (item.get("measurements") or {}).get(
                     "clipped_percent"),
                 "critique": (item.get("critique") or {}).get("next_change", ""),
                 "finished": (item.get("critique") or {}).get("finished")}
                for item in rounds],
        }, indent=2, sort_keys=True),
    ]
    return "\n".join(lines)


def treatment_policy(evidence_text: str) -> str:
    """What the panel is being asked to warrant, in one sentence."""
    evidence = data(evidence_text) or {}
    subject = str(evidence.get("about") or "").strip()
    said = f" The photographer says the shoot is: “{subject}”." if subject else ""
    return (
        "a read-only staged development of one photograph under the "
        "protect-then-reveal method: the diagnosis names what is "
        "irrecoverable in the frame; the strategy says what to protect "
        "and what to reveal; the recipe as rendered does not attempt to "
        "recover what the diagnosis called irrecoverable; each round's "
        "measurements are reported beside the round that produced them; "
        "and the chosen round is the one whose measurements best serve "
        "the stated strategy rather than merely the last one tried." + said)


def treatment_abstention(warrant: str) -> str:
    """What the panel refused, exactly as it saw it."""
    return ("THE PANEL DECLINED TO WARRANT THIS TREATMENT.\n\n"
            "The rounds are still on disk, with their renders and "
            "measurements; what is missing is a warrant that the finished "
            "treatment did what it said.\n\nEVIDENCE AS THE PANEL SAW IT:\n"
            + str(warrant))
