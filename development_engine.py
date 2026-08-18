"""Deterministic global development engine for compiled recipe IR."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from PIL import Image, ImageEnhance

from colour_profile import srgb_profile

ENGINE_FORMAT = "opencull-development-render-v1"

# Bumped whenever the operations render differently from before. A cached
# proof is keyed by the recipe and the file it came from, neither of which
# changes when the engine's own arithmetic does -- so without this, an
# improvement to the renderer is invisible on every frame already looked
# at, which is exactly the frames somebody is judging it by.
RECIPE_ENGINE_REVISION = 10


class DevelopmentError(ValueError):
    """A recipe or image cannot be safely rendered."""


def apply_adjustment_draft(recipe: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """Return a new recipe with only bounded, named adjustment controls."""
    if not isinstance(recipe, dict) or not isinstance(draft, dict):
        raise DevelopmentError("recipe and adjustment draft must be objects")
    result = json.loads(json.dumps(recipe))
    operations = result.setdefault("operations", [])
    mappings = {
        "midtones": {"op": "tone.exposure", "value": 0.15, "unit": "EV", "mode": "delta"},
        "cyan": {"op": "color.hsl_range", "value": -8.0, "unit": "percent", "mode": "delta",
                 "channel": "teal/blue", "component": "saturation"},
        "shadows": {"op": "tone.shadow", "value": 10.0, "unit": "percent", "mode": "delta"},
        "skin": {"op": "guardrail.skin_tones", "value": True, "unit": "boolean", "mode": "absolute"},
    }
    for change in draft.get("changes", []):
        control = change.get("control") if isinstance(change, dict) else None
        if control in mappings:
            operation = dict(mappings[control])
            operation["source_instruction"] = change.get("instruction", control)
            operations.append(operation)
    result["revision"] = int(result.get("revision", 0)) + 1
    result["adjustment_revision"] = draft.get("revision")
    result["adjustment_note"] = draft.get("note", "")
    return result


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _linear_rec2020_to_srgb(rgb: np.ndarray) -> np.ndarray:
    # Rec.2020 D65 linear -> XYZ D65 -> sRGB linear.
    to_xyz = np.array([
        [0.636958, 0.144617, 0.168881],
        [0.262700, 0.677998, 0.059302],
        [0.000000, 0.028073, 1.060985],
    ], dtype=np.float32)
    to_srgb = np.array([
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ], dtype=np.float32)
    linear = np.tensordot(rgb, to_xyz.T, axes=1)
    linear = np.tensordot(linear, to_srgb.T, axes=1)
    linear = np.maximum(linear, 0.0)
    return np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(np.maximum(linear, 1e-8), 1 / 2.4) - 0.055,
    )


def _srgb_to_linear_rec2020(rgb: np.ndarray) -> np.ndarray:
    """Convert display sRGB back to the engine's scene-linear Rec.2020 space."""
    srgb = np.clip(rgb, 0.0, 1.0)
    linear = np.where(srgb <= 0.04045, srgb / 12.92,
                     ((srgb + 0.055) / 1.055) ** 2.4)
    to_xyz = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=np.float32)
    from_xyz = np.linalg.inv(np.array([
        [0.636958, 0.144617, 0.168881],
        [0.262700, 0.677998, 0.059302],
        [0.000000, 0.028073, 1.060985],
    ], dtype=np.float32))
    xyz = np.tensordot(linear, to_xyz.T, axes=1)
    return np.maximum(np.tensordot(xyz, from_xyz.T, axes=1), 0.0)


