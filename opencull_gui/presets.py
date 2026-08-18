"""Presets: a look you can reach for, and a look you can keep.

Every treatment in this application so far was written for one
photograph by a model that had looked at it. That is the right default
and it is not the whole of editing. Sometimes the answer is a look you
already know -- a grade you use, a monochrome you like -- and asking a
model to rediscover it frame by frame is slower, costs money, and
arrives slightly different every time.

A preset is that: a fixed set of adjustments, stated once, applied to
anything. It makes no claim about the photograph in front of it. That is
the difference from a treatment, and it is why a preset is never
verified -- there is no claim to check.

Presets work better here than they do in most raw editors, and the
reason is worth writing down. A preset normally lands on whatever the
decoder produced, so the same numbers mean different things on every
frame and the photographer spends the day re-tuning. Here a treatment
starts from the camera-matched baseline: the raw developed and then
matched, per channel, to the rendering the camera made of that same
scene. The frame is normalised before the preset touches it, so a preset
means closer to the same thing across a shoot.

Two kinds live here. The built-in ones are written below in the same
grammar the models write in, and compiled by the same compiler -- so
they are readable, they are checked the way a suggestion is checked, and
there is one grammar in the application rather than two. The saved ones
are the photographer's own, kept outside any project, because a look
belongs to the person and not to one shoot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recipe_compiler import PRESET_STYLE, RecipeCompileError, compile_recipe

from .appdirs import support_dir

PRESET_FORMAT = "darkimiya-preset-v1"
# Where the photographer's own presets are kept, when it is not
# the ordinary place.
PRESETS_ENVIRONMENT = "DARKIMIYA_PRESETS"

# A crop and a rotation belong to one photograph -- they are about where
# its subject is, not about how it should look -- so they are left behind
# when a tuned treatment is kept as a preset. Applying somebody else's
# crop to a frame it was not made for damages the frame.
UNPORTABLE = ("geometry.",)

# The built-in set, written as the instructions a preset is. Each is one
# thing done clearly rather than a slight variation of its neighbour: a
# wall of near-duplicates is harder to choose from than eight presets
# that disagree with each other.
BUILT_IN: tuple[dict[str, Any], ...] = (
    {
        "slug": "clean-contrast",
        "name": "Clean contrast",
        "intent": "The baseline with its back straightened: a little more "
                  "contrast, the black and white points opened, and enough "
                  "clarity to separate what is in front from what is behind.",
        "instructions": {
            "global_exposure": ["Contrast +12"],
            "hdr_levels_curves": ["Blacks -5", "Whites +5"],
            "detail_and_noise": ["Clarity +8"],
        },
    },
    {
        "slug": "warm-daylight",
        "name": "Warm daylight",
        "intent": "Late-afternoon warmth without the whole frame going "
                  "orange: the temperature moves, the warm colours gain a "
                  "little, and the shadows open so the warmth reaches them.",
        "instructions": {
            "white_balance_and_color": ["Temperature +400 kelvin", "Tint +4"],
            "color_editor": ["Orange saturation +8", "Yellow saturation +5"],
            "hdr_levels_curves": ["Shadows +8"],
        },
    },
    {
        "slug": "cool-shade",
        "name": "Cool shade",
        "intent": "For open shade and overcast light, where the frame comes "
                  "back flat and blue-grey: the blues become deliberate "
                  "rather than accidental, and the highlights come down.",
        "instructions": {
            "white_balance_and_color": ["Temperature -350 kelvin"],
            "color_editor": ["Blue saturation +10", "Cyan saturation +8"],
            "hdr_levels_curves": ["Highlights -8", "Blacks -4"],
            "detail_and_noise": ["Clarity +6"],
        },
    },
    {
        "slug": "monochrome",
        "name": "Monochrome",
        "intent": "Colour removed and the tones asked to carry the "
                  "photograph on their own, with the contrast and local "
                  "separation that asks for.",
        "instructions": {
            "global_exposure": ["Saturation -100", "Contrast +14"],
            "detail_and_noise": ["Clarity +10", "Structure +6"],
        },
    },
    {
        "slug": "monochrome-red-filter",
        "name": "Monochrome, red filter",
        "intent": "The monochrome a red filter makes: skies driven down, "
                  "skin and stone lifted, before the colour is taken away. "
                  "The filtering happens first, which is what makes it a "
                  "filter rather than a tint.",
        "instructions": {
            # Order is the whole trick: these move the brightness of each
            # colour family while there is still colour to move, and the
            # desaturation below then reads those brightnesses as grey.
            "color_editor": [
                "Red lightness +22", "Orange lightness +18",
                "Blue lightness -24", "Cyan lightness -18",
            ],
            "global_exposure": ["Saturation -100", "Contrast +10"],
            "detail_and_noise": ["Clarity +8"],
        },
    },
    {
        "slug": "high-key",
        "name": "High key",
        "intent": "Bright and open, with the shadows lifted off the floor "
                  "and the contrast softened so nothing in the frame reads "
                  "as heavy.",
        "instructions": {
            "global_exposure": ["Exposure +0.35", "Contrast -10"],
            "hdr_levels_curves": ["Shadows +20", "Blacks +10",
                                  "Highlights -6"],
        },
    },
    {
        "slug": "low-key",
        "name": "Low key",
        "intent": "Weight and shadow: the frame settles down, the darks "
                  "close up, and a soft vignette keeps the eye off the "
                  "edges.",
        "instructions": {
            "global_exposure": ["Exposure -0.25", "Contrast +12"],
            "hdr_levels_curves": ["Shadows -16", "Blacks -10", "Whites +4"],
            "finishing_and_output": ["Vignette -14"],
        },
    },
    {
        "slug": "landscape-separation",
        "name": "Landscape separation",
        "intent": "Depth by colour rather than by contrast: the greens, the "
                  "water and the far haze are pulled apart from each other "
                  "so the distance between them can be seen.",
        "instructions": {
            "color_editor": [
                "Green saturation +12", "Green lightness -6",
                "Blue saturation +10", "Cyan saturation +12",
                "Orange saturation +5",
            ],
            "detail_and_noise": ["Clarity +12", "Dehaze +10"],
            "hdr_levels_curves": ["Blacks -4"],
        },
    },
    # --- infrared ---------------------------------------------------
    #
    # These belong with the others rather than in a category of their
    # own, because from the renderer's side they are ordinary presets.
    # What makes infrared work is upstream: an album marked infrared is
    # developed without matching to the camera's own rendering, because
    # past the filter's cut-off the camera does not know what it is
    # looking at. Neutralising is then stated here, as an operation the
    # photographer can see and move, rather than hidden in a decode.
    #
    # How much colour is left depends entirely on the cut-off. At 720nm
    # the channels still disagree enough to swap; at 760nm they are
    # within 3% of each other and at 850nm there is nothing but
    # brightness. So two of these are monochrome by arithmetic, not by
    # preference.
    {
        "slug": "infrared-760-mono",
        "name": "Infrared · 760nm",
        "intent": "For a full-spectrum body behind a 760nm cut filter. "
                  "Past that wavelength the three colour channels record "
                  "almost the same light, so there is no colour to keep: "
                  "the frame is neutralised, taken to grey, and given the "
                  "contrast that infrared foliage and sky need.",
        "instructions": {
            "white_balance_and_color": ["Neutralise"],
            "global_exposure": ["Saturation -100", "Contrast +16"],
            "hdr_levels_curves": ["Blacks -6", "Whites +6", "Shadows +8"],
            "detail_and_noise": ["Clarity +14", "Structure +8"],
        },
    },
    {
        "slug": "infrared-850-mono",
        "name": "Infrared · 850nm",
        "intent": "For an unconverted body behind an 850nm filter, where "
                  "the internal hot mirror is still fighting the filter. "
                  "Deeper than 760nm and much darker, so this lifts harder "
                  "and leans on contrast rather than on colour, of which "
                  "there is none at all at this wavelength.",
        "instructions": {
            "white_balance_and_color": ["Neutralise"],
            "global_exposure": ["Exposure +0.40", "Saturation -100",
                                "Contrast +20"],
            "hdr_levels_curves": ["Shadows +16", "Blacks -8", "Whites +8"],
            "detail_and_noise": ["Clarity +16", "Structure +10",
                                 "Luminance noise reduction 40"],
        },
    },
    {
        "slug": "infrared-720-false-colour",
        "name": "Infrared · 720nm false colour",
        "intent": "The classic Hoya R72 look. At 720nm enough visible red "
                  "still reaches the sensor for the channels to disagree, "
                  "so swapping red and blue turns the infrared-bright "
                  "foliage white and the sky blue. Neutralised first, "
                  "because the swap only means something once the cast it "
                  "would otherwise swap is gone.",
        "instructions": {
            "white_balance_and_color": ["Neutralise"],
            "color_editor": ["Swap red and blue channels"],
            "global_exposure": ["Contrast +10", "Saturation +14"],
            "hdr_levels_curves": ["Shadows +10", "Blacks -4"],
            "detail_and_noise": ["Clarity +10"],
        },
    },
    {
        "slug": "infrared-720-mono",
        "name": "Infrared · 720nm monochrome",
        "intent": "The same R72 frame read as black and white instead: "
                  "for the photographs where the false colour is a "
                  "distraction and the infrared tonality is the subject.",
        "instructions": {
            "white_balance_and_color": ["Neutralise"],
            "global_exposure": ["Saturation -100", "Contrast +14"],
            "hdr_levels_curves": ["Shadows +10", "Blacks -6", "Whites +4"],
            "detail_and_noise": ["Clarity +12", "Structure +6"],
        },
    },
    {
        "slug": "infrared-760-atmospheric",
        "name": "Infrared · 760nm atmospheric",
        "intent": "The considered version of a 760nm frame, for a scene "
                  "with sky in it: converted to grey by choosing which "
                  "channel carries the picture rather than by discarding "
                  "colour, the midtones lifted so cloud structure reads, "
                  "the brightest area held back so a clipped sun keeps a "
                  "clean edge, and the skyline left as silhouette.",
        "instructions": {
            "white_balance_and_color": ["Neutralise"],
            # Grey by channel weighting, not by desaturation. At 760nm the
            # three channels differ by sensor response rather than by
            # colour, so which of them carries the picture is a choice --
            # red leads because it is the cleanest at this wavelength, and
            # blue is kept low because it brings the grain and the flare.
            "color_editor": ["Monochrome mix: red 50, green 40, blue 10"],
            # Down, not up. Lifting an infrared frame to a comfortable
            # brightness is what destroys it: at +0.35 EV with the clouds
            # lifted this frame came back at 76 mean with 0.4% of it left
            # dark, which is a milky grey sky and no skyline at all. The
            # silhouette is the photograph.
            "global_exposure": ["Exposure -0.20", "Contrast +18"],
            # One tone equalizer band each: the clouds up, the halo around
            # the sun down, and nothing asked of the silhouette.
            "layers_and_masks": [
                # A restrained lift. At +0.35 the sky came back closer to
                # grey than to charcoal and held only 61% of the frame
                # dark, against 90% in the camera's own rendering.
                "Luma mask on the midtones: exposure +0.20",
                "Luma mask on the highlights: exposure -0.60",
                # A cool tone in the darks only, after the monochrome
                # structure is settled rather than instead of it.
                "Luma mask on the shadows: temperature -400 kelvin",
            ],
            "hdr_levels_curves": ["Blacks -40", "Shadows -24",
                                  "Highlights -20"],
            "detail_and_noise": [
                "Clarity +8", "Structure +5",
                "Luminance noise reduction 35", "Color noise reduction 60",
            ],
        },
    },
    {
        "slug": "infrared-as-shot-corrected",
        "name": "Infrared · as shot, corrected",
        "base": "camera",
        "intent": "The camera's own infrared rendering with the one thing "
                  "wrong with it put right. Measured on a 760nm frame the "
                  "hue is already coherent -- 232 degrees, with four fifths "
                  "of the frame inside a sixteen-degree band -- so there is "
                  "no cast to correct and no temperature to hunt. What is "
                  "wrong is that the sun is blue: a light source that bright "
                  "should read white, and at 27% saturation it does not. "
                  "The highlights are taken to neutral so the sun goes white "
                  "and the glow cools into the blue, and the whole frame "
                  "loses a quarter of its colour, which was loud rather "
                  "than deliberate.",
        "instructions": {
            "global_exposure": ["Saturation -25"],
            "layers_and_masks": [
                "Luma mask on the highlights: saturation -85",
            ],
        },
    },
    {
        "slug": "infrared-760-blue-violet",
        "name": "Infrared · 760nm blue-violet",
        "intent": "The false-colour reading of the same frame, kept "
                  "deliberately restrained: the cast that a 760nm capture "
                  "arrives with is retained rather than corrected, but "
                  "pulled back by about a third, with the brightest cloud "
                  "and the crescent brought towards neutral so the colour "
                  "sits in the middle tones instead of the extremes.",
        "instructions": {
            # No neutralising here: the cast is the subject. What it gets
            # instead is restraint -- the colour is arbitrary in origin,
            # so a lot of it is merely loud.
            # No lift: the skyline is as much the photograph here as it
            # is in the monochrome, and +0.3 EV thinned it from a fifth of
            # the frame to a tenth.
            "global_exposure": ["Saturation -32", "Contrast +10"],
            "layers_and_masks": [
                "Luma mask on the midtones: exposure +0.45",
                # Towards neutral where the frame is brightest: an
                # electric-blue sun is the giveaway of a false-colour
                # frame nobody controlled.
                "Luma mask on the highlights: saturation -45",
                # And out of the shadows, which is where saturated blue
                # stops reading as night and starts reading as a fault.
                "Luma mask on the shadows: saturation -25",
            ],
            "hdr_levels_curves": ["Blacks -20", "Shadows -10",
                                  "Highlights -6"],
            "detail_and_noise": [
                "Clarity +6", "Luminance noise reduction 30",
                "Color noise reduction 70",
            ],
        },
    },
    {
        "slug": "portrait-skin",
        "name": "Portrait skin",
        "intent": "Restraint where it matters: skin kept believable and a "
                  "little brighter, clarity taken off so texture is not "
                  "sharpened into damage, and the background left alone.",
        "instructions": {
            "color_editor": ["Skin saturation -3", "Skin lightness +5"],
            "detail_and_noise": ["Clarity -8"],
            "hdr_levels_curves": ["Shadows +8", "Highlights -5"],
        },
    },
)


class PresetError(ValueError):
    """A preset could not be read, written, or compiled."""


def presets_dir() -> Path:
    """Where a photographer's own presets live.

    Outside every project: a look is the photographer's, not the shoot's,
    and one kept during an Alps edit should be there for the next wedding.
    Naming a folder moves them all -- a shared drive, so two machines edit
    with the same looks, or somewhere a test can write without touching
    the presets of whoever is running it.
    """
    named = os.environ.get(PRESETS_ENVIRONMENT, "").strip()
    if named:
        return Path(named).expanduser()
    return support_dir() / "Presets"


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(name).casefold()).strip("-")
    return cleaned or "preset"


def _compiled(slug: str, name: str, intent: str,
              instructions: dict[str, list[str]]) -> list[dict[str, Any]]:
    """The operations a preset's instructions come to.

    Compiled by the recipe compiler rather than written as operations by
    hand, so a built-in preset is held to exactly the bounds a model's
    answer is held to, and a typo in one is a compile error here instead
    of a strange rendering later.
    """
    try:
        recipe = compile_recipe(
            "", PRESET_STYLE, name, intent, instructions, "", "raw")
    except RecipeCompileError as exc:
        raise PresetError(f"preset {slug!r} will not compile: {exc}") from exc
    unsupported = [
        item["instruction"] for item in recipe.get("diagnostics", [])]
    if unsupported:
        raise PresetError(
            f"preset {slug!r} has instructions the compiler cannot execute: "
            + "; ".join(unsupported))
    if not recipe["operations"]:
        raise PresetError(f"preset {slug!r} compiles to nothing")
    return recipe["operations"]


def built_in() -> list[dict[str, Any]]:
    """The presets that ship with the application."""
    made = []
    for item in BUILT_IN:
        made.append({
            "format": PRESET_FORMAT,
            "id": f"preset-{item['slug']}",
            "name": item["name"],
            "intent": item["intent"],
            "origin": "built-in",
            # Almost every look is developed from the raw, which is the
            # point of having a raw. One is not: where the camera's own
            # rendering is the better starting point -- a steeper
            # highlight rolloff and its own noise reduction, both of
            # which an infrared frame benefits from -- a preset can say
            # so, and then it is correcting a photograph rather than
            # developing a negative.
            "base": str(item.get("base") or "raw"),
            "instructions": item["instructions"],
            "operations": _compiled(
                item["slug"], item["name"], item["intent"],
                item["instructions"]),
        })
    return made


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("format") != PRESET_FORMAT:
        return None
    operations = value.get("operations")
    if not isinstance(operations, list) or not operations:
        return None
    if any(not isinstance(item, dict) for item in operations):
        return None
    value["origin"] = "saved"
    value.setdefault("id", f"saved-{path.stem}")
    value.setdefault("name", path.stem)
    value.setdefault("intent", "")
    return value


def saved(root: Path | None = None) -> list[dict[str, Any]]:
    """The photographer's own presets, oldest first.

    A preset file that cannot be read is skipped rather than raised: one
    bad file in the folder should not take the whole list away, and the
    list is drawn every time the develop page opens.
    """
    folder = Path(root) if root is not None else presets_dir()
    if not folder.is_dir():
        return []
    found = [_read(path) for path in sorted(folder.glob("*.json"))]
    return [item for item in found if item is not None]


def presets(root: Path | None = None) -> list[dict[str, Any]]:
    """Every preset there is: the photographer's own first, then the shipped."""
    return saved(root) + built_in()


