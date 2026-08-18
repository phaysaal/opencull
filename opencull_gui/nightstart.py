"""A starting point for a night frame, from its pixels and its EXIF.

A night photograph arrives almost black. Everything it was taken for
lives in the bottom few percent of the range, and finding it by
dragging sliders is a long evening -- the first useful version needs a
black point set just under the sky, a stretch strong enough to lift
the faint stars without blowing the bright ones, and enough quiet laid
on the background to make what was lifted bearable.

None of that is a matter of taste at the start; it is a matter of what
this frame's sky actually is. So it is measured. The sky's own level
and its noise come from the pixels, and the camera's settings say what
to expect of them: how much noise an ISO ought to carry, how much of
it a stack of N frames has already averaged away, and -- from focal
length and the sidereal rate -- how long an exposure could have been
before the stars trailed.

What comes back is a set of ordinary operations. Every one of them is
a slider the photographer can then move; nothing here is a mode.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

# Where a night sky wants to sit once it has been lifted: dark enough
# to still read as night, light enough that what is in it is visible.
SKY_TARGET = 0.13
# Noise this size in the stretched picture is what "grainy" means.
NOISE_BEARABLE = 0.012
# And this much is past what quieting the background can rescue: a
# lift that would leave more grain than this is not a lift, it is a
# magnified guess.
NOISE_CEILING = 0.045
# Below this the sky is not a sky, it is a lens cap.
DEAD_FRAME = 1e-4

_EXIF_TAGS = {33434: "seconds", 34855: "iso", 37386: "focal",
              33437: "aperture", 36867: "when", 272: "camera"}
_EXIF_MARKER = b"Exif\x00\x00"


def camera_facts(path: str | Path) -> dict[str, Any]:
    """What the camera recorded about how the frame was taken."""
    from PIL import Image

    path = Path(path)
    exif = None
    try:
        with Image.open(path) as opened:
            exif = opened.getexif()
    except Exception:                            # noqa: BLE001 - a raw
        head = b""
        try:
            with path.open("rb") as handle:
                head = handle.read(400_000)
        except OSError:
            return {}
        at = head.find(_EXIF_MARKER)
        if at < 0:
            return {}
        exif = Image.Exif()
        try:
            exif.load(head[at:])
        except Exception:                        # noqa: BLE001 - unreadable
            return {}
    if exif is None:
        return {}
    inner = {}
    try:
        inner = exif.get_ifd(0x8769)
    except Exception:                            # noqa: BLE001 - absent
        inner = {}
    found: dict[str, Any] = {}
    for tag, name in _EXIF_TAGS.items():
        value = exif.get(tag)
        if value is None:
            value = inner.get(tag)
        if value is None:
            continue
        if name in {"seconds", "focal", "aperture"}:
            try:
                found[name] = float(value)
            except (TypeError, ValueError):
                continue
        elif name == "iso":
            try:
                found[name] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            found[name] = str(value)
    return found


def measure(shown: np.ndarray) -> dict[str, float]:
    """What this frame's sky is, and how noisy it is.

    The median is the sky because a night frame is mostly sky; the
    noise is measured from what a small blur cannot explain, which is
    what noise is, and by the median of absolute deviations rather
    than the standard one, so a field of stars does not read as noise.
    """
    from development_engine import _box_mean

    lum = (shown[..., 0] * 0.2126 + shown[..., 1] * 0.7152
           + shown[..., 2] * 0.0722).astype(np.float32)
    sky = float(np.median(lum))
    # The sky's own colour, channel by channel. A city under cloud is
    # orange, a moonlit sky is blue, and neither is grey.
    channels = [float(np.median(shown[..., index])) for index in range(3)]
    residual = lum - _box_mean(lum, 3)
    # 1.4826 makes the median absolute deviation an estimate of sigma.
    noise = float(np.median(np.abs(residual))) * 1.4826
    return {"sky": sky, "noise": max(noise, 1e-6),
            "channels": channels,
            "cast": round(max(channels) - min(channels), 5),
            "brightest": float(np.percentile(lum, 99.99))}


def _lands_at(level: float, black: float, strength: float) -> float:
    """Where a level ends up after the black point and the stretch."""
    lifted = max(level - black, 0.0) / max(1.0 - black, 1e-6)
    factor = 10.0 ** (min(max(strength, 0.0), 100.0) / 25.0) - 1.0
    if factor <= 1e-6:
        return lifted
    return float(math.asinh(factor * lifted) / math.asinh(factor))


def _strength_for(sky: float, black: float, target: float) -> float:
    """The gentlest stretch that puts the sky where it should sit."""
    best, distance = 0.0, 1e9
    for step in range(0, 401):
        strength = step / 4.0
        away = abs(_lands_at(sky, black, strength) - target)
        if away < distance:
            best, distance = strength, away
    return best


def night_start(path: str | Path, shown: np.ndarray | None = None,
                frames: int = 1) -> dict[str, Any]:
    """A first version of a night frame: what to do, and why.

    ``frames`` is how many exposures were averaged into this picture.
    A stack of N has already divided its noise by the square root of
    N, so it needs proportionally less quieting -- which is the whole
    reason to stack, and the reason a stack should not then be
    smoothed as hard as a single frame.
    """
    path = Path(path)
    facts = camera_facts(path)
    if shown is None:
        from superimpose_kernel import _frame

        shown = _frame(path, demosaic=True)
    told = measure(np.asarray(shown, dtype=np.float32))
    sky, noise = told["sky"], told["noise"]
    if sky < DEAD_FRAME and told["brightest"] < DEAD_FRAME * 10:
        return {"operations": [], "measured": told, "camera": facts,
                "note": "This frame is black: there is nothing to lift."}

    # Light pollution first, because everything after it multiplies
    # what it leaves behind. Each channel's own sky floor comes off,
    # down to the darkest of the three -- which removes the cast while
    # leaving the pedestal for the black point to deal with.
    channels = told.get("channels") or [sky, sky, sky]
    floor = min(channels)
    # Exactly to the darkest channel and no less. Leaving a margin
    # "for safety" leaves the cast behind in proportion to the margin,
    # and the stretch then multiplies what was left -- which is how a
    # blue sky becomes a green one.
    offsets = [max(level - floor, 0.0) for level in channels]
    cast = max(offsets)
    # With the cast gone the grey sky sits at the darkest channel's
    # level, and that is what the stretch is aimed at.
    sky = floor

    # The black point sits just under the sky, not on it. Clipping the
    # sky itself would take the faintest stars with it, and they are
    # the ones the whole exercise is for.
    black = max(sky - 3.0 * noise, 0.0)
    strength = _strength_for(sky, black, SKY_TARGET)

    # Noise is lifted by exactly as much as the signal beside it, so
    # what matters is how big it will be AFTER the stretch, not now.
    def noise_after(at: float) -> float:
        lifted = (_lands_at(sky + noise, black, at)
                  - _lands_at(sky, black, at))
        return max(lifted, 1e-6) / math.sqrt(max(int(frames), 1))

    # And a frame cannot be lifted further than its own noise allows.
    # Where the sky is very dark and very noisy -- a short exposure at
    # a high ISO under a bright sky -- reaching the target would
    # amplify the grain past anything smoothing could rescue, so the
    # lift stops where the noise stops it and the note says so.
    reached = True
    while strength > 1.0 and noise_after(strength) > NOISE_CEILING:
        strength -= 0.5
        reached = False
    settled = noise_after(strength)
    quieting = min(max((settled / NOISE_BEARABLE - 1.0) * 55.0, 0.0),
                   92.0)
    # Colour noise is worse than luminance noise at every ISO and is
    # cheaper to remove, because chroma carries no detail a sky needs.
    iso = int(facts.get("iso", 0) or 0)
    colour = min(max((iso / 6400.0) * 70.0, 25.0), 85.0) if iso else 45.0

    operations = []
    if cast > 0.002:
        operations.append({
            "op": "color.sky_offset", "unit": "levels",
            "mode": "absolute",
            "value": {"red": round(offsets[0], 5),
                      "green": round(offsets[1], 5),
                      "blue": round(offsets[2], 5)},
            "source_instruction": "the sky's own colour, taken off",
            "enabled": True})
    operations += [
        {"op": "levels.black_input", "unit": "level-8bit",
         "mode": "absolute", "value": round(black * 255.0, 1),
         "source_instruction": "the sky's own floor, measured",
         "enabled": True},
        {"op": "tone.stretch", "unit": "percent", "mode": "delta",
         "value": round(strength, 1),
         "source_instruction": f"lift the sky to {SKY_TARGET:.2f}",
         "enabled": True},
    ]
    if quieting > 1.0:
        operations.append({
            "op": "detail.night_clean", "unit": "percent",
            "mode": "delta", "value": round(quieting, 1),
            "source_instruction": "quiet the sky, keep the stars",
            "enabled": True})
    operations.append({
        "op": "detail.clean_colour", "unit": "percent", "mode": "delta",
        "value": round(colour, 1),
        "source_instruction": (f"ISO {iso}" if iso else "colour speckle"),
        "enabled": True})
    return {"operations": operations, "measured": told, "camera": facts,
            "black": black, "strength": strength,
            "noise_after": settled, "reached_target": reached,
            "note": note_for(told, facts, frames, strength, quieting,
                             settled, reached)}


def trailing_limit(focal: float, crop: float = 1.5) -> float:
    """Seconds before the stars visibly trail, by the old rule.

    Five hundred divided by the equivalent focal length: rough, older
    than digital, and still the number every photographer reaches for.
    """
    equivalent = max(float(focal) * crop, 1e-6)
    return 500.0 / equivalent


def note_for(told: dict, facts: dict, frames: int, strength: float,
             quieting: float, settled: float,
             reached: bool = True) -> str:
    """What was decided, in the terms it was decided on."""
    parts = []
    if told.get("cast", 0) > 0.002:
        channels = told.get("channels") or []
        parts.append(
            "The sky is not grey -- it measured "
            + "/".join(f"{level:.3f}" for level in channels)
            + " in red, green and blue -- so that colour is subtracted "
            "before anything multiplies it.")
    parts += [
        f"The sky measured {told['sky']:.4f} with noise "
        f"{told['noise']:.4f}; the black point sits just under it and "
        f"a {strength:.0f}% stretch lifts it"
        + (f" to {SKY_TARGET:.2f}, which brings the faint stars up "
           "with it and compresses the bright ones instead of blowing "
           "them." if reached else
           " as far as its own noise allows -- short of where a sky "
           "should sit, because reaching there would magnify the "
           "grain past what any smoothing could rescue. More frames, "
           "or a longer exposure, is the only real answer.")]
    if quieting > 1:
        parts.append(
            f"That stretch multiplies the noise too, so {quieting:.0f}% "
            "of quieting is laid on the background only -- what stands "
            "clear of its surroundings keeps every pixel it had.")
    else:
        parts.append(
            "The stretched noise is already bearable; nothing is "
            "smoothed.")
    if frames > 1:
        parts.append(
            f"{frames} frames were averaged, which had already divided "
            f"the noise by {math.sqrt(frames):.1f}.")
    if facts.get("seconds") and facts.get("focal"):
        limit = trailing_limit(float(facts["focal"]))
        parts.append(
            f"Shot at {facts['seconds']:g}s, ISO "
            f"{facts.get('iso', '?')}, f/{facts.get('aperture', '?')} "
            f"on {facts['focal']:g}mm -- where the stars begin to "
            f"trail past about {limit:.0f}s.")
    return " ".join(parts)


__all__ = ["camera_facts", "measure", "night_start", "note_for",
           "trailing_limit", "SKY_TARGET"]
