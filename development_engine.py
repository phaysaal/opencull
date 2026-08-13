"""Deterministic global development engine for compiled recipe IR."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
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
RECIPE_ENGINE_REVISION = 7


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
    sample_source = source_display[::16, ::16]
    sample_target = target[::16, ::16]
    quantiles = np.linspace(0.0, 1.0, 33)
    calibrated = np.empty_like(source_display)
    knots: list[dict[str, list[float]]] = []
    for channel in range(3):
        source_knots = np.quantile(sample_source[..., channel], quantiles)
        target_knots = np.quantile(sample_target[..., channel], quantiles)
        source_knots, unique = np.unique(source_knots, return_index=True)
        target_knots = target_knots[unique]
        calibrated[..., channel] = np.interp(
            source_display[..., channel], source_knots, target_knots)
        knots.append({"source": source_knots.tolist(), "target": target_knots.tolist()})
    # The match is what the photographer wanted from the camera: its tones
    # and its colour. What they did not want is its clipping. A camera
    # rendering has already spent the highlights the raw still holds, so
    # matching it all the way to white throws that headroom away --
    # measured at 12.9% of one frame blown rising to 21.2%. The match
    # therefore holds through the shadows and midtones and fades out
    # towards the top, leaving the brightest tones where the decode put
    # them, with detail still in them to be worked on.
    luminance = source_display @ _LUMA
    weight = 1.0 - _ramp(
        (luminance - CALIBRATION_HOLD) /
        max(CALIBRATION_RELEASE - CALIBRATION_HOLD, 1e-6))
    calibrated = source_display + (
        calibrated - source_display) * weight[..., None]
    return _srgb_to_linear_rec2020(calibrated), {
        "reference_path": str(reference), "reference_sha256": _sha256(reference),
        "method": "per-channel-srgb-quantile-lut-with-highlight-rolloff",
        "rolloff": {"holds_below": CALIBRATION_HOLD,
                    "released_above": CALIBRATION_RELEASE},
        "knots": knots,
    }


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
                  progress: Any = None) -> np.ndarray:
    """Apply a recipe's adjustments in order.

    ``progress`` is called with (done, total, what) after each one. A
    treatment is twenty-odd named adjustments and the photographer is
    entitled to watch them arrive rather than watch a bar that cannot say
    anything until the whole thing is finished.
    """
    result = np.array(rgb, dtype=np.float32, copy=True)
    detail_scale = max(max(result.shape[:2]) / DETAIL_REFERENCE_EDGE, 0.25)
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
        if op.startswith("mask.") and isinstance(value, dict):
            mask = _spatial_mask(result, op[5:], value)
            opacity = float(value.get("opacity", 1.0))
            blend = np.clip(mask * opacity, 0, 1)[..., None]
            for effect in value.get("effects", []):
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
            factor = 1.0 + value / 5000.0 if item.get("mode") == "delta" else 1.0
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
        elif op == "levels.midpoint":
            # The levels midpoint is a gamma about the middle of the scale.
            if value > 0:
                result = np.clip(result, 0.0, None) ** (1.0 / max(value, 0.1))
        elif op == "finish.vignette":
            yy, xx = np.indices(result.shape[:2], dtype=np.float32)
            yy /= max(result.shape[0] - 1, 1)
            xx /= max(result.shape[1] - 1, 1)
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


def _spatial_mask(rgb: np.ndarray, shape: str, value: dict[str, Any]) -> np.ndarray:
    height, width = rgb.shape[:2]
    yy, xx = np.indices((height, width), dtype=np.float32)
    yy /= max(height - 1, 1)
    xx /= max(width - 1, 1)
    anchor = str(value.get("anchor", "")).casefold()
    if shape == "linear":
        if "bottom" in anchor:
            return yy
        if "left" in anchor:
            return 1 - xx
        if "right" in anchor:
            return xx
        return 1 - yy
    if shape == "radial":
        distance = np.sqrt((xx - 0.5) ** 2 + (yy - 0.5) ** 2) / 0.7072
        return np.clip(1 - distance, 0, 1)
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
    return _hue_mask(rgb, anchor)


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


def render_recipe(
    baseline_tiff: Path, recipe: dict[str, Any], output_dir: Path,
    allow_incomplete: bool = False, reference_jpeg: Path | None = None,
    progress: Any = None,
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
    rgb = _apply_global(
        rgb, operations,
        progress=lambda done, _total, what: stage(done + 2, steps, what))
    stage(len(wanted) + 2, steps, "writing the photograph")
    display = _linear_rec2020_to_srgb(np.clip(rgb, 0, 1))
    image = Image.fromarray(np.uint8(np.clip(display * 255 + 0.5, 0, 255)), "RGB")
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