def _calibrate_to_jpeg(linear: np.ndarray, reference: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Fit a conservative per-channel display LUT to the camera JPEG.

    The JPEG is the visual domain seen by the edit-direction model. Quantile
    matching avoids pretending that camera JPEG processing is recoverable as
    a physical RAW profile while aligning exposure, tone, and channel balance.
    """
    try:
        with Image.open(reference) as image:
            target = np.asarray(image.convert("RGB").resize(
                (linear.shape[1], linear.shape[0]), Image.Resampling.BILINEAR),
                dtype=np.float32) / 255.0
    except (OSError, ValueError) as exc:
        raise DevelopmentError(f"cannot read calibration JPEG {reference}: {exc}") from exc
    source_display = _linear_rec2020_to_srgb(np.clip(linear, 0, 1))
    knots = _calibration_knots(source_display, target)
    calibrated = _apply_calibration(source_display, knots)
    return _srgb_to_linear_rec2020(calibrated), {
        "reference_path": str(reference), "reference_sha256": _sha256(reference),
        "method": "per-channel-srgb-quantile-lut-with-highlight-rolloff",
        "rolloff": {"holds_below": CALIBRATION_HOLD,
                    "released_above": CALIBRATION_RELEASE},
        "knots": knots,
    }


def _calibration_knots(source_display: np.ndarray,
                       target: np.ndarray) -> list[dict[str, list[float]]]:
    """The per-channel quantile LUT, fitted from sampled pixels.

    Split from the application so a windowed render can fit the LUT on
    the WHOLE frame and apply it to the crop: quantiles of a crop are a
    different match, and the seam would show the moment the full render
    swapped in.
    """
    sample_source = source_display[::16, ::16]
    sample_target = target[::16, ::16]
    quantiles = np.linspace(0.0, 1.0, 33)
    knots: list[dict[str, list[float]]] = []
    for channel in range(3):
        source_knots = np.quantile(sample_source[..., channel], quantiles)
        target_knots = np.quantile(sample_target[..., channel], quantiles)
        source_knots, unique = np.unique(source_knots, return_index=True)
        target_knots = target_knots[unique]
        knots.append({"source": source_knots.tolist(),
                      "target": target_knots.tolist()})
    return knots


def _apply_calibration(source_display: np.ndarray,
                       knots: list[dict[str, list[float]]]) -> np.ndarray:
    """The fitted LUT laid on pixels, held in tone, released in light.

    The match is what the photographer wanted from the camera: its tones
    and its colour. What they did not want is its clipping. A camera
    rendering has already spent the highlights the raw still holds, so
    matching it all the way to white throws that headroom away --
    measured at 12.9% of one frame blown rising to 21.2%. The match
    therefore holds through the shadows and midtones and fades out
    towards the top, leaving the brightest tones where the decode put
    them, with detail still in them to be worked on.
    """
    calibrated = np.empty_like(source_display)
    for channel in range(3):
        calibrated[..., channel] = np.interp(
            source_display[..., channel],
            np.asarray(knots[channel]["source"]),
            np.asarray(knots[channel]["target"]))
    luminance = source_display @ _LUMA
    weight = 1.0 - _ramp(
        (luminance - CALIBRATION_HOLD) /
        max(CALIBRATION_RELEASE - CALIBRATION_HOLD, 1e-6))
    return source_display + (
        calibrated - source_display) * weight[..., None]


def _load_linear(path: Path) -> np.ndarray:
    try:
        array = np.asarray(tifffile.imread(path))
    except (OSError, ValueError) as exc:
        raise DevelopmentError(f"cannot read baseline image {path}: {exc}") from exc
    if array.ndim == 2:
        array = np.repeat(array[..., None], 3, axis=2)
    if array.ndim != 3 or array.shape[2] < 3:
        raise DevelopmentError("baseline must contain at least three channels")
    original_dtype = array.dtype
    array = array[..., :3].astype(np.float32)
    scale = 65535.0 if np.issubdtype(original_dtype, np.integer) else 1.0
    if original_dtype == np.uint8:
        scale = 255.0
    return np.clip(array / scale, 0.0, None)


# --- where a slider means what it says -------------------------------------
#
# The recipes are written in the vocabulary of a develop program: Contrast
# +17, Black -13, Saturation +5. Those sliders act on a display-referred
# image. Applied instead to scene-linear light -- which is what this engine
# holds -- the same numbers behave nothing like their names: a contrast
# raise multiplies the distance between channels, so it inflates colour
# rather than tone, and a small black offset lands where linear values are
# tiny and crushes whole shadows to nothing. Measured on a Fujifilm frame,
# a recipe that asked to *reduce* blue, yellow and orange saturation raised
# all three (+18, +14, +32 points) and crushed 6% of the frame to black.
#
# So tonal and colour sliders are applied through a gamma encoding, where
# midtones sit near the middle and the numbers mean what a photographer
# means by them. Exposure and white balance stay in linear light, because
# those two are physical and belong there.

# Where the camera match gives way to the raw's own highlights: full
# strength up to the first, gone by the second, smoothly in between.
CALIBRATION_HOLD = 0.55
CALIBRATION_RELEASE = 0.92

_ENCODE_GAMMA = 2.2

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Tonal moves act on brightness, not on colour. Applied to each channel
# separately they pull the channels apart, so a contrast raise silently
# undoes a colour instruction that asked for less saturation -- which is
# how a recipe that said "reduce blue by 8" produced a bluer sky. These
# are applied to luminance, and the channels follow it in proportion.
_LUMA_OPS = frozenset({
    "tone.contrast", "tone.brightness", "tone.highlight", "tone.shadow",
    "tone.white", "tone.black", "levels.white_input", "levels.black_input",
    "detail.clarity", "detail.structure", "detail.dehaze",
})

# The operations whose numbers come from display-referred sliders.
_DISPLAY_OPS = frozenset({
    "tone.contrast", "tone.brightness", "tone.highlight", "tone.shadow",
    "tone.white", "tone.black", "color.saturation", "color.hsl_range",
    "detail.clarity", "detail.structure", "detail.dehaze",
    "detail.sharpen_amount", "detail.denoise_luminance",
    "detail.denoise_color", "levels.white_input", "levels.black_input",
    "levels.midpoint", "finish.vignette",
})

# Detail work, at the scale a bounded proof is drawn at. A develop
# program's sharpening amount runs to several hundred; taken literally as
# a multiplier that would tear the frame apart, so it is read as a
# proportion of a restrained unsharp mask.
# Detail radii are written for a proof-sized frame and scaled to whatever
# is actually being rendered. A radius fixed in pixels is a different
# adjustment at every size: one pixel of unsharp mask covers nearly three
# times less of a full-size frame than of the proof it was approved on,
# so the delivery came back softer than the picture the photographer
# said yes to -- measured at 123.6 units of edge energy in the proof
# against 105.6 in the render.
DETAIL_REFERENCE_EDGE = 1600
SHARPEN_RADIUS = 1.0
SHARPEN_STRENGTH = 0.35
DENOISE_RADIUS = 1.4
DENOISE_STRENGTH = 0.5
DENOISE_COLOR_STRENGTH = 0.9
VIGNETTE_DEPTH = 0.35


def _encoded(linear: np.ndarray) -> np.ndarray:
    """Scene-linear light as a display-referred image, same primaries."""
    return np.clip(linear, 0.0, None) ** (1.0 / _ENCODE_GAMMA)


def _decoded(display: np.ndarray) -> np.ndarray:
    return np.clip(display, 0.0, None) ** _ENCODE_GAMMA


def _ramp(value: np.ndarray) -> np.ndarray:
    """A smooth 0..1 ramp, so a tonal move has no visible edge."""
    clipped = np.clip(value, 0.0, 1.0)
    return clipped * clipped * (3.0 - 2.0 * clipped)



def _blur(image: np.ndarray, radius: float) -> np.ndarray:
    """A separable Gaussian, wide enough for detail work and no wider."""
    radius = max(float(radius), 0.1)
    span = max(int(radius * 3), 1)
    offsets = np.arange(-span, span + 1, dtype=np.float32)
    kernel = np.exp(-(offsets ** 2) / (2.0 * radius * radius))
    kernel /= kernel.sum()
    padded = np.pad(image, ((span, span), (span, span), (0, 0)), mode="edge")
    across = np.zeros_like(image)
    for index, weight in enumerate(kernel):
        across += padded[span:span + image.shape[0],
                         index:index + image.shape[1]] * weight
    padded = np.pad(across, ((span, span), (0, 0), (0, 0)), mode="edge")
    down = np.zeros_like(image)
    for index, weight in enumerate(kernel):
        down += padded[index:index + image.shape[0]] * weight
    return down


def _named(item: dict[str, Any]) -> str:
    """What an adjustment is, in enough detail to be worth reading.

    "colour" says less than "blue saturation", and the colour family is
    the part a photographer recognises as their own work.
    """
    op = str(item.get("op", ""))
    if op == "color.hsl_range":
        channel = str(item.get("channel", "")).strip()
        component = str(item.get("component", "saturation")).strip()
        return f"{op}:{channel}:{component}" if channel else op
    if op.startswith("mask."):
        return f"mask:{op[5:]}"
    return op


def active(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The adjustments that are actually meant to happen.

    The fine-tuning page lets a photographer switch one off, and that was
    recorded on the operation and then ignored here -- the switch moved,
    the label said the operation was off, and the renderer applied it
    anyway. An operation nobody wants is not applied and is not counted,
    so the progress bar does not name work that is not being done either.
    """
    return [item for item in operations
            if isinstance(item, dict) and item.get("enabled", True) is not False]


def _apply_global(rgb: np.ndarray, operations: list[dict[str, Any]],
                  progress: Any = None,
                  window: dict[str, float] | None = None) -> np.ndarray:
    """Apply a recipe's adjustments in order.

    ``progress`` is called with (done, total, what) after each one. A
    treatment is twenty-odd named adjustments and the photographer is
    entitled to watch them arrive rather than watch a bar that cannot say
    anything until the whole thing is finished.

    ``window`` says these pixels are a crop of a larger frame -- x, y,
    w, h as fractions -- so everything that speaks in frame coordinates
    (masks, spots, the vignette, detail radii) behaves as if the whole
    photograph were here. Blur-based detail work differs slightly at
    the crop's border, where the context it would have read is absent.
    """
    result = np.array(rgb, dtype=np.float32, copy=True)
    frame_edge = float(max(result.shape[:2]))
    if window:
        frame_edge = max(result.shape[0] / max(float(window["h"]), 1e-6),
                         result.shape[1] / max(float(window["w"]), 1e-6))
    detail_scale = max(frame_edge / DETAIL_REFERENCE_EDGE, 0.25)
    operations = active(operations)
    total = len(operations)
    for done, item in enumerate(operations, start=1):
        op = item.get("op")
        value = item.get("value")
        # Structured local operations must be handled before the scalar-value
        # guard.  Their value is a mask descriptor, not a number.
        if op == "color.neutralize":
            # White balance where it can still be done honestly.
            #
            # The obvious place is darktable's own raw coefficients, and
            # that was tried: on this Sony it drove red from 45 to 1 and
            # saturation to 99%, because those multipliers sit upstream of
            # a camera colour matrix that has no meaning for a photograph
            # taken past 760nm. The matrix is built for light the filter
            # removed.
            #
            # So it is done here instead, on the developed frame, by the
            # oldest method there is: make the channel averages agree.
            # For an infrared frame that is exactly the classic move of
            # white balancing off the foliage, because past the cut-off
            # the foliage is most of what the sensor saw.
            strength = min(max(float(value) / 100.0, 0.0), 1.0)
            averages = result.reshape(-1, 3).mean(axis=0)
            target = float(averages.mean())
            gains = np.where(averages > 1e-6, target / np.maximum(averages, 1e-6), 1.0)
            result = result * (1.0 + (gains - 1.0) * strength).astype(np.float32)
            if progress is not None:
                progress(done, total, _named(item))
            continue
        if op == "color.channel_mixer" and isinstance(value, list):
            # A channel mixer is a matrix, and a matrix belongs in linear
            # light: mixing gamma-encoded numbers mixes the encoding along
            # with the colour. Handled here, above the scalar guard,
            # because its value is nine numbers rather than one.
            #
            # This is the operation infrared work turns on. Past a 720nm
            # filter the red channel holds the foliage and the blue holds
            # what little sky there is, and swapping them is what makes
            # the false colour a person can actually read.
            matrix = np.asarray(value, dtype=np.float32)
            if matrix.shape == (3, 3):
                result = np.maximum(
                    np.einsum("ij,...j->...i", matrix, result), 0.0)
            if progress is not None:
                progress(done, total, _named(item))
            continue
        if op == "heal.spots" and isinstance(value, dict):
            # Sensor dust and lens spots, taken back out: each spot is a
            # small disc rebuilt from its own boundary, feathered so the
            # repair has no edge. Coordinates are fractions of the frame
            # -- the same words at proof size and at full size -- and a
            # frame with no listed spots is untouched.
            result = _heal_spots(result, value, window)
            if progress is not None:
                progress(done, total, _named(item))
            continue
        if op == "color.warp" and isinstance(value, dict):
            # A smooth warp of colour space itself: a coarse lattice of
            # deltas, trilinearly interpolated, applied in the encoded
            # domain where colour is judged. This is the shape of a
            # camera look -- the profile idea from the Capture One
            # study -- and its smoothness is by construction: a 9-cubed
            # lattice cannot hold a cliff, only slopes.
            result = _colour_warp(result, value)
            if progress is not None:
                progress(done, total, _named(item))
            continue
        if op == "tone.curve" and isinstance(value, dict):
            # The curve is a display-referred instrument: it is drawn
            # against the picture as shown, so it runs on the encoded
            # values and the result is decoded back.
            #
            # Two readings of the same drawing. The classic RGB curve
            # runs identically on all three channels: contrast separates
            # the channels, which saturates and bends hue -- the film
            # look, bought the way film bought it. The luma reading runs
            # the curve on luminance alone and scales the channels by
            # the same ratio, so a pixel gets lighter or darker without
            # changing what colour it is. "preserve" is the dial between
            # them: 0 is all film, 100 is all faithful.
            points = value.get("points") or []
            if len(points) >= 2:
                shown = np.clip(_encoded(np.clip(result, 0.0, None)),
                                0.0, 1.0)
                lut = _curve_lut(points)
                keep = min(max(float(value.get("preserve", 0) or 0), 0.0),
                           100.0) / 100.0
                curved = None
                if keep < 1.0:
                    curved = np.interp(
                        shown * 255.0, np.arange(256), lut
                    ).astype(np.float32)
                if keep > 0.0:
                    luma = (shown[..., 0] * 0.2126 + shown[..., 1] * 0.7152
                            + shown[..., 2] * 0.0722)
                    lifted = np.interp(
                        luma * 255.0, np.arange(256), lut
                    ).astype(np.float32)
                    ratio = lifted / np.maximum(luma, 1e-4)
                    held = np.clip(
                        shown * ratio[..., None], 0.0, 1.0)
                    curved = (held if curved is None
                              else curved * (1.0 - keep) + held * keep)
                result = _decoded(np.clip(curved, 0.0, 1.0))
            if progress is not None:
                progress(done, total, _named(item))
            continue
        if op.startswith("mask.") and isinstance(value, dict):
            mask = _spatial_mask(result, op[5:], value, window)
            opacity = float(value.get("opacity", 1.0))
            blend = np.clip(mask * opacity, 0, 1)[..., None]
            for effect in value.get("effects", []):
                effect_name = str(effect.get("op", "")) if isinstance(
                    effect, dict) else ""
                if effect_name.startswith("uniformity."):
                    adjusted = _uniformity(result, effect, value)
                else:
                    adjusted = _apply_global(result, [effect])
                result = result * (1 - blend) + adjusted * blend
            if progress is not None:
                progress(done, total, _named(item))
            continue
        if not isinstance(value, (int, float)):
            if progress is not None:
                progress(done, total, _named(item))
            if op in {"lens.profile", "lens.chromatic_aberration"}:
                # Not skipped work: the decoder applies the camera's lens
                # profile and its chromatic-aberration correction while
                # demosaicing, before this engine sees the frame. Doing it
                # again here would correct an already corrected picture.
                continue
            if op in {"geometry.crop_aspect", "output.color_space", "color.hsl_range"}:
                continue
            continue
        value = float(value)
        display = op in _DISPLAY_OPS
        if display:
            result = _encoded(result)
        tonal = op in _LUMA_OPS
        if tonal:
            colour = result
            result = (result * _LUMA).sum(axis=2, keepdims=True)
        if op == "tone.exposure":
            result *= 2.0 ** value
        elif op == "tone.contrast":
            result = (result - 0.5) * (1.0 + value / 100.0) + 0.5
        elif op == "tone.brightness":
            # A midtone lift that leaves the two ends where they are, which
            # is what the slider does and why it is not exposure.
            weight = 1.0 - np.abs(2.0 * np.clip(result, 0.0, 1.0) - 1.0)
            result += weight * (value / 100.0) * 0.12
        elif op == "tone.highlight":
            if value < 0:
                mask = _ramp((result - 0.60) / 0.40)
                result -= mask * (-value / 100.0) * 0.30
            else:
                mask = _ramp((result - 0.60) / 0.40)
                result += mask * (value / 100.0) * 0.30
        elif op == "tone.shadow":
            if value != 0:
                mask = _ramp((0.45 - result) / 0.45)
                result += mask * (value / 100.0) * 0.30
        elif op == "tone.white":
            # The white point moves; everything below it follows in
            # proportion, so nothing is pushed through the ceiling.
            result *= 1.0 + value / 100.0 * 0.15
        elif op == "tone.black":
            floor = -value / 100.0 * 0.06
            if floor > 0:
                result = (result - floor) / max(1.0 - floor, 1e-4)
            elif floor < 0:
                result = -floor + result * (1.0 + floor)
        elif op == "levels.white_input":
            result /= max(value / 255.0, 1e-4)
        elif op == "levels.black_input":
            result = (result - value / 255.0) / max(1 - value / 255.0, 1e-4)
        elif op == "color.saturation":
            lum = (result * np.array([0.2126, 0.7152, 0.0722])).sum(axis=2, keepdims=True)
            result = lum + (result - lum) * (1.0 + value / 100.0)
        elif op == "color.temperature":
            # A delta warms or cools from where the frame is. An absolute
            # kelvin names the light itself, and is read against the D65
            # base every reference here is developed to -- 5400 on a D65
            # frame asks for a cooler rendering, the way a develop
            # module's temperature slider reads. Treated as a no-op
            # before, an absolute temperature (which is how the compiler
            # spells any kelvin of 2000 or more, including its own
            # "white balance 5500K" example) silently changed nothing,
            # and the fine-tune slider on such an operation was dead.
            told = (value if item.get("mode") == "delta"
                    else value - 6500.0)
            # Bounded: the compiler admits absolutes to 50000K, and an
            # unbounded factor would push channels negative or absurd.
            factor = min(max(1.0 + told / 5000.0, 0.2), 5.0)
            result[..., 0] *= factor
            result[..., 2] /= max(factor, 0.01)
        elif op == "color.tint":
            result[..., 1] *= 1.0 - value / 200.0
        elif op == "detail.clarity":
            # Global clarity is represented conservatively as local-contrast
            # contrast; true masks and edge-aware detail are later phases.
            result = (result - 0.18) * (1.0 + value / 300.0) + 0.18
        elif op == "detail.structure":
            result = (result - 0.18) * (1.0 + value / 500.0) + 0.18
        elif op == "detail.dehaze":
            result = (result - 0.18) * (1.0 + value / 180.0) + 0.18
        elif op == "color.hsl_range":
            channel = str(item.get("channel", "all")).casefold()
            component = str(item.get("component", "saturation")).casefold()
            mask = _hue_mask(result, channel)
            if component == "hue":
                # Hue-shift is intentionally small and bounded; full color
                # science belongs in the managed color stage.
                result = _rotate_hue(result, value, mask)
            else:
                lum = (result * np.array([0.2126, 0.7152, 0.0722])).sum(
                    axis=2, keepdims=True)
                if component == "lightness":
                    result += mask[..., None] * (value / 100.0) * 0.10
                else:
                    result = np.where(
                        mask[..., None],
                        lum + (result - lum) * (1.0 + value / 100.0),
                        result)
        elif op == "detail.sharpen_amount":
            # An unsharp mask on brightness only: sharpening colour is how
            # edges pick up fringes that were never in the photograph.
            lum = (result * _LUMA).sum(axis=2, keepdims=True)
            detail = lum - _blur(lum, SHARPEN_RADIUS * detail_scale)
            result = result + detail * (value / 100.0) * SHARPEN_STRENGTH
        elif op == "detail.denoise_luminance":
            lum = (result * _LUMA).sum(axis=2, keepdims=True)
            softened = _blur(lum, DENOISE_RADIUS * detail_scale)
            weight = min(max(value / 100.0, 0.0), 1.0) * DENOISE_STRENGTH
            result = result + (softened - lum) * weight
        elif op == "detail.denoise_color":
            # Chroma only: the colour speckle goes, the detail stays,
            # which is the whole reason the two are separate controls.
            lum = (result * _LUMA).sum(axis=2, keepdims=True)
            chroma = result - lum
            weight = min(max(value / 100.0, 0.0), 1.0) * DENOISE_COLOR_STRENGTH
            result = lum + chroma + (
                _blur(chroma, DENOISE_RADIUS * detail_scale) - chroma) * weight
        elif op == "detail.clean_colour":
            result = _clean_colour(result, value, detail_scale, window)
        elif op == "finish.grain":
            result = _film_grain(result, value, window)
        elif op == "levels.midpoint":
            # The levels midpoint is a gamma about the middle of the scale.
            if value > 0:
                result = np.clip(result, 0.0, None) ** (1.0 / max(value, 0.1))
        elif op == "finish.vignette":
            yy, xx = _frame_grid(
                result.shape[0], result.shape[1], window)
            edge = np.minimum.reduce([xx, 1 - xx, yy, 1 - yy])
            fall = np.clip(1.0 - edge * 4.0, 0.0, 1.0)[..., None]
            result = result * (1.0 + (value / 100.0) * VIGNETTE_DEPTH * fall)
        elif op.startswith("mask."):
            if op == "mask.vignette":
                yy, xx = np.indices(result.shape[:2], dtype=np.float32)
                yy /= max(result.shape[0] - 1, 1)
                xx /= max(result.shape[1] - 1, 1)
                edge = np.minimum.reduce([xx, 1 - xx, yy, 1 - yy])
                result *= 1.0 + value / 100.0 * np.clip(1 - edge * 5, 0, 1)[..., None]
        if tonal:
            # The channels keep their ratios to one another; only their
            # common brightness has moved.
            before = (colour * _LUMA).sum(axis=2, keepdims=True)
            result = colour * (result / np.maximum(before, 1e-5))
        if display:
            result = _decoded(result)
        if progress is not None:
            progress(done, total, _named(item))
    return np.maximum(result, 0.0)


def _hue_mask(rgb: np.ndarray, channel: str) -> np.ndarray:
    channel = channel.replace(" range", "")
    channels = {part.strip() for part in channel.split("/") if part.strip()}
    if not channels or "all" in channels:
        return np.ones(rgb.shape[:2], dtype=np.float32)
    r, g, b = [np.clip(rgb[..., i], 0, None) for i in range(3)]
    mx, mn = np.maximum.reduce([r, g, b]), np.minimum.reduce([r, g, b])
    delta = mx - mn
    hue = np.zeros_like(mx)
    nonzero = delta > 1e-6
    red = (mx == r) & nonzero
    green = (mx == g) & nonzero
    blue = (mx == b) & nonzero
    hue[red] = ((g[red] - b[red]) / delta[red]) % 6
    hue[green] = (b[green] - r[green]) / delta[green] + 2
    hue[blue] = (r[blue] - g[blue]) / delta[blue] + 4
    hue *= 60
    ranges = {
        "red": ((hue >= 330) | (hue < 15)),
        "orange": (hue >= 15) & (hue < 45),
        "yellow": (hue >= 45) & (hue < 75),
        "green": (hue >= 75) & (hue < 165),
        "teal": (hue >= 165) & (hue < 200),
        "blue": (hue >= 200) & (hue < 260),
        "purple": (hue >= 260) & (hue < 300),
        "magenta": (hue >= 300) & (hue < 330),
    }
    mask = np.zeros(hue.shape, dtype=bool)
    for name in channels:
        if name in ranges:
            mask |= ranges[name]
    return mask.astype(np.float32)


def _rotate_hue(rgb: np.ndarray, degrees: float, mask: np.ndarray) -> np.ndarray:
    # A conservative RGB rotation around the neutral axis approximates a hue
    # shift without introducing an unmanaged alternate color space.
    angle = math.radians(degrees)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    matrix = np.array([
        [0.333 + 0.667 * cos_a, 0.333 - 0.333 * cos_a - 0.577 * sin_a,
         0.333 - 0.333 * cos_a + 0.577 * sin_a],
        [0.333 - 0.333 * cos_a + 0.577 * sin_a, 0.333 + 0.667 * cos_a,
         0.333 - 0.333 * cos_a - 0.577 * sin_a],
        [0.333 - 0.333 * cos_a - 0.577 * sin_a,
         0.333 - 0.333 * cos_a + 0.577 * sin_a, 0.333 + 0.667 * cos_a],
    ], dtype=np.float32)
    shifted = np.tensordot(rgb, matrix.T, axes=1)
    blend = mask[..., None]
    return rgb * (1 - blend) + shifted * blend


def _curve_lut(points: list) -> np.ndarray:
    """A 256-entry tone curve through the given points, monotone.

    Fritsch-Carlson tangents: the interpolant never overshoots between
    control points, so a curve that looks gentle on the panel cannot
    fold values back on themselves in the render.
    """
    settled = sorted(
        (float(x), float(y)) for x, y in points
        if isinstance((x, y), tuple) or True)
    if not settled or settled[0][0] > 0:
        settled.insert(0, (0.0, settled[0][1] if settled else 0.0))
    if settled[-1][0] < 255:
        settled.append((255.0, settled[-1][1]))
    xs = np.array([p[0] for p in settled], dtype=np.float64)
    ys = np.array([p[1] for p in settled], dtype=np.float64)
    xs, keep = np.unique(xs, return_index=True)
    ys = ys[keep]
    if len(xs) == 1:
        return np.full(256, np.clip(ys[0] / 255.0, 0, 1), dtype=np.float32)
    h = np.diff(xs)
    slopes = np.diff(ys) / np.maximum(h, 1e-6)
    tangents = np.empty(len(xs))
    tangents[0] = slopes[0]
    tangents[-1] = slopes[-1]
    for i in range(1, len(xs) - 1):
        if slopes[i - 1] * slopes[i] <= 0:
            tangents[i] = 0.0
        else:
            weight = 3 * (h[i - 1] + h[i])
            tangents[i] = weight / (
                (2 * h[i] + h[i - 1]) / slopes[i - 1]
                + (h[i] + 2 * h[i - 1]) / slopes[i])
    grid = np.arange(256, dtype=np.float64)
    spans = np.clip(np.searchsorted(xs, grid, side="right") - 1,
                    0, len(xs) - 2)
    t = (grid - xs[spans]) / np.maximum(h[spans], 1e-6)
    t = np.clip(t, 0.0, 1.0)
    h00 = (1 + 2 * t) * (1 - t) ** 2
    h10 = t * (1 - t) ** 2
    h01 = t * t * (3 - 2 * t)
    h11 = t * t * (t - 1)
    out = (h00 * ys[spans] + h10 * h[spans] * tangents[spans]
           + h01 * ys[spans + 1] + h11 * h[spans] * tangents[spans + 1])
    return np.clip(out / 255.0, 0.0, 1.0).astype(np.float32)


def _frame_grid(height: int, width: int,
                window: dict[str, float] | None) -> tuple:
    """Normalized coordinates -- of the WHOLE frame, window or not.

    A windowed render sees a crop, but a mask's geometry speaks in
    fractions of the photograph. Mapping the grid, rather than every
    mask, is what lets one change serve them all.
    """
    yy, xx = np.indices((height, width), dtype=np.float32)
    if window:
        # The same arithmetic as the full frame -- (index)/(edge-1) --
        # with the crop's indices shifted into the frame's own. Mapping
        # fractions instead left one-ULP drifts that moved a mask edge
        # a pixel between the fast pass and the full one.
        full_w = width / max(float(window["w"]), 1e-6)
        full_h = height / max(float(window["h"]), 1e-6)
        xx = (np.float32(window["x"] * full_w) + xx) \
            / np.float32(max(full_w - 1.0, 1.0))
        yy = (np.float32(window["y"] * full_h) + yy) \
            / np.float32(max(full_h - 1.0, 1.0))
    else:
        yy /= max(height - 1, 1)
        xx /= max(width - 1, 1)
    return yy, xx


def _spatial_mask(rgb: np.ndarray, shape: str, value: dict[str, Any],
                  window: dict[str, float] | None = None) -> np.ndarray:
    height, width = rgb.shape[:2]
    yy, xx = _frame_grid(height, width, window)
    anchor = str(value.get("anchor", "")).casefold()
    if shape == "linear":
        if "bottom" in anchor:
            ramp = yy
        elif "left" in anchor:
            ramp = 1 - xx
        elif "right" in anchor:
            ramp = xx
        else:
            ramp = 1 - yy
        # How far the gradient reaches from its edge, as "up to 35%".
        # Without it the ramp spans the whole frame -- weight one half in
        # the middle -- so a "bottom" gradient meant for a rooftop lifts
        # the sky and brushes the sun, and no wording in the anchor could
        # say otherwise: two treatments spent three rounds each asking a
        # gradient to "end below the crescent", which this ramp cannot.
        # The full-frame ramp is kept, bit for bit, where no reach is
        # stated, because finished recipes already render with it.
        stated = re.search(r"(?i)\bup\s*to\s*(\d+(?:\.\d+)?)\s*%", anchor)
        if stated:
            reach = min(max(float(stated.group(1)) / 100.0, 0.05), 1.0)
            ramp = np.clip((ramp - (1.0 - reach)) / reach, 0.0, 1.0)
            # Smoothstep, so the gradient lands without a visible edge.
            ramp = ramp * ramp * (3.0 - 2.0 * ramp)
        return ramp
    if shape == "radial":
        centre_x, centre_y, reach = 0.5, 0.5, 0.7072
        if "bright" in anchor or "sun" in anchor:
            # Anchored to the light rather than to the frame. A shoot
            # follows its subject around the picture -- the sun is in a
            # different place in all eight of these -- so a mask pinned
            # to the middle is a mask for one photograph.
            lum = (rgb * np.array([0.2126, 0.7152, 0.0722])).sum(axis=2)
            # Half the brightest thing in the frame, not a percentile: a
            # sun is a handful of pixels in a dark sky, and any
            # percentile wide enough to be robust is wide enough to
            # select the sky as well -- which centres the mask on the
            # middle of the frame and quietly does nothing.
            found = np.nonzero(lum >= 0.5 * float(lum.max()))
            if found[0].size:
                centre_y = float(found[0].mean()) / max(height - 1, 1)
                centre_x = float(found[1].mean()) / max(width - 1, 1)
                # As wide as the bright part itself, unless told otherwise.
                spread = max(
                    float(np.std(found[0])) / max(height - 1, 1),
                    float(np.std(found[1])) / max(width - 1, 1))
                reach = max(spread * 4.0, 0.04)
        # An explicit centre, where something other than the brightest
        # thing in the frame is being masked -- a face, a building, the
        # second reflection. Stated as fractions of the frame so it
        # survives being rendered at proof size and again at full size.
        placed = re.search(
            r"(?i)\bat\s*(\d+(?:\.\d+)?)\s*%[,\s]+(\d+(?:\.\d+)?)\s*%",
            anchor)
        if placed:
            centre_x = min(max(float(placed.group(1)) / 100.0, 0.0), 1.0)
            centre_y = min(max(float(placed.group(2)) / 100.0, 0.0), 1.0)
        stated = re.search(r"(?i)radius\s*(\d+(?:\.\d+)?)\s*%", anchor)
        if stated:
            reach = max(float(stated.group(1)) / 100.0, 0.01)
        # Circular on the photograph, not on the unit square -- the
        # WHOLE photograph, when these pixels are only a window of it.
        if window:
            aspect = round(width / max(float(window["w"]), 1e-6)) \
                / max(round(height / max(float(window["h"]), 1e-6)), 1)
        else:
            aspect = width / max(height, 1)
        distance = np.sqrt(
            ((xx - centre_x) * aspect) ** 2 + (yy - centre_y) ** 2) / reach
        # Feather: what fraction of the radius the falloff occupies. At
        # 1.0 the weight falls across the whole radius, which is exactly
        # the old behaviour -- clip((1-d)/1) == clip(1-d) -- so recipes
        # rendered before feather was read do not change at full
        # feather. Below it, an inner core holds full weight and the
        # transition narrows toward the rim. It was promised by every
        # mask's schema and read by nothing until now.
        feather = float(value.get("feather", 1.0) or 1.0)
        feather = min(max(feather, 0.05), 1.0)
        mask = np.clip((1 - distance) / feather, 0, 1)
        # Smoothstep: no visible edge where the mask runs out, which is
        # the whole difference between a local adjustment and a halo.
        mask = mask * mask * (3.0 - 2.0 * mask)
        if "invert" in anchor or "outside" in anchor or "except" in anchor:
            return 1.0 - mask
        return mask
    if shape == "luma":
        lum = (rgb * np.array([0.2126, 0.7152, 0.0722])).sum(axis=2)
        if "midtone" in anchor or "mid tone" in anchor or "middle" in anchor:
            # A band that peaks where the eye reads middle grey and falls
            # away at both ends -- one tone equalizer band, which is what
            # a photograph asks for when the clouds need lifting and
            # neither the silhouette below them nor the sun above them
            # does. Measured perceptually rather than in linear light,
            # because "the midtones" is a statement about how a scene
            # looks and mid grey sits at 0.18 of the light.
            shown = np.power(np.clip(lum, 0.0, 1.0), 1.0 / _ENCODE_GAMMA)
            return np.clip(1.0 - np.abs(shown - 0.5) * 2.5, 0.0, 1.0)
        return np.clip((1 - lum * 2) if "shadow" in anchor else lum * 2, 0, 1)
    if shape == "brush":
        # Painted by hand: the weights are a small greyscale map carried
        # inside the operation itself -- base64, bounded resolution -- so
        # a portable recipe stays portable and the same strokes land on a
        # proof and on the full-size render alike. Feather blurs the map
        # before it is stretched, so the stroke's edge softens in the
        # map's own scale whatever size the render is.
        import io as io_module

        from PIL import Image as PILImage
        from PIL import ImageFilter

        encoded = str(value.get("map") or "")
        if not encoded:
            return np.zeros(rgb.shape[:2], dtype=np.float32)
        try:
            sheet = PILImage.open(io_module.BytesIO(
                base64.b64decode(encoded))).convert("L")
        except Exception:                            # noqa: BLE001 - no mask
            return np.zeros(rgb.shape[:2], dtype=np.float32)
        stated = value.get("feather")
        feather = 1.0 if stated is None else float(stated)
        radius = max(0.0, min(feather, 1.0)) * 6.0
        if radius >= 0.5:
            sheet = sheet.filter(ImageFilter.GaussianBlur(radius))
        height, width = rgb.shape[:2]
        if window:
            mw, mh = sheet.size
            sheet = sheet.crop((
                int(window["x"] * mw), int(window["y"] * mh),
                max(int((window["x"] + window["w"]) * mw),
                    int(window["x"] * mw) + 1),
                max(int((window["y"] + window["h"]) * mh),
                    int(window["y"] * mh) + 1)))
        sheet = sheet.resize((width, height), PILImage.Resampling.BILINEAR)
        weights = np.asarray(sheet, dtype=np.float32) / 255.0
        if "invert" in anchor or "outside" in anchor or "except" in anchor:
            return (1.0 - weights).astype(np.float32)
        return weights
    if shape == "color":
        # Selected by what the pixels ARE rather than where they sit: a
        # hue around a centre, softly, gated so near-grey pixels -- which
        # have no honest hue -- stay out. The numbers ride in the anchor
        # sentence like every other mask's, so the same carrier serves
        # the engine, the page and a reader.
        def stated(name: str, default: float) -> float:
            found = re.search(
                rf"(?i)\b{name}\s*(-?\d+(?:\.\d+)?)", anchor)
            return float(found.group(1)) if found else default

        centre = stated("hue", 0.0) % 360.0
        core = max(stated("range", 30.0), 1.0) / 2.0
        soft = max(stated("softness", 20.0), 0.0)
        floor = min(max(stated("above", 10.0), 0.0), 100.0) / 100.0
        r, g, b = [np.clip(rgb[..., i], 0, None) for i in range(3)]
        mx = np.maximum.reduce([r, g, b])
        mn = np.minimum.reduce([r, g, b])
        delta = mx - mn
        hue = np.zeros_like(mx)
        nonzero = delta > 1e-6
        red = (mx == r) & nonzero
        green = (mx == g) & nonzero
        blue = (mx == b) & nonzero
        hue[red] = ((g[red] - b[red]) / delta[red]) % 6
        hue[green] = (b[green] - r[green]) / delta[green] + 2
        hue[blue] = (r[blue] - g[blue]) / delta[blue] + 4
        hue *= 60
        away = np.abs(((hue - centre) + 180.0) % 360.0 - 180.0)
        if soft > 0:
            weight = np.clip(1.0 - (away - core) / soft, 0.0, 1.0)
        else:
            weight = (away <= core).astype(np.float32)
        weight = weight * weight * (3.0 - 2.0 * weight)
        saturation = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0)
        if floor > 0:
            gate = np.clip(saturation / max(floor, 1e-6), 0.0, 1.0)
            weight = weight * (gate * gate * (3.0 - 2.0 * gate))
        # The wedge's other walls, each a feathered ramp rather than a
        # cliff: a ceiling on saturation (pastels without the neon), and
        # a floor and ceiling on brightness (the sky without the sea).
        # All default wide open, so a mask that never asked is unmoved.
        ceiling = min(max(stated("below", 100.0), 0.0), 100.0) / 100.0
        lit = min(max(stated("brighter than", 0.0), 0.0), 100.0) / 100.0
        dim = min(max(stated("darker than", 100.0), 0.0), 100.0) / 100.0
        ramp = 0.05 + soft / 200.0
        if ceiling < 1.0:
            gate = np.clip((ceiling - saturation) / ramp + 0.5, 0.0, 1.0)
            weight = weight * (gate * gate * (3.0 - 2.0 * gate))
        if lit > 0.0 or dim < 1.0:
            shown = np.clip(
                np.power(np.maximum(mx, 0.0), 1.0 / _ENCODE_GAMMA), 0.0, 1.0)
            if lit > 0.0:
                gate = np.clip((shown - lit) / ramp + 0.5, 0.0, 1.0)
                weight = weight * (gate * gate * (3.0 - 2.0 * gate))
            if dim < 1.0:
                gate = np.clip((dim - shown) / ramp + 0.5, 0.0, 1.0)
                weight = weight * (gate * gate * (3.0 - 2.0 * gate))
        if "invert" in anchor or "outside" in anchor or "except" in anchor:
            return (1.0 - weight).astype(np.float32)
        return weight.astype(np.float32)
    return _hue_mask(rgb, anchor)