def portable_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The part of a treatment that can honestly travel to another frame.

    What the photographer switched off is not part of the look they are
    keeping -- carrying it along as a disabled operation would put an
    instruction into the preset that says to do nothing. A camera's own
    look stays behind too: it belongs to the camera, is laid back under
    every render of its frames, and carried inside a preset it would be
    worn twice at home and wrongly anywhere else.
    """
    return [
        json.loads(json.dumps(item)) for item in operations
        if isinstance(item, dict)
        and item.get("enabled", True) is not False
        and not item.get("camera_look")
        and not item.get("dust_map")
        and not str(item.get("op", "")).startswith(UNPORTABLE)
    ]


def save(name: str, operations: list[dict[str, Any]], intent: str = "",
         origin_note: dict[str, Any] | None = None,
         root: Path | None = None) -> dict[str, Any]:
    """Keep a set of adjustments as a preset of the photographer's own.

    What is kept is the operations, not the prose that produced them: the
    photographer has moved the numbers, and the numbers are what they
    meant. Where those numbers came from is recorded beside them so the
    preset can say what it is a memory of.
    """
    title = " ".join(str(name).split())[:80]
    if not title:
        raise PresetError("a preset needs a name")
    portable = portable_operations(operations)
    if not portable:
        raise PresetError(
            "there is nothing in this version that would mean the same on "
            "another photograph")
    digest = hashlib.sha256(json.dumps(
        portable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    folder = Path(root) if root is not None else presets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{_slug(title)}-{digest[:8]}"
    value = {
        "format": PRESET_FORMAT,
        "id": f"saved-{stem}",
        "name": title,
        "intent": str(intent).strip(),
        "origin": "saved",
        "saved_at": datetime.now(UTC).isoformat(),
        "operations": portable,
    }
    if origin_note:
        value["kept_from"] = origin_note
    destination = folder / f"{stem}.json"
    temporary = destination.with_suffix(".json.writing")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=False), encoding="utf-8")
    temporary.replace(destination)
    return value


def forget(preset_id: str, root: Path | None = None) -> bool:
    """Remove one of the photographer's own presets. Built-ins cannot go."""
    if not str(preset_id).startswith("saved-"):
        return False
    folder = Path(root) if root is not None else presets_dir()
    destination = folder / f"{str(preset_id)[len('saved-'):]}.json"
    if not destination.is_file():
        return False
    destination.unlink()
    return True


def recipe_for(preset: dict[str, Any], photo: str,
               source_kind: str) -> dict[str, Any]:
    """One preset, as a recipe the renderer will execute for one photograph."""
    return {
        "format": "opencull-development-recipe-v1",
        "source_photo": photo,
        "source_kind": source_kind,
        "style": PRESET_STYLE,
        "title": str(preset.get("name") or "Preset"),
        "intent": str(preset.get("intent") or ""),
        "working_space": "scene-linear-rec2020-d65",
        "operations": json.loads(json.dumps(preset["operations"])),
        "guardrails": [],
        "diagnostics": [],
        "coverage": {
            "instructions": len(preset["operations"]),
            "executable": len(preset["operations"]),
            "guardrails": 0, "unsupported": 0,
        },
    }


__all__ = [
    "PRESETS_ENVIRONMENT", "PRESET_FORMAT", "PresetError", "built_in", "forget", "portable_operations",
    "presets", "presets_dir", "recipe_for", "save", "saved",
]
