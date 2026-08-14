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


# --- the four questions, in order ---------------------------------------

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


def diagnosis_prompt(evidence_text: str) -> str:
    evidence = json.loads(evidence_text)
    return "\n\n".join([
        _VOICE,
        "STEP 1 of 4 -- DIAGNOSE. Say what this photograph is of, what is "
        "wrong with it, and -- this is the part that decides everything "
        "after it -- what about it is beyond recovery.",
        "A clipped light source is not a fault to be fixed. No amount of "
        "highlight recovery invents detail in channels that all saturated; "
        "attempting it turns a shape into a smudge. Name such things "
        "plainly so the rest of the development can stop trying.",
        _context(evidence),
    ])


def strategy_prompt(evidence_text: str, diagnosis: str) -> str:
    return "\n\n".join([
        _VOICE,
        "STEP 2 of 4 -- DECIDE. Given that diagnosis, say what must be "
        "PROTECTED (left alone, because touching it can only cost) and what "
        "must be REVEALED (worth spending the development on), in priority "
        "order. Two or three of each at most; a photograph with six "
        "priorities has none.",
        f"YOUR DIAGNOSIS:\n{diagnosis}",
        _context(json.loads(evidence_text)),
    ])


def structure_prompt(evidence_text: str, strategy: str) -> str:
    evidence = json.loads(evidence_text)
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
        f"YOUR STRATEGY:\n{strategy}",
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
        f"YOUR STRATEGY:\n{strategy}",
        f"YOUR STRUCTURE:\n{structure}",
        _GRAMMAR,
    ]
    if previous:
        parts.append(f"WHAT YOU WROTE LAST ROUND:\n{previous}")
    if critique:
        parts.append(
            "WHAT THE RENDER SHOWED, AND WHAT YOU SAID TO CHANGE:\n"
            f"{critique}\n\nRevise. Change what the critique named and "
            "leave the rest alone -- a rewrite that moves everything "
            "cannot be judged.")
    parts.append(_context(json.loads(evidence_text)))
    return "\n\n".join(parts)


def critique_prompt(evidence_text: str, strategy: str, before: str,
                    after: str, round_number: int, rounds: int) -> str:
    """Ask what the render actually did -- with the numbers beside it."""
    evidence = json.loads(evidence_text)
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
        "good photograph:\n" + strategy,
        "BEFORE:\n" + json.dumps(json.loads(before), indent=2),
        "AFTER:\n" + json.dumps(json.loads(after), indent=2),
    ])


# --- turning an answer into something that renders ----------------------

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
    record: Any = answer
    if isinstance(record, str):
        try:
            record = json.loads(record or "{}")
        except ValueError:
            return json.dumps({})
    if not isinstance(record, dict):
        return json.dumps({})
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


def compiled_treatment(photo: str, title: str, intent: str,
                       sections: str, source_kind: str = "raw") -> str:
    """Compile a round's instructions, or say why they will not compile."""
    try:
        recipe = compile_recipe(
            photo, PRESET_STYLE, str(title)[:120], str(intent)[:600],
            json.loads(sections), "", source_kind)
    except (RecipeCompileError, ValueError) as exc:
        return json.dumps({"error": str(exc), "operations": []})
    return json.dumps(recipe, sort_keys=True)


def treatment_usable(recipe_text: str) -> bool:
    """A round is usable when something in it will actually happen."""
    try:
        recipe = json.loads(str(recipe_text))
    except ValueError:
        return False
    return bool(recipe.get("operations")) and not recipe.get("error")


def unsupported_note(recipe_text: str) -> str:  # noqa: D401
    """What the compiler could not execute, for the next round to hear."""
    recipe = json.loads(str(recipe_text))
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
        str(photo), json.loads(str(recipe_text)), int(maximum))
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
    last = json.loads(str(records[-1]))
    critique = last.get("critique") or {}
    return not bool(critique.get("finished"))


def latest_critique(records: list[str]) -> str:
    """What the last round's look at itself concluded, for the next write."""
    if not records:
        return ""
    last = json.loads(str(records[-1]))
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
    return str(json.loads(str(records[-1])).get("sections") or "")


def round_record(directory: str, records: list[str], recipe_text: str,
                 sections: str, render: str = "", measurements: str = "",
                 critique: str = "") -> str:
    """One round, written down whole.

    Kept because the process should be inspectable, and because round two
    is sometimes the better photograph -- which nobody discovers if only
    the last one survives.
    """
    number = round_number(records)
    record = {
        "round": number,
        "sections": str(sections),
        "recipe": json.loads(recipe_text),
        "unsupported": unsupported_note(recipe_text),
        "render": str(render),
        "measurements": json.loads(measurements) if measurements else {},
        "critique": json.loads(critique) if critique else None,
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
    kept = [json.loads(str(item)) for item in rounds]
    return json.dumps({
        "format": TREATMENT_FORMAT,
        "strategy": STRATEGY,
        "photo": str(photo),
        "created_at": datetime.now(UTC).isoformat(),
        "evidence": json.loads(evidence_text),
        "reasoning": {
            "diagnosis": diagnosis,
            "strategy": strategy,
            "structure": structure,
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
        record = json.loads(str(item))
        value = float(record.get("measurements", {}).get(target, 0) or 0)
        if value >= score:
            best, score = int(record.get("round", 0)), value
    return best


# --- what the panel is asked to warrant ---------------------------------

def treatment_valid(report_text: str) -> bool:
    """Deterministic checks, before anybody is asked to vouch for it."""
    try:
        report = json.loads(str(report_text))
    except ValueError:
        return False
    rounds = report.get("rounds") or []
    if report.get("format") != TREATMENT_FORMAT or not rounds:
        return False
    if not any(item.get("render") for item in rounds):
        return False
    numbers = [int(item.get("round", 0)) for item in rounds]
    if numbers != list(range(1, len(rounds) + 1)):
        return False
    reasoning = report.get("reasoning") or {}
    return all(str(reasoning.get(key, "")).strip()
               for key in ("diagnosis", "strategy", "structure"))


def treatment_evidence(report_text: str) -> str:
    """The finished treatment as a judge reads it: the plan and the numbers.

    Deliberately not the pictures. A panel asked to look at a photograph
    will say whether it likes it; a panel given the stated strategy, the
    measurements before and after, and what the treatment claims it did
    can say whether those agree -- which is the only question a warrant
    can honestly answer.
    """
    report = json.loads(str(report_text))
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
    evidence = json.loads(str(evidence_text))
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