_HEAL_MOST = 64          # spots a single operation may carry
_HEAL_WIDEST = 0.05      # radius cap, as a fraction of the short edge


def _heal_spots(rgb: np.ndarray, value: dict[str, Any],
                window: dict[str, float] | None = None) -> np.ndarray:
    """Rebuild each listed disc from the ring around it.

    For every pixel inside a spot, the healed colour is the inverse-
    distance blend of samples taken on a ring just outside the disc --
    so gradients (a sky darkening toward a corner) heal into the same
    gradient, not into a flat average. The repair fades in across the
    disc's outer quarter, and outside the disc nothing is touched.
    """
    spots = value.get("spots")
    if not isinstance(spots, list) or not spots:
        return rgb
    height, width = rgb.shape[:2]
    # All arithmetic runs in the FULL frame's coordinates and only the
    # indexing is local: the same numbers with a different offset drift
    # by one ULP, and one ULP at a ring sample is a different pixel.
    full_w = round(width / max(float(window["w"]), 1e-6)) if window \
        else width
    full_h = round(height / max(float(window["h"]), 1e-6)) if window \
        else height
    off_x = round(float(window["x"]) * full_w) if window else 0
    off_y = round(float(window["y"]) * full_h) if window else 0
    short = float(min(full_h, full_w))
    healed = rgb.copy()
    for spot in spots[:_HEAL_MOST]:
        if not isinstance(spot, dict):
            continue
        try:
            cx = float(spot["x"]) * full_w
            cy = float(spot["y"]) * full_h
            radius = min(max(float(spot["r"]), 0.0), _HEAL_WIDEST) * short
        except (KeyError, TypeError, ValueError):
            continue
        if radius < 0.75:
            continue
        left = max(int(cx - radius) - 1, off_x)
        right = min(int(cx + radius) + 2, off_x + width)
        top = max(int(cy - radius) - 1, off_y)
        bottom = min(int(cy + radius) + 2, off_y + height)
        if right <= left or bottom <= top:
            continue
        ys, xs = np.mgrid[top:bottom, left:right].astype(np.float32)
        away = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
        inside = away <= radius
        if not inside.any():
            continue
        angles = np.linspace(0.0, 2.0 * np.pi, 16, endpoint=False)
        ring_x = np.clip(cx + np.cos(angles) * radius * 1.35,
                         off_x, off_x + width - 1).astype(np.int32)
        ring_y = np.clip(cy + np.sin(angles) * radius * 1.35,
                         off_y, off_y + height - 1).astype(np.int32)
        ring = healed[ring_y - off_y, ring_x - off_x]        # (16, 3)
        px = xs[inside][:, None]
        py = ys[inside][:, None]
        reach = ((px - ring_x[None, :].astype(np.float32)) ** 2
                 + (py - ring_y[None, :].astype(np.float32)) ** 2)
        pull = 1.0 / (reach + 1.0)
        patch = (pull @ ring) / pull.sum(axis=1, keepdims=True)
        # The repair fades in across the disc's outer quarter.
        blend = np.clip((radius - away[inside]) / max(radius * 0.25, 0.5),
                        0.0, 1.0)
        blend = blend * blend * (3.0 - 2.0 * blend)
        piece = healed[top - off_y:bottom - off_y,
                       left - off_x:right - off_x]
        piece[inside] = (piece[inside] * (1.0 - blend[:, None])
                         + patch * blend[:, None])
    return healed


