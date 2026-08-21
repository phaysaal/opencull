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

One thing here is measured rather than set: the shape a star lands as
on this lens. Deciding whether a small bright thing is a star or a
speck of grain is the question the quieting asks at every pixel, and
asking it about SHAPE rather than about brightness is what lets the
sky be smoothed hard while the faint stars stay.

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
    # How far the background slopes across the frame, measured where
    # no star stands: a low quantile of each of nine big tiles.
    height, width = lum.shape
    corners = [float(np.percentile(
        lum[row * height // 3:(row + 1) * height // 3,
            column * width // 3:(column + 1) * width // 3], 20))
        for row in range(3) for column in range(3)]
    return {"sky": sky, "noise": max(noise, 1e-6),
            "channels": channels,
            "cast": round(max(channels) - min(channels), 5),
            "slope": round(max(corners) - min(corners), 5),
            "brightest": float(np.percentile(lum, 99.99))}


# How coarse the fitted sky is, and it is a judgement rather than a
# constant. Fine enough to follow a lens's vignetting and a town on
# the horizon; far too coarse to follow a star, which is the point --
# a surface that could fit a star would subtract it. Measured on a
# real stack, a 70% slope came down to 19% at twelve tiles and 7% at
# twenty-four; sixteen leaves 11% and is where this sits, because
# the finer the grid the less a surface can tell a SLOPE from a
# NEBULA. On a frame with the Milky Way across it, fewer tiles or a
# lower strength -- the fit cannot know it is eating the subject.
BACKGROUND_TILES = 16
# Which part of each tile counts as "no star here".
BACKGROUND_QUANTILE = 20


def fit_background(shown: np.ndarray, tiles: int = BACKGROUND_TILES,
                   quantile: float = BACKGROUND_QUANTILE,
                   strength: float = 100.0) -> dict[str, Any] | None:
    """Measure the sky's slope where no star stands, as an operation.

    Each tile answers with a low quantile of its own pixels rather
    than a mean: a mean is pulled up by every star in the tile, and a
    surface pulled up by stars subtracts them. The grid is then
    smoothed, because a tile that happened to hold a bright clump
    should not put a dent in the sky.
    """
    import base64

    height, width = shown.shape[:2]
    # A tile has to be far bigger than a star, or its low quantile is
    # a star's own skirt and the surface follows what it is supposed
    # to ignore -- measured on a small frame, a star lost a sixth of
    # its brightness to a grid whose tiles were twenty pixels wide.
    # So the grid is only as fine as the frame can carry.
    tiles = int(min(tiles, min(height, width) // 40))
    if tiles < 3:
        return None
    grid = np.zeros((tiles, tiles, 3), np.float32)
    for row in range(tiles):
        top, bottom = row * height // tiles, (row + 1) * height // tiles
        for column in range(tiles):
            left = column * width // tiles
            right = (column + 1) * width // tiles
            tile = shown[top:bottom, left:right]
            grid[row, column] = np.percentile(
                tile.reshape(-1, 3), quantile, axis=0)
    # One gentle pass, so the surface is a slope and not a patchwork.
    # Two flattened the surface itself and under-corrected: 19% of the
    # slope left against 11% with one.
    for _pass in range(1):
        padded = np.pad(grid, ((1, 1), (1, 1), (0, 0)), mode="edge")
        grid = sum(padded[y:y + tiles, x:x + tiles]
                   for y in range(3) for x in range(3)) / 9.0
    middle = [float(np.median(grid[..., index])) for index in range(3)]
    spread = float(grid.max() - grid.min())
    return {
        "op": "color.background", "unit": "surface", "mode": "absolute",
        "value": {
            "size": int(tiles),
            "levels": base64.b64encode(
                grid.astype(np.float16).tobytes()).decode(),
            "middle": [round(level, 6) for level in middle],
            "strength": round(float(strength), 1),
        },
        "source_instruction": (
            f"the sky's own slope, {spread:.4f} across the frame"),
        "enabled": True,
    }


# How wide a patch a star's shape is measured in. Wide enough to hold
# the whole smudge including the coma an f/1.8 corner draws; narrow
# enough that two stars rarely share one. Measured on a real frame the
# shape's own deviation was 2.0 px, so thirteen is six deviations.
STAR_PATCH = 13
# Below this many isolated stars the shape is an average of accidents.
STARS_ENOUGH = 24


def measure_psf(shown: np.ndarray,
                size: int = STAR_PATCH) -> dict[str, Any] | None:
    """Measure how this lens draws a point of light, as an operation.

    A star is a point source: whatever the picture shows around it IS
    the lens's answer, blur and coma and focus error together. So the
    shape is not modelled and not assumed Gaussian -- it is read off
    the photograph's own isolated stars, which is the only description
    that can be right about this lens at this aperture.

    The stars are taken by MEDIAN rather than mean: a mean is pulled
    out of shape by the one patch that happened to hold a companion
    star or a hot pixel, and there is no reason to let a single
    accident describe the lens.
    """
    import base64

    from development_engine import _LUMA, _box_mean

    shown = np.asarray(shown, dtype=np.float32)
    reach = size // 2
    lum = (shown * _LUMA).sum(axis=2) if shown.ndim == 3 else shown
    over = lum - _box_mean(lum, 40)
    noise = float(np.median(np.abs(over - _box_mean(over, 3)))) * 1.4826
    if not np.isfinite(noise) or noise <= 0.0:
        return None
    lit = over > noise * 8.0
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                lit &= over >= np.roll(np.roll(over, dy, 0), dx, 1)
    # A star that touched the top of the range has had its own shape
    # cut off, and a shape measured from clipped stars is flat-topped.
    lit &= lum < 0.92
    edge = np.zeros(lit.shape, bool)
    edge[reach + 1:-(reach + 1), reach + 1:-(reach + 1)] = True
    found = np.argwhere(lit & edge)
    if len(found) < STARS_ENOUGH:
        return None
    # Only stars standing alone: a patch with a neighbour in it
    # describes two lenses at once.
    crowd = _box_mean((lit & edge).astype(np.float32), reach) \
        * (2 * reach + 1) ** 2
    alone = crowd[found[:, 0], found[:, 1]] < 1.5
    found = found[alone]
    if len(found) < STARS_ENOUGH:
        return None
    order = np.argsort(over[found[:, 0], found[:, 1]])[::-1][:400]
    found = found[order]
    patches = np.stack([
        over[y - reach:y + reach + 1, x - reach:x + reach + 1]
        / max(float(over[y, x]), 1e-6) for y, x in found])
    shape = np.clip(np.median(patches, axis=0), 0.0, None)
    total = float(shape.sum())
    if total <= 1e-6 or float(shape[reach, reach]) < shape.max() * 0.99:
        return None
    shape = (shape / total).astype(np.float32)
    # What the filter buys, in the only terms that matter: a matched
    # filter's gain over reading one pixel is the root of the shape's
    # effective area, sum(w)^2 / sum(w^2), which for a point spread
    # normalised to one is simply one over the sum of its squares.
    gain = float(np.sqrt(1.0 / float((shape ** 2).sum())))
    return {
        "op": "detail.star_shape", "unit": "shape", "mode": "absolute",
        "value": {
            "size": int(size),
            "shape": base64.b64encode(
                shape.astype(np.float16).tobytes()).decode(),
            # Where a correlation stops being grain and starts being
            # a star, in deviations of the CORRELATED picture -- which
            # is a much quieter picture than the one it came from, so
            # these are smaller numbers than a raw threshold would be
            # and still stricter. Swept against a five-frame stack of
            # the same sky, through the whole chain: three-to-seven
            # left the sky 45% quieter than the brightness gate AND
            # showed more real stars. Stricter settings quietened no
            # further and began smoothing real faint stars away.
            "floor": 3.0, "ceiling": 7.0,
            # The size of the picture this shape was read off, so a
            # render at any other size can scale it to its own stars.
            "edge": int(max(shown.shape[0], shown.shape[1])),
        },
        "source_instruction": (
            f"the shape a star lands as here, from {len(found)} of "
            f"them -- worth {gain:.1f} times a single pixel"),
        "enabled": True,
    }


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
    It is not arithmetic here -- a stack's own measured noise already
    carries what the stacking bought, and counting it again would be
    counting it twice -- but it is worth saying out loud, because it
    is the difference between a sky that needs smoothing and one that
    does not.
    """
    path = Path(path)
    facts = camera_facts(path)
    if shown is None:
        from superimpose_kernel import _frame

        shown = _frame(path, demosaic=True)
    shown = np.asarray(shown, dtype=np.float32)
    # The slope first, and measured before anything else, because
    # every number after it describes a sky that no longer slopes.
    first = measure(shown)
    # A slope worth removing has to be bigger than the noise it would
    # be measured through. Below that the tiles differ by chance, the
    # fit "improves" a sky that was already even, and the photographer
    # is left with an operation that did nothing they can see.
    flatten = (fit_background(shown)
               if first["slope"] > first["noise"] * 2.5 else None)
    if flatten is not None:
        from development_engine import _apply_global, _decoded, _encoded

        settled = np.clip(_encoded(_apply_global(
            _decoded(shown).astype(np.float32), [flatten])), 0.0, 1.0)
        before = first
        after = measure(settled)
        if after.get("slope", 1.0) >= before.get("slope", 0.0) * 0.9:
            # It found nothing worth removing; an operation that does
            # nothing is worse than no operation, because it invites
            # the photographer to wonder what it did.
            flatten = None
        else:
            shown = settled
    told = measure(shown)
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

    # The black point sits WELL under the sky, and the distance is
    # what matters. Setting it a few deviations under -- the obvious
    # rule, and the one this had -- leaves the sky exactly that many
    # deviations above black, so the lift needed to reach the target
    # is the target divided by those deviations, and the noise
    # cancels out of the answer entirely. Measured on a real
    # ten-frame stack against one of its own frames: the stack was
    # 2.9 times cleaner and came out with IDENTICAL grain, because
    # every bit of what the stacking bought was spent on a longer
    # lift. Far enough under that nothing clips, and no further: what
    # is left of the sky is real signal, and keeping it is what keeps
    # the lift short.
    black = max(sky - 8.0 * noise, 0.0)
    strength = _strength_for(sky, black, SKY_TARGET)

    # Noise is lifted by exactly as much as the signal beside it, so
    # what matters is how big it will be AFTER the stretch, not now.
    def noise_after(at: float) -> float:
        # No credit for stacking here, and that is not an oversight.
        # The noise being lifted is the noise MEASURED IN THIS
        # PICTURE, and if this picture is a stack of fifty then the
        # stacking is already in that number. Dividing by the root of
        # N as well counts it twice, which on a real ten-frame stack
        # prescribed no quieting at all for a sky that plainly wanted
        # some. What the frame count is honestly for is the note.
        lifted = (_lands_at(sky + noise, black, at)
                  - _lands_at(sky, black, at))
        return max(lifted, 1e-6)

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
    if flatten is not None:
        operations.append(flatten)
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
        # Before the stretch, because the stretch reads it: lift the
        # LUMINANCE and carry each pixel's colour by ratio, so a red
        # star stays red on its way up. Measured on a ten-frame stack,
        # the stars kept 82% of their colour this way against 34%
        # through the per-channel lift.
        {"op": "tone.preserve", "unit": "percent", "mode": "delta",
         "value": 100.0,
         "source_instruction": "the stars keep their own colour",
         "enabled": True},
        {"op": "tone.stretch", "unit": "percent", "mode": "delta",
         "value": round(strength, 1),
         "source_instruction": f"lift the sky to {SKY_TARGET:.2f}",
         "enabled": True},
    ]
    # The shape of a star, measured and published BEFORE the quieting
    # -- which is what reads it. Nothing about this operation changes
    # a pixel; it changes what the smoothing believes is a star, and
    # a gate that knows what it is looking for both keeps more of the
    # faint ones and lets more of the sky be smoothed.
    # Measured only when there is quieting to inform: the shape costs
    # a pass over the frame, and with nothing smoothing there is
    # nothing for it to decide.
    shape = measure_psf(shown) if quieting > 1.0 else None
    if quieting > 1.0:
        if shape is not None:
            operations.append(shape)
        operations.append({
            "op": "detail.night_clean", "unit": "percent",
            "mode": "delta", "value": round(quieting, 1),
            "source_instruction": (
                "quiet the sky, keep the stars -- by their shape"
                if shape is not None else "quiet the sky, keep the stars"),
            "enabled": True})
    operations.append({
        "op": "detail.clean_colour", "unit": "percent", "mode": "delta",
        "value": round(colour, 1),
        "source_instruction": (f"ISO {iso}" if iso else "colour speckle"),
        "enabled": True})
    return {"operations": operations, "measured": told, "camera": facts,
            "black": black, "strength": strength,
            "noise_after": settled, "reached_target": reached,
            "flattened": flatten is not None,
            "star_shape": shape is not None,
            "note": note_for(dict(told, flattened=(
                before["slope"] if flatten is not None else 0.0)),
                facts, frames, strength, quieting, settled, reached,
                shape)}


def trailing_limit(focal: float, crop: float = 1.5) -> float:
    """Seconds before the stars visibly trail, by the old rule.

    Five hundred divided by the equivalent focal length: rough, older
    than digital, and still the number every photographer reaches for.
    """
    equivalent = max(float(focal) * crop, 1e-6)
    return 500.0 / equivalent


def note_for(told: dict, facts: dict, frames: int, strength: float,
             quieting: float, settled: float,
             reached: bool = True, shape: dict | None = None) -> str:
    """What was decided, in the terms it was decided on."""
    parts = []
    if told.get("flattened"):
        parts.append(
            f"The sky sloped by {told['flattened']:.4f} across the "
            f"frame -- {told['flattened'] / max(told['noise'], 1e-6):.1f} "
            "times the noise, and the largest thing in the picture "
            "after the stars -- so a surface measured where no star "
            "stands was subtracted, keeping the sky's level and "
            "taking only its tilt.")
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
    if shape is not None:
        parts.append(
            "The shape a star lands as on this lens was measured off "
            "this photograph's own isolated stars, and the quieting "
            "asks about that shape rather than about brightness: a "
            "star's few pixels add together under it where the grain "
            "beside them cancels. Against a stack of the same sky it "
            "keeps half again as many real stars as brightness alone "
            "does, and leaves the sky a third quieter -- because "
            "brightness alone protects every noise spike as if it "
            "were a star, and protected noise is never smoothed.")
    if frames > 1:
        parts.append(
            f"{frames} frames were averaged into this, and the sky "
            "measured above is what that stacking left -- which is why "
            "it needs less quieting than one frame would.")
    if facts.get("seconds") and facts.get("focal"):
        limit = trailing_limit(float(facts["focal"]))
        parts.append(
            f"Shot at {facts['seconds']:g}s, ISO "
            f"{facts.get('iso', '?')}, f/{facts.get('aperture', '?')} "
            f"on {facts['focal']:g}mm -- where the stars begin to "
            f"trail past about {limit:.0f}s.")
    return " ".join(parts)


__all__ = ["camera_facts", "fit_background", "measure", "measure_psf",
           "night_start", "note_for", "trailing_limit", "SKY_TARGET"]