def _warp_lattice(value: dict[str, Any]) -> tuple[np.ndarray, int] | None:
    """The lattice a warp carries: (size^3, 3) float32 deltas, or None."""
    try:
        size = int(value.get("size", 9))
    except (TypeError, ValueError):
        return None
    if not 2 <= size <= 33:
        return None
    encoded = str(value.get("lattice") or "")
    if not encoded:
        return None
    try:
        raw = np.frombuffer(base64.b64decode(encoded), dtype=np.float16)
        lattice = raw.astype(np.float32).reshape(size, size, size, 3)
    except (ValueError, TypeError):
        return None
    return lattice, size


def _colour_warp(rgb: np.ndarray, value: dict[str, Any]) -> np.ndarray:
    """Move every colour by the lattice's word for it, smoothly.

    The lattice is indexed by encoded R, G, B; between nodes the delta
    is the trilinear blend of the eight corners, so neighbouring
    colours always receive neighbouring corrections -- the smoothness
    doctrine as arithmetic. A lattice of zeros is a perfect no-op, and
    anything unreadable is treated as one.
    """
    held = _warp_lattice(value)
    if held is None:
        return rgb
    lattice, size = held
    shown = np.clip(_encoded(np.clip(rgb, 0.0, None)), 0.0, 1.0)
    pos = shown * (size - 1)
    base = np.minimum(np.floor(pos).astype(np.int32), size - 2)
    frac = (pos - base).astype(np.float32)
    r0, g0, b0 = base[..., 0], base[..., 1], base[..., 2]
    fr, fg, fb = (frac[..., i][..., None] for i in range(3))
    delta = np.zeros_like(shown)
    for dr in (0, 1):
        wr = fr if dr else (1.0 - fr)
        for dg in (0, 1):
            wg = fg if dg else (1.0 - fg)
            for db in (0, 1):
                wb = fb if db else (1.0 - fb)
                corner = lattice[r0 + dr, g0 + dg, b0 + db]
                delta = delta + corner * (wr * wg * wb)
    strength = float(value.get("strength", 100.0) or 0.0) / 100.0
    strength = min(max(strength, 0.0), 1.0)
    walked = np.clip(shown + delta * strength, 0.0, 1.0)
    return _decoded(walked).astype(np.float32)


def _box_mean(plane: np.ndarray, radius: int) -> np.ndarray:
    """A box average by integral image: any radius, one pass."""
    height, width = plane.shape
    integral = np.zeros((height + 1, width + 1), np.float64)
    integral[1:, 1:] = np.cumsum(np.cumsum(plane, axis=0), axis=1)
    y0 = np.clip(np.arange(height) - radius, 0, height)
    y1 = np.clip(np.arange(height) + radius + 1, 0, height)
    x0 = np.clip(np.arange(width) - radius, 0, width)
    x1 = np.clip(np.arange(width) + radius + 1, 0, width)
    area = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    summed = (integral[y1][:, x1] - integral[y0][:, x1]
              - integral[y1][:, x0] + integral[y0][:, x0])
    return (summed / area).astype(np.float32)


def _clean_colour(rgb: np.ndarray, strength: float,
                  detail_scale: float,
                  window: dict[str, float] | None = None) -> np.ndarray:
    """Colour cleaned by what the green channel knows.

    Phase One's old moiré patent, done the modern fast way: red and
    blue are each rebuilt as a locally-linear function of green (a
    guided filter with green as the guide), so colour speckle and
    false-colour shimmer -- places where red or blue wander while
    green holds still -- are averaged away, while every edge green
    knows about survives untouched. Green itself, the luminance
    anchor, is never moved.
    """
    k = min(max(float(strength), 0.0), 100.0) / 100.0
    if k <= 0.0:
        return rgb
    height, width = rgb.shape[:2]
    virtual_min = min(height, width)
    if window:
        virtual_min = min(height / max(float(window["h"]), 1e-6),
                          width / max(float(window["w"]), 1e-6))
    radius = max(2, int(round(virtual_min * 0.008 * detail_scale)))
    guide = rgb[..., 1].astype(np.float32)
    mean_guide = _box_mean(guide, radius)
    variance = _box_mean(guide * guide, radius) - mean_guide * mean_guide
    eps = 4e-4   # how strong an edge must be before it is kept
    cleaned = rgb.copy()
    for channel in (0, 2):
        plane = rgb[..., channel].astype(np.float32)
        mean_plane = _box_mean(plane, radius)
        covariance = _box_mean(guide * plane, radius) \
            - mean_guide * mean_plane
        slope = covariance / (variance + eps)
        offset = mean_plane - slope * mean_guide
        smoothed = _box_mean(slope, radius) * guide \
            + _box_mean(offset, radius)
        cleaned[..., channel] = plane + (smoothed - plane) * k
    return np.clip(cleaned, 0.0, None).astype(np.float32)


def _film_grain(rgb: np.ndarray, value: Any,
                window: dict[str, float] | None = None) -> np.ndarray:
    """Film grain: deterministic, sized to the frame, strongest midtone.

    The noise is a hash of each pixel's position IN THE FULL FRAME, so
    the same frame always grows the same grain -- a re-render is not a
    re-roll, a cached proof stays honest, and a windowed fast pass is
    the full render's grain cropped, bit for bit. Cells scale with the
    frame so grain is a look, not a resolution artifact; the weight
    peaks in the midtones and dies toward black and white, the way
    silver did.
    """
    amount = min(max(float(value or 0.0), 0.0), 100.0) / 100.0
    if amount <= 0.0:
        return rgb
    height, width = rgb.shape[:2]
    full_w = round(width / max(float(window["w"]), 1e-6)) if window \
        else width
    full_h = round(height / max(float(window["h"]), 1e-6)) if window \
        else height
    off_x = round(float(window["x"]) * full_w) if window else 0
    off_y = round(float(window["y"]) * full_h) if window else 0
    cell = max(1, round(max(full_w, full_h) / 1500))
    xs = ((np.arange(width, dtype=np.int64) + off_x) // cell)
    ys = ((np.arange(height, dtype=np.int64) + off_y) // cell)
    gx, gy = np.meshgrid(xs.astype(np.float64), ys.astype(np.float64))
    seeded = np.sin(gx * 12.9898 + gy * 78.233) * 43758.5453
    noise = (seeded - np.floor(seeded)).astype(np.float32) - 0.5
    shown = np.clip(_encoded(np.clip(rgb, 0.0, None)), 0.0, 1.0)
    lum = (shown[..., 0] * 0.2126 + shown[..., 1] * 0.7152
           + shown[..., 2] * 0.0722)
    weight = 4.0 * lum * (1.0 - lum)              # silver's own curve
    grained = np.clip(
        shown + (noise * weight * amount * 0.12)[..., None], 0.0, 1.0)
    return _decoded(grained).astype(np.float32)


def _uniformity(rgb: np.ndarray, effect: dict[str, Any],
                value: dict[str, Any]) -> np.ndarray:
    """Pull every pixel's hue, colour or light toward the mask's own aim.

    The evener: skin that wanders between red and yellow, a sky that
    shifts across a gradient -- each pixel walks part of the way toward
    one chosen colour, and the walk is gated by the mask outside, so
    only the wedge's inhabitants move. The aim rides in the anchor
    sentence ("target saturation 55") where the eyedropper wrote it;
    without one, the wedge's own middle serves.
    """
    anchor = str(value.get("anchor") or "").casefold()

    def stated(name: str, default: float) -> float:
        found = re.search(rf"(?i)\b{name}\s*(-?\d+(?:\.\d+)?)", anchor)
        return float(found.group(1)) if found else default

    k = min(max(float(effect.get("value", 0.0) or 0.0), 0.0), 100.0) / 100.0
    if k <= 0.0:
        return rgb
    which = str(effect.get("op", "")).split(".", 1)[-1]
    r, g, b = [np.clip(rgb[..., i], 0, None) for i in range(3)]
    mx = np.maximum.reduce([r, g, b])
    mn = np.minimum.reduce([r, g, b])
    delta = mx - mn
    if which == "hue":
        hue = np.zeros_like(mx)
        nonzero = delta > 1e-6
        red = (mx == r) & nonzero
        green = (mx == g) & nonzero
        blue = (mx == b) & nonzero
        hue[red] = ((g[red] - b[red]) / delta[red]) % 6
        hue[green] = (b[green] - r[green]) / delta[green] + 2
        hue[blue] = (r[blue] - g[blue]) / delta[blue] + 4
        hue *= 60
        aim = stated("hue", 0.0) % 360.0
        walked = (hue + (((aim - hue) + 180.0) % 360.0 - 180.0) * k) % 360.0
        # The same brightness and chroma, rebuilt on the walked hue.
        sixth = (walked / 60.0) % 6.0
        x = delta * (1.0 - np.abs(sixth % 2.0 - 1.0))
        zeros = np.zeros_like(delta)
        band = (np.floor(sixth).astype(np.int32) % 6)[..., None]
        highs = np.select(
            [band == 0, band == 1, band == 2, band == 3, band == 4],
            [np.stack([delta, x, zeros], -1), np.stack([x, delta, zeros], -1),
             np.stack([zeros, delta, x], -1), np.stack([zeros, x, delta], -1),
             np.stack([x, zeros, delta], -1)],
            np.stack([delta, zeros, x], -1))
        return (highs + mn[..., None]).astype(np.float32)
    if which == "saturation":
        floor = stated("above", 10.0)
        ceiling = stated("below", 100.0)
        aim = min(max(stated(
            "target saturation", (floor + ceiling) / 2.0), 0.0), 100.0) / 100.0
        saturation = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0)
        scale = np.where(
            saturation > 1e-6,
            (saturation + (aim - saturation) * k)
            / np.maximum(saturation, 1e-6), 1.0)
        walked = mx[..., None] + (rgb - mx[..., None]) * scale[..., None]
        return np.clip(walked, 0.0, None).astype(np.float32)
    if which == "lightness":
        lit = stated("brighter than", 0.0)
        dim = stated("darker than", 100.0)
        aim = min(max(stated(
            "target light", (lit + dim) / 2.0), 0.0), 100.0) / 100.0
        shown = np.power(np.maximum(mx, 1e-6), 1.0 / _ENCODE_GAMMA)
        walked = shown + (aim - shown) * k
        scale = np.power(np.clip(walked, 0.0, None), _ENCODE_GAMMA) \
            / np.maximum(mx, 1e-6)
        return np.clip(rgb * scale[..., None], 0.0, None).astype(np.float32)
    return rgb


def mask_weights(rgb: np.ndarray, shape: str, value: dict[str, Any]) -> np.ndarray:
    """The weights a mask will actually blend with, for measuring.

    Public because a treatment that asks for a mask should be able to
    find out what the renderer will do with the answer before paying for
    the render: the gap between "a bottom gradient over the rooftop" and
    a ramp at half strength in the middle of the sky lived here,
    unmeasured, for nine live runs.
    """
    return _spatial_mask(rgb, shape, value)


def _inscribed(width: int, height: int, degrees: float) -> tuple[int, int]:
    """The largest upright rectangle that fits inside a turned frame.

    Straightening a horizon turns the picture, and the corners of the
    original then hang outside the upright frame while the corners of the
    upright frame hang outside the picture. A develop program crops back
    to what is still photograph. Returning the whole turned canvas
    instead leaves black wedges in the corners -- 5.7% of one frame at a
    degree and a half, which is not a straighten, it is a mistake with a
    border.
    """
    angle = math.radians(abs(float(degrees)) % 180.0)
    if angle > math.pi / 2:
        angle = math.pi - angle
    sin_a, cos_a = abs(math.sin(angle)), abs(math.cos(angle))
    if width <= 0 or height <= 0:
        return width, height
    long_side, short_side = max(width, height), min(width, height)
    if short_side <= 2.0 * sin_a * cos_a * long_side or abs(sin_a - cos_a) < 1e-10:
        half = 0.5 * short_side
        if width <= height:
            kept = (half / max(sin_a, 1e-9), half / max(cos_a, 1e-9))
        else:
            kept = (half / max(cos_a, 1e-9), half / max(sin_a, 1e-9))
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        kept = ((width * cos_a - height * sin_a) / cos_2a,
                (height * cos_a - width * sin_a) / cos_2a)
    return max(int(kept[0]), 1), max(int(kept[1]), 1)


def _straighten(image: Image.Image, degrees: float) -> Image.Image:
    """Turn the photograph, and keep only what is still photograph."""
    if not degrees:
        return image
    width, height = image.size
    turned = image.rotate(float(degrees), resample=Image.Resampling.BICUBIC,
                          expand=True, fillcolor=(0, 0, 0))
    keep_width, keep_height = _inscribed(width, height, degrees)
    left = max((turned.size[0] - keep_width) // 2, 0)
    top = max((turned.size[1] - keep_height) // 2, 0)
    return turned.crop((left, top, left + min(keep_width, turned.size[0]),
                        top + min(keep_height, turned.size[1])))


def _geometry(image: Image.Image, operations: list[dict[str, Any]]) -> Image.Image:
    result = image
    operations = active(operations)
    # Straightening comes before framing, whatever order the recipe put
    # them in: a crop chosen on a crooked picture is not the crop the
    # photographer asked for.
    for item in operations:
        if item.get("op") == "geometry.rotation":
            result = _straighten(result, float(item["value"]))
    for item in operations:
        if item.get("op") == "geometry.crop" and isinstance(
                item.get("value"), dict):
            # The photographer's own frame: a rectangle in fractions of
            # the straightened picture, so the same crop lands on a proof
            # and on the full-size render alike. Bounded so a degenerate
            # drag can never ask for an empty photograph.
            value = item["value"]
            width, height = result.size
            left = min(max(float(value.get("left", 0.0)), 0.0), 0.95)
            top = min(max(float(value.get("top", 0.0)), 0.0), 0.95)
            wide = min(max(float(value.get("width", 1.0)), 0.05),
                       1.0 - left)
            tall = min(max(float(value.get("height", 1.0)), 0.05),
                       1.0 - top)
            result = result.crop((
                int(left * width), int(top * height),
                max(int((left + wide) * width), int(left * width) + 8),
                max(int((top + tall) * height), int(top * height) + 8)))
    for item in operations:
        if item.get("op") == "geometry.crop_aspect":
            ratio = item.get("value")
            if isinstance(ratio, list) and len(ratio) == 2 and ratio[1]:
                target = float(ratio[0]) / float(ratio[1])
                width, height = result.size
                current = width / height
                if current > target:
                    new_width = int(height * target)
                    left = (width - new_width) // 2
                    result = result.crop((left, 0, left + new_width, height))
                elif current < target:
                    new_height = int(width / target)
                    top = (height - new_height) // 2
                    result = result.crop((0, top, width, top + new_height))
    return result


def _window_crop(rgb: np.ndarray, window: dict[str, float]
                 ) -> tuple[np.ndarray, dict[str, float]]:
    """The crop, and the window recomputed from the integer cut.

    The grid the ops build must describe exactly the pixels kept, so
    the fractions are re-derived from the rounded pixel box -- a
    half-pixel drift at 4x magnification is a visible seam.
    """
    height, width = rgb.shape[:2]
    x0 = min(max(int(round(float(window["x"]) * width)), 0), width - 8)
    y0 = min(max(int(round(float(window["y"]) * height)), 0), height - 8)
    x1 = min(max(int(round((float(window["x"]) + float(window["w"]))
                           * width)), x0 + 8), width)
    y1 = min(max(int(round((float(window["y"]) + float(window["h"]))
                           * height)), y0 + 8), height)
    exact = {"x": x0 / width, "y": y0 / height,
             "w": (x1 - x0) / width, "h": (y1 - y0) / height}
    return rgb[y0:y1, x0:x1], exact


def render_recipe(
    baseline_tiff: Path, recipe: dict[str, Any], output_dir: Path,
    allow_incomplete: bool = False, reference_jpeg: Path | None = None,
    progress: Any = None, window: dict[str, float] | None = None,
) -> dict[str, Any]:
    source = baseline_tiff.expanduser().resolve()
    if not source.is_file():
        raise DevelopmentError(f"baseline image is unavailable: {source}")
    if recipe.get("format") != "opencull-development-recipe-v1":
        raise DevelopmentError("unsupported recipe IR format")
    diagnostics = recipe.get("diagnostics", [])
    if diagnostics and not allow_incomplete:
        raise DevelopmentError(
            f"recipe contains {len(diagnostics)} unsupported instruction(s)")
    destination = output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    operations = recipe.get("operations", [])
    if not isinstance(operations, list):
        raise DevelopmentError("recipe operations must be a list")
    def stage(done: int, total: int, what: str) -> None:
        if progress is not None:
            progress(done, total, what)

    # Two stages bracket the adjustments: reading the decode in, and
    # matching it to the camera. Counted so the bar starts moving before
    # the first adjustment and does not sit full while the file is
    # written.
    wanted = active(operations)
    steps = len(wanted) + 3
    stage(0, steps, "reading the frame")
    rgb = _load_linear(source)
    calibration = None
    if reference_jpeg is not None:
        reference = reference_jpeg.expanduser().resolve()
        if not reference.is_file():
            raise DevelopmentError(f"calibration JPEG is unavailable: {reference}")
        stage(1, steps, "matching the camera")
        rgb, calibration = _calibrate_to_jpeg(rgb, reference)
    if window is not None:
        # A windowed render is a fast look at one part of the frame:
        # the LUT above was fitted on the whole picture, so the crop
        # wears exactly the colour the full render will, and every op
        # below reads coordinates through the window.
        rgb, window = _window_crop(rgb, window)
    rgb = _apply_global(
        rgb, operations,
        progress=lambda done, _total, what: stage(done + 2, steps, what),
        window=window)
    stage(len(wanted) + 2, steps, "writing the photograph")
    display = _linear_rec2020_to_srgb(np.clip(rgb, 0, 1))
    image = Image.fromarray(np.uint8(np.clip(display * 255 + 0.5, 0, 255)), "RGB")
    if window is None:
        # Geometry frames the whole photograph; a window is already a
        # framing, and the caller only asks for one when the recipe
        # carries no geometry of its own.
        image = _geometry(image, operations)
    image = ImageEnhance.Sharpness(image).enhance(1.0)
    stem = Path(str(recipe.get("source_photo", source.stem))).stem
    style = str(recipe.get("style", "render"))
    output = destination / f"{stem}.{style}.jpg"
    provenance = destination / f"{stem}.{style}.render.json"
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=destination)
    os.close(fd)
    try:
        image.save(temporary, format="JPEG", quality=95, optimize=True,
                   icc_profile=srgb_profile())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    record = {
        "format": ENGINE_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "source_baseline": {"path": str(source), "sha256": _sha256(source)},
        "calibration": calibration,
        "recipe": recipe,
        "output": {"path": str(output), "sha256": _sha256(output),
                   "width": image.width, "height": image.height},
        "complete": not bool(diagnostics),
        "notice": (
            "Deterministic preview; unsupported diagnostics remain unexecuted."
            if diagnostics else "Complete typed global recipe render."),
    }
    provenance.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("recipe", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--reference-jpeg", type=Path,
                        help="camera JPEG used to calibrate the RAW baseline")
    parser.add_argument("--adjustments", type=Path,
                        help="bounded adjustment draft applied as a new recipe revision")
    parser.add_argument("--project", type=Path,
                        help="project manifest to update after a successful render")
    args = parser.parse_args(argv)
    recipe = json.loads(args.recipe.read_text())
    if args.adjustments:
        recipe = apply_adjustment_draft(recipe, json.loads(args.adjustments.read_text()))
    result = render_recipe(
        args.baseline, recipe, args.output_dir,
        allow_incomplete=args.allow_incomplete, reference_jpeg=args.reference_jpeg)
    if args.project:
        from opencull_gui.project import register_render
        register_render(args.project, result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
