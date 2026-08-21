"""Many frames of one sky, laid on top of each other.

Two things a tripod and a dark hour make possible, and neither is a
video: star TRAILS, where the sky's motion is the subject and the
frames are combined by keeping whatever is brightest; and STACKING,
where the sky's motion is the enemy, the frames are registered on the
stars themselves, and averaging many exposures buys the signal-to-noise
of one long one without its blown highlights or its star trailing.

The registration leans on the one thing the sky is honest about: it
turns at a constant rate. A sidereal day is 86164 seconds, so stars
cross 15.041 arcseconds every second, and with a focal length and a
sensor width that is a number of PIXELS per second -- an upper bound on
how far anything can have moved between two frames whose timestamps are
known. Two consequences, and they are what make this cheap:

  * the first pair is solved inside a disc of known radius rather than
    an unbounded plane, by voting: every reference star paired with
    every frame star implies a shift, and the true shift is the one
    that many pairs agree on;
  * once one pair is solved, the drift is a VECTOR PER SECOND, and
    every later frame's shift is predicted rather than searched. Only a
    small window around the prediction is examined, and a frame whose
    prediction fails falls back to the full disc.

Field rotation is not corrected here. On a fixed tripod over a few
minutes it is small; over an hour it is not, and that -- with star
pattern matching for handheld frames -- is the next tier.

Standalone:
  python superimpose_kernel.py trails /path/to/frames "*.RAF"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

FORMAT = "darkimiya-superimpose-v1"
PROGRESS_MARKER = "TIMELAPSE_PROGRESS"

# The sky's own clock: 360 degrees in one sidereal day, in arcseconds
# per second of time.
SIDEREAL_ARCSEC = 360.0 * 3600.0 / 86164.0905
# A sensor's width when nobody says otherwise. APS-C, because that is
# what the cameras in this house are.
DEFAULT_SENSOR_MM = 23.5
# How many of the brightest points are matched between two frames.
STARS_WANTED = 140
# A vote's bin, and how close two stars must land to agree, in pixels.
VOTE_BIN = 2.0
# When a prediction is trusted, how far around it is still examined.
REFINE_PX = 10.0
# Fewer agreeing pairs than this is not a registration, it is a guess.
LEAST_AGREEING = 8

MODES = ("trails", "average", "clipped", "drizzle")

# Drizzle's own two numbers. The grid is finer by SCALE; each input
# pixel is shrunk to PIXFRAC of its width before it is dropped on to
# that grid. At pixfrac 1 a drop is the whole pixel and drizzling
# approaches plain averaging; toward 0 it approaches a point, which
# recovers the most detail and leaves the most holes.
# At pixfrac 1 a drop is the whole pixel and drizzling approaches
# plain averaging; toward 0 it approaches a point, which in theory
# recovers the most detail. In practice a small drop needs MANY more
# frames to fill the finer grid evenly -- measured on twenty dithered
# frames, 0.5 came back noisier AND blunter than 0.8, because each
# output pixel heard from too few drops. So the default is generous
# and the number is offered to whoever has hundreds of frames.
DRIZZLE_SCALE = 2.0
DRIZZLE_PIXFRAC = 0.8


def _say(percent: float, stage: str) -> None:
    print(f"{PROGRESS_MARKER} {max(0, min(100, round(percent)))} {stage}",
          flush=True)


# --- reading frames -------------------------------------------------------

RAW_SUFFIXES = {".raf", ".arw", ".nef", ".cr2", ".cr3", ".dng"}


def _frame(path: Path, demosaic: bool = True) -> np.ndarray:
    """One frame as float display-referred RGB in 0..1.

    Display-referred on purpose: the stack is written as an ordinary
    16-bit TIFF that every tool -- and this application's own develop
    path -- can open, and an encoded 16 bits holds faint sky far better
    than a linear 16 bits does.
    """
    if demosaic and path.suffix.casefold() in RAW_SUFFIXES:
        import rawpy

        try:
            with rawpy.imread(str(path)) as raw:
                pixels = raw.postprocess(
                    use_camera_wb=True, no_auto_bright=True, output_bps=16)
            return pixels.astype(np.float32) / 65535.0
        except Exception:                    # noqa: BLE001 - fall to preview
            pass
    if path.suffix.casefold() in {".tif", ".tiff"}:
        held = _tiff_frame(path)
        if held is not None:
            return held
    from timelapse_kernel import _preview

    return np.asarray(_preview(path).convert("RGB"),
                      dtype=np.float32) / 255.0


def _tiff_frame(path: Path) -> np.ndarray | None:
    """A TIFF read at the depth it was written, or None to fall back.

    Developing every frame first and stacking afterwards is an ordinary
    way to work, and what a developer writes is a TIFF of sixteen bits
    or of floats. Read through the imaging library those arrive as
    eight bits a channel -- and a float one does not open at all -- so
    a stack built that way has thrown most of what stacking is for
    away before it begins.
    """
    try:
        import tifffile
    except ImportError:
        return None
    try:
        held = np.asarray(tifffile.imread(path))
    except Exception:                    # noqa: BLE001 - PIL may still cope
        return None
    if held.ndim == 2:
        held = np.stack([held] * 3, axis=2)
    if held.ndim != 3 or held.shape[2] < 3:
        return None
    held = held[..., :3]
    if np.issubdtype(held.dtype, np.integer):
        # Whole numbers are a fraction of what the type can hold.
        ceiling = float(np.iinfo(held.dtype).max)
        return (held.astype(np.float32) / ceiling).astype(np.float32)
    # Floats are already that fraction, and a developer is entitled to
    # let a highlight sit above one. Clipping is the stack's business,
    # not the reader's.
    return held.astype(np.float32)


def _grey(rgb: np.ndarray) -> np.ndarray:
    return (rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152
            + rgb[..., 2] * 0.0722).astype(np.float32)


def frames_of(photos: str, pattern: str = "*.RAF",
              only: str = "") -> list[Path]:
    """Every frame the run should read, in shooting order by name."""
    from timelapse_kernel import _only_names

    root = Path(str(photos)).expanduser().resolve()
    wanted = _only_names(only)
    found = sorted(path for path in root.glob(pattern or "*.RAF")
                   if path.is_file())
    if wanted:
        found = [path for path in found if path.name in wanted]
    return found


# --- what the sky can have done -------------------------------------------

def drift_bound(seconds: float, focal_mm: float, width_px: int,
                sensor_mm: float = DEFAULT_SENSOR_MM) -> float:
    """The most any star can have moved, in pixels, in that time.

    At the celestial equator a star crosses the full sidereal rate; at
    the pole it barely moves. Declination is unknown, so this is the
    equator's answer -- an upper bound, which is exactly what a search
    radius wants to be.
    """
    if focal_mm <= 0 or width_px <= 0 or sensor_mm <= 0:
        return 0.0
    arcsec_per_px = 206265.0 * (sensor_mm / float(width_px)) / focal_mm
    if arcsec_per_px <= 0:
        return 0.0
    return abs(float(seconds)) * SIDEREAL_ARCSEC / arcsec_per_px


def sky_rotation(seconds: float) -> float:
    """Degrees the sky turns in that time, about the celestial pole.

    On a fixed tripod the whole transformation between two frames IS
    this rotation -- and its angle is not searched for, it is read off
    the clock. Where the pole sits in the frame is unknown, but a
    rotation about an unknown centre is a rotation about a known one
    followed by a translation, and the translation is what the vote
    was already finding. So rotation costs nothing but a pre-turn.
    """
    return float(seconds) * 360.0 / 86164.0905


def turned(points: np.ndarray, degrees: float,
           centre: tuple[float, float]) -> np.ndarray:
    """Star positions rotated about a point, in the image's own axes."""
    if not len(points) or abs(degrees) < 1e-9:
        return points
    angle = np.radians(degrees)
    cos, sin = np.cos(angle), np.sin(angle)
    x = points[:, 0] - centre[0]
    y = points[:, 1] - centre[1]
    return np.stack([centre[0] + x * cos - y * sin,
                     centre[1] + x * sin + y * cos], axis=1).astype(
        np.float32)


def vote_rotation(reference: np.ndarray, moving: np.ndarray,
                  most: int = 30, bin_degrees: float = 0.5,
                  tolerance: float = 3.0) -> tuple[float, int]:
    """The turn many star PAIRS agree on, at any angle at all.

    A hand does not obey the sidereal rate: between two frames the sky
    can have moved anywhere and turned any amount, so nothing bounds
    the search and the clock says nothing. What survives is shape. The
    distance between two stars does not change when the camera moves
    or rolls, so pairs of the same length in both frames are probably
    the same pair -- and the angle between those two pairs is the
    roll. Wrong pairings scatter across the circle; right ones pile up
    on one answer, which is the same argument the shift vote makes,
    one dimension down.
    """
    ref, mov = reference[:most], moving[:most]
    if len(ref) < 3 or len(mov) < 3:
        return 0.0, 0
    ri, rj = np.triu_indices(len(ref), k=1)
    mi, mj = np.triu_indices(len(mov), k=1)
    rdx = ref[rj, 0] - ref[ri, 0]
    rdy = ref[rj, 1] - ref[ri, 1]
    mdx = mov[mj, 0] - mov[mi, 0]
    mdy = mov[mj, 1] - mov[mi, 1]
    ref_len, mov_len = np.hypot(rdx, rdy), np.hypot(mdx, mdy)
    ref_ang = np.degrees(np.arctan2(rdy, rdx))
    mov_ang = np.degrees(np.arctan2(mdy, mdx))
    # Only pairs that could be the same pair: the same length, within
    # what centroiding and a little scale error can explain.
    gap = np.abs(ref_len[:, None] - mov_len[None, :])
    allow = tolerance + ref_len[:, None] * 0.01
    near = (gap < allow) & (ref_len[:, None] > 12.0)
    if not near.any():
        return 0.0, 0
    turns = (ref_ang[:, None] - mov_ang[None, :])[near]
    # A pair has no head or tail, so every answer has a twin half a
    # turn away; folding them together doubles the pile.
    turns = (turns + 180.0) % 180.0
    bins: dict[int, int] = {}
    for value in np.round(turns / bin_degrees).astype(int):
        bins[int(value)] = bins.get(int(value), 0) + 1
    best = max(bins, key=lambda key: bins[key])
    inside = np.round(turns / bin_degrees).astype(int) == best
    return float(turns[inside].mean()), int(bins[best])


# --- finding stars --------------------------------------------------------

def stars_in(grey: np.ndarray, wanted: int = STARS_WANTED,
             above: float = 5.0) -> np.ndarray:
    """The brightest compact points, as (x, y) at subpixel accuracy.

    A star is a small thing brighter than the sky around it, so the sky
    around it is what it is measured against: a wide box mean is the
    background, the deviation of the residual is the noise, and what
    stands well clear of both and is the brightest thing in its own
    neighbourhood is a star.
    """
    from development_engine import _box_mean

    height, width = grey.shape
    reach = max(8, min(height, width) // 40)
    background = _box_mean(grey, reach)
    residual = grey - background
    noise = float(np.std(residual[::4, ::4])) or 1e-6
    lit = residual > noise * above
    if not lit.any():
        return np.zeros((0, 2), np.float32)
    # A local maximum in a 3x3 neighbourhood, done by comparing against
    # the eight shifted copies -- no convolution library needed.
    peak = np.ones_like(lit)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            shifted = np.roll(np.roll(residual, dy, axis=0), dx, axis=1)
            peak &= residual >= shifted
    found = np.argwhere(lit & peak)
    if not len(found):
        return np.zeros((0, 2), np.float32)
    brightness = residual[found[:, 0], found[:, 1]]
    order = np.argsort(brightness)[::-1][:wanted]
    found = found[order]
    # Subpixel centroid in a 5x5 box: a star's true middle is rarely a
    # pixel's middle, and half a pixel of error per frame is a smeared
    # stack.
    points = []
    for y, x in found:
        y0, y1 = max(int(y) - 2, 0), min(int(y) + 3, height)
        x0, x1 = max(int(x) - 2, 0), min(int(x) + 3, width)
        patch = np.clip(residual[y0:y1, x0:x1], 0, None)
        total = float(patch.sum())
        if total <= 0:
            points.append((float(x), float(y)))
            continue
        ys, xs = np.mgrid[y0:y1, x0:x1]
        points.append((float((xs * patch).sum() / total),
                       float((ys * patch).sum() / total)))
    return np.asarray(points, dtype=np.float32)


# --- agreeing on a shift --------------------------------------------------

def _score(reference: np.ndarray, moving: np.ndarray,
           shift: tuple[float, float], tolerance: float = VOTE_BIN
           ) -> int:
    """How many stars land on stars when the frame is moved that far."""
    if not len(reference) or not len(moving):
        return 0
    cell = max(tolerance * 2.0, 1.0)
    buckets: dict[tuple[int, int], list] = {}
    for x, y in reference:
        buckets.setdefault((int(x // cell), int(y // cell)), []).append(
            (x, y))
    agreed = 0
    for x, y in moving:
        px, py = x + shift[0], y + shift[1]
        home = (int(px // cell), int(py // cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for rx, ry in buckets.get((home[0] + dx, home[1] + dy), ()):
                    if abs(rx - px) <= tolerance and abs(ry - py) <= tolerance:
                        agreed += 1
                        break
                else:
                    continue
                break
    return agreed


def vote_shift(reference: np.ndarray, moving: np.ndarray,
               radius: float, bin_px: float = VOTE_BIN,
               centre: tuple[float, float] = (0.0, 0.0)
               ) -> tuple[tuple[float, float], int]:
    """The shift many star pairs agree on, inside a bounded disc.

    Every reference star paired with every moving star implies one
    shift. Wrong pairings scatter; right ones pile up on the same
    answer. The pile is the registration, and the disc -- the most the
    sky can have turned in the time between the frames -- keeps the
    scatter from ever being consulted.
    """
    if not len(reference) or not len(moving):
        return (0.0, 0.0), 0
    dx = reference[:, 0][:, None] - moving[:, 0][None, :]
    dy = reference[:, 1][:, None] - moving[:, 1][None, :]
    away = np.sqrt((dx - centre[0]) ** 2 + (dy - centre[1]) ** 2)
    inside = away <= max(radius, bin_px)
    if not inside.any():
        return (0.0, 0.0), 0
    dx, dy = dx[inside], dy[inside]
    keys: dict[tuple[int, int], int] = {}
    for one, two in zip(np.round(dx / bin_px).astype(int),
                        np.round(dy / bin_px).astype(int)):
        keys[(int(one), int(two))] = keys.get((int(one), int(two)), 0) + 1
    best = max(keys, key=lambda key: keys[key])
    # The bin says roughly where; the pairs inside it say exactly.
    near = ((np.round(dx / bin_px).astype(int) == best[0])
            & (np.round(dy / bin_px).astype(int) == best[1]))
    settled = (float(dx[near].mean()), float(dy[near].mean()))
    return settled, _score(reference, moving, settled, bin_px)


# --- the run --------------------------------------------------------------

def _linear(shown: np.ndarray) -> np.ndarray:
    """Light, from a picture of light.

    Frames are read display-referred, which is right for storing them
    and wrong for doing arithmetic on them: an encoded value is a
    lightness, and lightnesses do not add. Two exposures of the same
    star do not average to the encoded middle of their two encodings,
    and a pedestal the sensor added to the LIGHT does not come off the
    lightness by subtraction.
    """
    from development_engine import _decoded

    return np.clip(_decoded(np.clip(shown, 0.0, None)), 0.0, None
                   ).astype(np.float32)


def _shown(light: np.ndarray) -> np.ndarray:
    """And back again, for anything that has to be looked at."""
    from development_engine import _encoded

    return np.clip(_encoded(np.clip(light, 0.0, None)), 0.0, 1.0
                   ).astype(np.float32)


def light_scales(paths: list[Path]) -> list[float]:
    """How much light each frame gathered, against the first.

    A six-and-a-half second frame is not a noisier three second frame;
    it is a different measurement. It holds more than twice the light
    -- sky and stars together -- and averaging it in as though it were
    the same quantity does two things, both bad. The mean lands
    between two scales, so the stars lose contrast against a sky that
    was lifted more than they were; and the sigma clip, which throws
    out whatever sits far from what the frames agree on, now sees a
    spread at EVERY pixel that is the exposure difference rather than
    a satellite. It rejects good light and keeps what it was built to
    remove.

    Shutter, sensitivity and aperture together, because all three
    decide how much light arrived. Where any frame cannot say, they
    all count the same -- which is what this did before, and a guess
    applied to some frames and not others is worse than no guess.
    """
    from opencull_gui.nightstart import camera_facts

    gathered = []
    for path in paths:
        facts = camera_facts(path)
        seconds = float(facts.get("seconds", 0.0) or 0.0)
        if seconds <= 0.0:
            return [1.0] * len(paths)
        speed = float(facts.get("iso", 0) or 0) or 100.0
        stop = float(facts.get("aperture", 0.0) or 0.0)
        # Light per unit area goes as the square of the aperture ratio;
        # an unknown aperture is simply left out of the comparison,
        # which is right when it did not change and unknowable when it
        # did.
        gathered.append(seconds * speed / (stop ** 2 if stop > 0 else 1.0))
    first = gathered[0]
    if first <= 0.0 or not all(level > 0.0 for level in gathered):
        return [1.0] * len(paths)
    return [first / level for level in gathered]


def _capture_seconds(paths: list[Path]) -> list[float | None]:
    from opencull_gui.scenes import capture_time

    return [capture_time(path) for path in paths]


def _median_of(folder: str, pattern: str,
               demosaic: bool) -> np.ndarray | None:
    """The middle of a folder of calibration frames, or None.

    The median rather than the mean throughout: a calibration frame's
    job is to describe what the camera does, and one cosmic ray should
    not become part of that description.
    """
    if not str(folder).strip():
        return None
    frames = frames_of(folder, pattern)
    if len(frames) < 2:
        return None
    # In light, not in lightness. A bias is a pedestal the sensor adds
    # to the LIGHT it measured, and a flat is a factor the light was
    # multiplied by; neither is a statement about the encoded picture,
    # so both are read back into light before they are used.
    return np.median(
        np.stack([_linear(_frame(path, demosaic)) for path in frames]),
        axis=0).astype(np.float32)


def _master_flat(folder: str, pattern: str, demosaic: bool,
                 bias: np.ndarray | None = None) -> np.ndarray | None:
    """What the lens and the sensor do to an evenly lit field.

    A flat carries three things at once: the lens's vignetting, the
    shadow of every speck on the sensor, and each pixel's own
    sensitivity. Dividing by it takes all three out. It is normalised
    per channel rather than overall, so it corrects the SHAPE of the
    illumination and not its colour -- a flat shot on a warm panel
    would otherwise cool every frame it touched.
    """
    flat = _median_of(folder, pattern, demosaic)
    if flat is None:
        return None
    if bias is not None and bias.shape == flat.shape:
        flat = flat - bias
    for channel in range(flat.shape[2]):
        middle = float(np.median(flat[..., channel]))
        if middle > 1e-4:
            flat[..., channel] /= middle
    # A flat near zero somewhere would divide a light frame into
    # nonsense there; the floor keeps the correction bounded.
    return np.clip(flat, 0.2, 5.0).astype(np.float32)


def _master_dark(folder: str, pattern: str,
                 demosaic: bool) -> np.ndarray | None:
    """The camera's own fixed pattern, from frames of the lens cap on.

    The median rather than the mean: a dark frame's job is to describe
    what the sensor does with no light, and one cosmic ray should not
    become part of that description.
    """
    return _median_of(folder, pattern, demosaic)


# How far a Lanczos kernel reaches, in pixels either side. Three is
# the photographic standard: two is visibly softer on a point source
# and four buys nothing back but ringing.
LANCZOS_TAPS = 3
# The kernel is a sinc times a sinc, which is far too slow to evaluate
# at twenty-six million pixels thirty-six times over. It is a smooth
# function of one variable, so it is tabulated once and read.
#
# The step count is not round by accident. The table must hold every
# WHOLE-pixel distance exactly on an entry of its own, because at
# whole distances the kernel is exactly one and exactly zero -- which
# is what makes moving a frame by a whole number of pixels, or not
# turning it at all, cost it nothing. Land those between two entries
# and an identity move quietly softens the frame. So the table has
# 2*taps*K + 1 entries, K per pixel.
_LANCZOS_PER_PIXEL = 1365
_LANCZOS_STEPS = 2 * LANCZOS_TAPS * _LANCZOS_PER_PIXEL + 1


def _lanczos_table(taps: int = LANCZOS_TAPS,
                   steps: int = _LANCZOS_STEPS) -> np.ndarray:
    """The Lanczos kernel, sampled fine enough to read instead of solve."""
    span = np.linspace(-taps, taps, steps, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        weight = np.sinc(span) * np.sinc(span / taps)
    weight[np.abs(span) >= taps] = 0.0
    return weight.astype(np.float32)


_LANCZOS = _lanczos_table()


def _lanczos_at(distance: np.ndarray, taps: int = LANCZOS_TAPS,
                table: np.ndarray | None = None) -> np.ndarray:
    """The kernel's value at each distance, by table lookup."""
    table = _LANCZOS if table is None else table
    steps = table.shape[0]
    where = (distance + taps) * ((steps - 1) / (2.0 * taps))
    # Rounded, not truncated: truncation lands a whole-pixel distance
    # one step short of the entry that holds its exact zero, and the
    # identity stops being the identity.
    return table[np.clip(np.rint(where), 0, steps - 1).astype(np.int32)]


def _lanczos_line(length: int, offset: float,
                  taps: int = LANCZOS_TAPS) -> tuple[np.ndarray, np.ndarray]:
    """Where to read and how much, for one axis of a constant shift.

    A shift that is the same everywhere is not resampling at all --
    it is a convolution, and the same handful of weights serves every
    pixel in the frame. Worth separating out, because most of these
    stacks are a tripod's slow drift with no turn in them at all.
    """
    base = int(np.floor(offset))
    taken = np.arange(base - taps + 1, base + taps + 1)
    weights = _lanczos_at(np.asarray(offset - taken, np.float32), taps)
    total = float(weights.sum())
    if abs(total) > 1e-6:
        weights = weights / total
    return taken, weights.astype(np.float32)


def _corner_bounds(frame: np.ndarray, y0: np.ndarray,
                   x0: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The range of the four pixels a sample sits between."""
    a = frame[y0, x0]
    b = frame[y0, x0 + 1]
    c = frame[y0 + 1, x0]
    d = frame[y0 + 1, x0 + 1]
    low = np.minimum(np.minimum(a, b), np.minimum(c, d))
    high = np.maximum(np.maximum(a, b), np.maximum(c, d))
    return low, high


def _unrung(out: np.ndarray, frame: np.ndarray,
            dx: float, dy: float) -> np.ndarray:
    """Hold a resampled pixel inside the range it was drawn from.

    A Lanczos kernel is negative either side of its peak, which is
    where its sharpness comes from and also where its ringing does: at
    a steep enough edge it overshoots above and undershoots below, and
    the undershoot leaves a dark rim. Against a night sky that hardly
    showed while the arithmetic was done on ENCODED values, because
    encoding compresses a star's range to something gentle. Done in
    light it is not gentle at all -- a star is two thousand times its
    sky -- and the rims became visible enough to widen every star by a
    fifth on a synthetic field.

    So a sample is held between the smallest and largest of the four
    pixels it sits between. Interpolation cannot honestly produce a
    value outside the values it interpolated, and refusing to lets the
    kernel keep all of its sharpness and none of its overshoot.
    """
    height, width = frame.shape[:2]
    base_x = int(np.floor(dx))
    base_y = int(np.floor(dy))
    columns = np.clip(np.arange(width) + base_x, 0, width - 2)
    rows = np.clip(np.arange(height) + base_y, 0, height - 2)
    low, high = _corner_bounds(frame, rows[:, None], columns[None, :])
    return np.clip(out, low, high)


def _lanczos_shift(frame: np.ndarray, shift: tuple[float, float],
                   taps: int = LANCZOS_TAPS) -> np.ndarray:
    """A frame moved by a subpixel amount, separably and sharply."""
    height, width = frame.shape[:2]
    out = np.zeros(frame.shape, np.float32)
    seen = np.zeros((height, width), np.float32)
    taken_x, weight_x = _lanczos_line(width, -float(shift[0]), taps)
    taken_y, weight_y = _lanczos_line(height, -float(shift[1]), taps)
    # Along x first, into a strip that still has every row it started
    # with, then along y. Two passes of six taps rather than one of
    # thirty-six.
    middle = np.zeros(frame.shape, np.float32)
    covered = np.zeros((height, width), np.float32)
    columns = np.arange(width)
    for offset, weight in zip(taken_x, weight_x):
        source = columns + offset
        good = (source >= 0) & (source < width)
        if not good.any():
            continue
        middle[:, good] += frame[:, source[good]] * weight
        covered[:, good] += weight
    rows = np.arange(height)
    for offset, weight in zip(taken_y, weight_y):
        source = rows + offset
        good = (source >= 0) & (source < height)
        if not good.any():
            continue
        out[good] += middle[source[good]] * weight
        seen[good] += covered[source[good]] * weight
    out = _unrung(out, frame, -float(shift[0]), -float(shift[1]))
    # Where the kernel hung off the edge it collected less than a
    # whole pixel's worth, and the frame would darken at its rim.
    # Anything not fully covered is outside, and outside is black --
    # which is what the stack already knows how to ignore.
    return np.where((seen > 0.999)[..., None], out, 0.0).astype(np.float32)


def _lanczos_warp(frame: np.ndarray, degrees: float,
                  shift: tuple[float, float],
                  taps: int = LANCZOS_TAPS,
                  band: int = 384) -> np.ndarray:
    """A frame turned and moved, sampled by Lanczos at every pixel.

    A turn sends every output pixel to its own fractional place, so
    the weights differ pixel by pixel and the separable trick a plain
    shift enjoys does not apply. Still separable WITHIN a pixel: the
    weight on a source pixel is the x kernel times the y kernel, so
    the six-by-six neighbourhood is a product of six numbers and six
    numbers rather than thirty-six sinc evaluations.

    Done a band of rows at a time, and both sets of weights worked out
    once per band. The whole frame at once would want a gigabyte of
    weights; a band wants a manageable slice of that, stays in cache,
    and turned a fifty-five second pass into a far shorter one.
    """
    height, width = frame.shape[:2]
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    angle = np.radians(-degrees)
    cos, sin = float(np.cos(angle)), float(np.sin(angle))
    out = np.zeros(frame.shape, np.float32)
    columns = np.arange(width, dtype=np.float32) - shift[0] - cx
    reach = range(-taps + 1, taps + 1)
    for top in range(0, height, band):
        low = min(top + band, height)
        rows = (np.arange(top, low, dtype=np.float32)
                - shift[1] - cy)[:, None]
        sx = cx + columns[None, :] * cos - rows * sin
        sy = cy + columns[None, :] * sin + rows * cos
        x0 = np.floor(sx).astype(np.int32)
        y0 = np.floor(sy).astype(np.int32)
        inside = ((x0 >= taps - 1) & (x0 < width - taps)
                  & (y0 >= taps - 1) & (y0 < height - taps))
        x0c = np.clip(x0, taps - 1, width - taps - 1)
        y0c = np.clip(y0, taps - 1, height - taps - 1)
        # Both kernels for this band, once.
        weight_x = [_lanczos_at(sx - (x0c + step), taps) for step in reach]
        weight_y = [_lanczos_at(sy - (y0c + step), taps) for step in reach]
        piece = np.zeros((low - top, width, frame.shape[2]), np.float32)
        total = np.zeros((low - top, width), np.float32)
        for dy, wy in zip(reach, weight_y):
            rows_at = y0c + dy
            for dx, wx in zip(reach, weight_x):
                weight = wx * wy
                piece += frame[rows_at, x0c + dx] * weight[..., None]
                total += weight
        floor, ceiling = _corner_bounds(frame, y0c, x0c)
        # The tabulated kernel does not sum to exactly one at an
        # arbitrary offset, and an unnormalised resample brightens and
        # darkens in a fine pattern across the frame -- which on a
        # flat sky is the one place there is nothing to hide it behind.
        piece /= np.maximum(total, 1e-6)[..., None]
        piece = np.clip(piece, floor, ceiling)
        out[top:low] = np.where(inside[..., None], piece, 0.0)
    return out


def _warped(frame: np.ndarray, degrees: float,
            shift: tuple[float, float]) -> np.ndarray:
    """The frame turned and moved, sampled between its own pixels.

    Bilinear rather than nearest: a star landing half a pixel off in
    every frame is a stack of smeared stars, and rounding the shift
    away throws out exactly the subpixel accuracy the centroids were
    computed to have. What falls outside is left black, and the stack
    sees it as the black it is.
    """
    height, width = frame.shape[:2]
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    # Where each output pixel came from: undo the move, then the turn.
    px = xx - shift[0] - cx
    py = yy - shift[1] - cy
    angle = np.radians(-degrees)
    cos, sin = np.cos(angle), np.sin(angle)
    sx = cx + px * cos - py * sin
    sy = cy + px * sin + py * cos
    x0 = np.floor(sx).astype(np.int32)
    y0 = np.floor(sy).astype(np.int32)
    fx = (sx - x0)[..., None]
    fy = (sy - y0)[..., None]
    inside = ((x0 >= 0) & (x0 < width - 1)
              & (y0 >= 0) & (y0 < height - 1))
    x0c = np.clip(x0, 0, width - 2)
    y0c = np.clip(y0, 0, height - 2)
    top = (frame[y0c, x0c] * (1 - fx) + frame[y0c, x0c + 1] * fx)
    low = (frame[y0c + 1, x0c] * (1 - fx)
           + frame[y0c + 1, x0c + 1] * fx)
    out = top * (1 - fy) + low * fy
    return np.where(inside[..., None], out, 0.0).astype(np.float32)


def _shifted(frame: np.ndarray, shift: tuple[float, float]) -> np.ndarray:
    """The frame moved by whole pixels, with what leaves it left black."""
    dx, dy = int(round(shift[0])), int(round(shift[1]))
    if dx == 0 and dy == 0:
        return frame
    out = np.zeros_like(frame)
    height, width = frame.shape[:2]
    sy0, sy1 = max(0, -dy), min(height, height - dy)
    ty0, ty1 = max(0, dy), min(height, height + dy)
    sx0, sx1 = max(0, -dx), min(width, width - dx)
    tx0, tx1 = max(0, dx), min(width, width + dx)
    if sy1 > sy0 and sx1 > sx0:
        out[ty0:ty1, tx0:tx1] = frame[sy0:sy1, sx0:sx1]
    return out


# A tripod knocked mid-burst does not move the sky rigidly: the lens
# bends each pointing differently, and after the best shift and turn
# a residual FIELD of small displacements remains, growing toward the
# corners. Left alone it doubles the corner stars and fails the
# roundness gate. Measured on a coarse grid and interpolated, it can
# be folded into the same single resample as the rigid move.
FIELD_BAR = 0.6
FIELD_CORNER = 1.0
FIELD_DONE = 0.35
FIELD_QUALITY = 8.0


def _measure_field(reference: np.ndarray, moving: np.ndarray
                   ) -> dict[str, Any] | None:
    """The residual displacement grid between two aligned greys.

    Windowed phase correlation at each grid cell, refined to a
    fraction of a pixel by a quadratic fit around the peak. A cell
    whose correlation peak barely rises above its own floor -- cloud,
    vignette-black corner -- inherits the median of its sound
    neighbours rather than voting nonsense. If most cells are unsound
    the whole measurement declines to exist.
    """
    height, width = reference.shape[:2]
    win = 512 if min(height, width) >= 2048 else max(
        64, 1 << int(np.log2(max(min(height, width) // 4, 64))))
    half = win // 2
    if height < 3 * half or width < 3 * half:
        return None
    rows = 5 if height >= 2048 else 3
    cols = 7 if width >= 2048 else 3
    cys = np.linspace(half + 8, height - half - 8, rows).astype(int)
    cxs = np.linspace(half + 8, width - half - 8, cols).astype(int)
    han = np.hanning(win)[:, None] * np.hanning(win)[None, :]

    def offset_at(cy: int, cx: int) -> tuple[float, float, float]:
        def cut(grey: np.ndarray) -> np.ndarray:
            piece = grey[cy - half:cy + half, cx - half:cx + half].copy()
            piece -= np.median(piece)
            return piece * han
        # A window that is partly outside the frame's coverage -- the
        # black border a shift or turn leaves -- correlates its own
        # edge, not the sky. Blocked, not unsound: it says nothing
        # about the frame, for or against.
        for grey in (moving, reference):
            piece = grey[cy - half:cy + half, cx - half:cx + half]
            if float(np.mean(piece <= 1e-4)) > 0.15:
                return 0.0, 0.0, -1.0
        a, b = cut(moving), cut(reference)
        corr = np.fft.fftshift(np.fft.irfft2(
            np.fft.rfft2(a) * np.conj(np.fft.rfft2(b))))
        peak = np.unravel_index(np.argmax(corr), corr.shape)
        quality = float(corr[peak] / (np.abs(corr).mean() + 1e-12))

        def refine(low: float, mid: float, high: float) -> float:
            den = low - 2.0 * mid + high
            return 0.0 if den == 0 else 0.5 * (low - high) / den

        py, px = peak
        if not (0 < py < win - 1 and 0 < px < win - 1):
            return 0.0, 0.0, 0.0
        down = py - half + refine(corr[py - 1, px], corr[py, px],
                                  corr[py + 1, px])
        across = px - half + refine(corr[py, px - 1], corr[py, px],
                                    corr[py, px + 1])
        return down, across, quality

    downs = np.zeros((rows, cols))
    acrosses = np.zeros((rows, cols))
    sound = np.zeros((rows, cols), bool)
    blocked = np.zeros((rows, cols), bool)
    for i, cy in enumerate(cys):
        for j, cx in enumerate(cxs):
            downs[i, j], acrosses[i, j], quality = offset_at(cy, cx)
            blocked[i, j] = quality < 0.0
            sound[i, j] = (quality >= FIELD_QUALITY
                           and abs(downs[i, j]) <= half / 4
                           and abs(acrosses[i, j]) <= half / 4)
    # A real bend is SMOOTH: at grid scale it is close to an affine
    # field, and a cell that defies the affine everyone else agrees on
    # -- a satellite streak, a cloud edge -- is a lie, however sharp
    # its correlation peak looked. Fit, reject the defiant, refit; the
    # unsound cells then inherit the fit's own prediction, so nothing
    # a single window claimed can bend the whole frame.
    # The verdict must rest on a MAJORITY of the cells that could
    # speak at all: a border window the coverage blocked is not a
    # vote against the frame, it is an empty chair -- a turned frame
    # blacks out its corners and still deserves its measurement. But
    # the majority must be a real crowd: with only a handful of
    # cells, an affine can pass straight through an outlier instead
    # of exposing it, and a streak becomes a tilt for the frame.
    measurable = int(sound.size - blocked.sum())
    if sound.sum() < max(6, (2 * measurable + 2) // 3):
        return None
    # The trigger reads the raw sound cells alone -- a genuine bend
    # moves most of them; one polluted window moves one.
    typical = float(np.median(np.hypot(downs[sound], acrosses[sound])))

    # The smoothness a bend is held to. A lens does not bend affinely
    # -- pincushion pushes BOTH edges one way while the middle sits
    # still, which is a quadratic shape -- so a grid dense enough to
    # support six terms per axis is fitted quadratically, and only
    # the sparse grids of small frames fall back to the affine.
    normal_x = (np.asarray(cxs, np.float64) - width / 2.0) / width
    normal_y = (np.asarray(cys, np.float64) - height / 2.0) / height

    def terms_at(nx: np.ndarray, ny: np.ndarray) -> np.ndarray:
        flat = [nx, ny, np.ones_like(nx)]
        if rows * cols >= 20:
            flat = [nx * nx, nx * ny, ny * ny] + flat
        return np.stack(flat, -1)

    def model_of(mask: np.ndarray) -> np.ndarray | None:
        picked = np.nonzero(mask)
        if len(picked[0]) < max(3, mask.size // 2):
            return None
        design = terms_at(normal_x[picked[1]], normal_y[picked[0]])
        fit_a, *_ = np.linalg.lstsq(design, acrosses[mask], rcond=None)
        fit_d, *_ = np.linalg.lstsq(design, downs[mask], rcond=None)
        return np.stack([fit_a, fit_d])

    grid_x = terms_at(np.broadcast_to(normal_x, (rows, cols)),
                      np.broadcast_to(normal_y[:, None], (rows, cols)))
    fit = model_of(sound)
    if fit is None:
        return None
    told_across = grid_x @ fit[0]
    told_down = grid_x @ fit[1]
    defiant = (np.hypot(downs - told_down, acrosses - told_across)
               > 2.5) & sound
    if defiant.any():
        fit = model_of(sound & ~defiant)
        if fit is None:
            return None
        told_across = grid_x @ fit[0]
        told_down = grid_x @ fit[1]
        sound &= ~defiant
    downs = np.where(sound, downs, told_down)
    acrosses = np.where(sound, acrosses, told_across)
    worst = float(max(np.abs(downs).max(), np.abs(acrosses).max()))
    # A knock's bend concentrates in the corners: its middle may sit
    # still while the edges walk. The filled grid's farthest reach --
    # affine-extrapolated where the corners were blocked -- is the
    # honest size of that.
    spread = float(np.hypot(downs, acrosses).max())
    return {"downs": downs, "acrosses": acrosses,
            "cys": cys, "cxs": cxs, "worst": worst,
            "typical": typical, "spread": spread}


def _field_rows(field: dict[str, Any], width: int, y0: int, y1: int
                ) -> tuple[np.ndarray, np.ndarray]:
    """The field, bilinearly interpolated over rows y0..y1."""
    cys, cxs = field["cys"], field["cxs"]
    xs = np.arange(width, dtype=np.float32)
    down_rows = np.stack([np.interp(xs, cxs, field["downs"][i])
                          for i in range(len(cys))])
    across_rows = np.stack([np.interp(xs, cxs, field["acrosses"][i])
                            for i in range(len(cys))])
    ys = np.arange(y0, y1, dtype=np.float32)
    seg = np.clip(np.searchsorted(cys, ys) - 1, 0, len(cys) - 2)
    t = np.clip((ys - cys[seg]) / np.maximum(
        cys[seg + 1] - cys[seg], 1), 0.0, 1.0)[:, None]
    return ((1 - t) * down_rows[seg] + t * down_rows[seg + 1],
            (1 - t) * across_rows[seg] + t * across_rows[seg + 1])


def _field_warp(frame: np.ndarray, degrees: float,
                shift: tuple[float, float], field: dict[str, Any],
                taps: int = LANCZOS_TAPS,
                band: int = 384) -> np.ndarray:
    """The rigid move and the residual field in ONE resample.

    The field says where each output pixel's true counterpart sits
    relative to its rigidly-placed one; sampling the composition
    directly costs the frame a single Lanczos pass, where correcting
    an already-warped frame would soften it twice.
    """
    height, width = frame.shape[:2]
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    angle = np.radians(-degrees)
    cos, sin = float(np.cos(angle)), float(np.sin(angle))
    out = np.zeros(frame.shape, np.float32)
    xs = np.arange(width, dtype=np.float32)
    reach = range(-taps + 1, taps + 1)
    for top in range(0, height, band):
        low = min(top + band, height)
        field_down, field_across = _field_rows(field, width, top, low)
        columns = (xs[None, :] + field_across) - shift[0] - cx
        rows = (np.arange(top, low, dtype=np.float32)[:, None]
                + field_down) - shift[1] - cy
        sx = cx + columns * cos - rows * sin
        sy = cy + columns * sin + rows * cos
        x0 = np.floor(sx).astype(np.int32)
        y0 = np.floor(sy).astype(np.int32)
        inside = ((x0 >= taps - 1) & (x0 < width - taps)
                  & (y0 >= taps - 1) & (y0 < height - taps))
        x0c = np.clip(x0, taps - 1, width - taps - 1)
        y0c = np.clip(y0, taps - 1, height - taps - 1)
        weight_x = [_lanczos_at(sx - (x0c + step), taps)
                    for step in reach]
        weight_y = [_lanczos_at(sy - (y0c + step), taps)
                    for step in reach]
        piece = np.zeros((low - top, width, frame.shape[2]), np.float32)
        total = np.zeros((low - top, width), np.float32)
        for dy, wy in zip(reach, weight_y):
            rows_at = y0c + dy
            for dx, wx in zip(reach, weight_x):
                weight = wx * wy
                piece += frame[rows_at, x0c + dx] * weight[..., None]
                total += weight
        floor, ceiling = _corner_bounds(frame, y0c, x0c)
        piece /= np.maximum(total, 1e-6)[..., None]
        piece = np.clip(piece, floor, ceiling)
        out[top:low] = np.where(inside[..., None], piece, 0.0)
    return out


def _added_fields(first: dict[str, Any], second: dict[str, Any]
                  ) -> dict[str, Any]:
    """Two rounds of the same grid, folded into one field."""
    downs = first["downs"] + second["downs"]
    acrosses = first["acrosses"] + second["acrosses"]
    return {"downs": downs, "acrosses": acrosses,
            "cys": first["cys"], "cxs": first["cxs"],
            "worst": float(max(np.abs(downs).max(),
                               np.abs(acrosses).max())),
            "typical": float(np.median(np.hypot(downs, acrosses))),
            "spread": float(np.hypot(downs, acrosses).max())}


def _best_placement(anchors: np.ndarray, moving: np.ndarray,
                    angles: list[float], centre: tuple[float, float],
                    radius: float, guess: tuple[float, float] | None
                    ) -> tuple[float, tuple[float, float], int]:
    """Of the angles worth trying, the one whose stars agree most.

    Every candidate turn is settled by asking the same question the
    translation vote already answers -- how many stars land on stars
    -- so a wrong angle is not argued about, it simply scores badly.
    """
    best = (0.0, (0.0, 0.0), 0)
    for degrees in angles:
        spun = turned(moving, degrees, centre)
        if guess is not None:
            shift, agreed = vote_shift(
                anchors, spun, REFINE_PX, centre=guess)
            if agreed >= LEAST_AGREEING:
                if agreed > best[2]:
                    best = (degrees, shift, agreed)
                continue
        shift, agreed = vote_shift(anchors, spun, radius)
        if agreed > best[2]:
            best = (degrees, shift, agreed)
    return best


def _frame_weights(frames: list[dict[str, Any]],
                   scales: list[float] | None = None) -> np.ndarray:
    """What each frame is worth in an average: one over its variance.

    The least-noise way to combine measurements of one quantity is to
    weight each by one over its own variance -- so a frame twice as
    grainy as its neighbour counts a quarter as much, not half, and
    certainly not the same. The noise was measured while registering.

    Guarded rather than trusted. A frame reporting no noise at all
    would take the whole stack to itself, and one reporting absurdly
    little is far more likely to be a measurement that went wrong
    than a frame that is genuinely perfect, so no frame is allowed to
    outweigh the median frame by more than four times. Frames that
    said nothing about their noise all weigh the same, which is the
    plain average this replaced.
    """
    told = [float(item.get("noise", 0.0) or 0.0) for item in frames]
    usable = [level for level in told if level > 1e-9]
    if len(usable) < len(told) or not usable:
        return np.ones(len(told), np.float64)
    middle = float(np.median(usable))
    floor = middle / 2.0
    kept = [max(level, floor) for level in told]
    # Putting a frame on the common scale multiplies its noise by the
    # same factor it multiplies its signal by, so the noise being
    # weighed here is the noise AFTER that -- otherwise a long
    # exposure, scaled down and thereby quietened, would be given the
    # weight of the noisy thing it was before.
    if scales is not None and len(scales) == len(kept):
        kept = [level * abs(float(scale)) if scale else level
                for level, scale in zip(kept, scales)]
    weights = np.array([1.0 / level ** 2 for level in kept],
                       dtype=np.float64)
    return weights / float(weights.max())


def _frame_noise(grey: np.ndarray) -> float:
    """How grainy one frame is, in the units the frame is stored in.

    By the median of absolute deviations from a small blur, times the
    constant that makes it a standard deviation -- so a field of stars
    does not read as noise the way a plain deviation would.
    """
    from development_engine import _box_mean

    # Measured in light rather than in lightness, because light is
    # what the stack averages and what the weights below are about.
    plane = _linear(np.asarray(grey, np.float32))
    return float(np.median(np.abs(plane - _box_mean(plane, 3)))) * 1.4826


# A turn smaller than this tells you almost nothing about where the
# sky is turning: the frame barely moved, and dividing by how little it
# moved amplifies whatever error the vote had into a wild pole.
LEAST_TURN_FOR_POLE = 0.01
# How far a frame may sit from where a rigid sky says it should before
# the model is not describing this sequence at all.
POLE_FIT_PX = 6.0


def pole_shift(pole: tuple[float, float], degrees: float,
               centre: tuple[float, float]) -> tuple[float, float]:
    """The shift a turn about that pole implies, with nothing free.

    Turning about a pole and turning about the frame's middle differ
    by exactly one translation, and this is it. Which means: given the
    pole, a frame's place is not searched for at all. The clock says
    how far the sky went and this says where that puts it.
    """
    angle = np.radians(degrees)
    cos, sin = float(np.cos(angle)), float(np.sin(angle))
    dx = pole[0] - centre[0]
    dy = pole[1] - centre[1]
    return ((1.0 - cos) * dx + sin * dy,
            -sin * dx + (1.0 - cos) * dy)


def pole_from(frames: list[dict[str, Any]],
              centre: tuple[float, float]) -> dict[str, Any] | None:
    """Where the sky turned about, solved from the frames already placed.

    Every frame was placed as a turn about the middle of the picture
    followed by a shift, and that pair is not two independent facts:
    a sky rotates about ONE point, and both the turn and the shift are
    consequences of where that point is. So each frame contributes two
    equations in the pole's two coordinates,

        (I - R(theta)) (P - c) = t

    and the whole sequence is one small overdetermined system. Solved
    together rather than frame by frame, which matters: a frame whose
    turn is tiny divides by nearly nothing and answers wildly on its
    own, while in a least-squares fit its rows are simply small and it
    quietly counts for little.

    On the user's ten-frame album this lands the pole 2705 px from the
    middle of the picture -- above the top edge, off the photograph,
    which is exactly where an ordinary night shot's pole is.
    """
    rows, answers, kept = [], [], []
    for item in frames:
        turn = float(item.get("turn", 0.0) or 0.0)
        if (int(item.get("agreed", 0)) < LEAST_AGREEING
                or abs(turn) < LEAST_TURN_FOR_POLE):
            continue
        angle = np.radians(turn)
        cos, sin = float(np.cos(angle)), float(np.sin(angle))
        rows += [[1.0 - cos, sin], [-sin, 1.0 - cos]]
        answers += [float(item.get("dx", 0.0)), float(item.get("dy", 0.0))]
        kept.append(item)
    if len(kept) < 2:
        return None
    matrix = np.asarray(rows, np.float64)
    wanted = np.asarray(answers, np.float64)
    # An ill-conditioned system here is not a failure to solve, it is
    # the sequence genuinely not saying where its pole is -- too short
    # a span, or too small a turn across all of it.
    singular = np.linalg.svd(matrix, compute_uv=False)
    if singular[-1] <= 1e-9 or singular[0] / singular[-1] > 1e6:
        return None
    # Fitted more than once, dropping whatever will not agree. A frame
    # that registered on a handful of coincidentally-matching stars
    # reports a shift that is simply wrong, and least squares handed a
    # wrong answer does not ignore it -- it splits the difference, and
    # the pole ends up somewhere that fits nothing. Measured on the
    # user's own ten-frame album: one such frame took the fit from
    # under a pixel to seven and a half, which was enough to make the
    # whole sequence look like a sky that does not rotate.
    keep = np.ones(len(kept), bool)
    offset = np.zeros(2)
    for _round in range(3):
        rows_in = np.repeat(keep, 2)
        if int(keep.sum()) < 2:
            return None
        offset, *_ = np.linalg.lstsq(matrix[rows_in], wanted[rows_in],
                                     rcond=None)
        apart = np.hypot(*(matrix @ offset - wanted).reshape(-1, 2).T)
        middle = float(np.median(apart[keep]))
        # Generous, and with a floor: where every frame agrees to a
        # tenth of a pixel, three times the median is still a tenth of
        # a pixel and would throw away good frames for nothing.
        allowed = max(middle * 3.0, 1.0)
        fresh = apart <= allowed
        if bool(np.array_equal(fresh, keep)):
            break
        keep = fresh
    used = int(keep.sum())
    if used < 2:
        return None
    pole = (float(centre[0] + offset[0]), float(centre[1] + offset[1]))
    agreeing = [float(np.hypot(*(
        np.asarray(pole_shift(pole, float(item["turn"]), centre))
        - np.array([float(item["dx"]), float(item["dy"])]))))
        for item, taken in zip(kept, keep) if taken]
    return {
        "x": round(pole[0], 1), "y": round(pole[1], 1),
        "from_middle_px": round(float(np.hypot(
            pole[0] - centre[0], pole[1] - centre[1])), 1),
        "fit_px": round(float(np.median(agreeing)), 2),
        "worst_px": round(float(max(agreeing)), 2),
        "frames": used,
        "set_aside": int(len(kept) - used),
    }


def register(photos: str, pattern: str = "*.RAF", only: str = "",
             focal_mm: float = 0.0, sensor_mm: float = DEFAULT_SENSOR_MM,
             demosaic: bool = True, handheld: bool = False,
             hints: str = "") -> str:
    """Where every frame sits relative to the first, in pixels.

    On a tripod the turn is read off the clock and only the shift is
    searched for. Handheld, nothing is bounded: the turn is voted on
    by star pairs -- shape survives what a hand does -- and the shift
    is voted on afterwards, over the whole frame. Coarse hints, where
    a program has any, narrow that unbounded search back down.
    """
    paths = frames_of(photos, pattern, only)
    if len(paths) < 2:
        return json.dumps({
            "format": FORMAT, "photos": str(photos), "pattern": pattern,
            "frames": [{"name": path.name, "dx": 0.0, "dy": 0.0,
                        "agreed": 0} for path in paths],
            "note": "one frame or none: nothing to register"})
    taken = _capture_seconds(paths)
    coarse = _hints_of(hints)
    reference_grey = _grey(_frame(paths[0], demosaic))
    height, width = reference_grey.shape
    centre = ((width - 1) / 2.0, (height - 1) / 2.0)
    anchors = stars_in(reference_grey)
    placed = [{"name": paths[0].name, "dx": 0.0, "dy": 0.0,
               "turn": 0.0, "seconds": 0.0, "agreed": int(len(anchors)),
               "stars": int(len(anchors)),
               "sky": round(float(np.median(reference_grey)), 5),
               "noise": round(_frame_noise(reference_grey), 6)}]
    rate: tuple[float, float] | None = None
    # Every frame's stars, kept. They cost a few kilobytes each and
    # they are what lets the pole pass below correct a placement
    # without opening a single photograph twice.
    seen_stars: dict[str, np.ndarray] = {}
    for index, path in enumerate(paths[1:], start=1):
        _say(index * 45.0 / max(len(paths) - 1, 1),
             f"registering {index} of {len(paths) - 1}")
        grey = _grey(_frame(path, demosaic))
        moving = stars_in(grey)
        seen_stars[path.name] = moving
        sky = float(np.median(grey))
        gap = ((taken[index] - taken[0])
               if taken[index] is not None and taken[0] is not None
               else None)
        if handheld:
            # Nothing is bounded: shape decides the turn, and the
            # shift is looked for across the whole frame -- unless a
            # program has said roughly where the sky went.
            spun, agreeing = vote_rotation(anchors, moving)
            angles = [spun, spun - 180.0]
            radius = float(max(height, width))
            guess = coarse.get(path.name)
            if guess is not None:
                radius = min(radius, max(height, width) * 0.25)
        else:
            bound = (drift_bound(gap, focal_mm, width, sensor_mm)
                     if gap is not None and focal_mm > 0 else 0.0)
            # The sky's own bound where it is knowable; otherwise a
            # generous fraction of the frame, which is still a disc.
            radius = bound if bound > 0 else min(height, width) * 0.15
            # The clock says how far the sky turned; which way depends
            # on where the camera looked, so both ways are tried and
            # the stars settle it.
            spun = sky_rotation(gap) if gap is not None else 0.0
            angles = [0.0] if abs(spun) < 0.02 else [-spun, spun, 0.0]
            guess = None
            if rate is not None and gap is not None:
                guess = (rate[0] * gap, rate[1] * gap)
        degrees, shift, agreed = _best_placement(
            anchors, moving, angles, centre, radius, guess)
        if agreed < LEAST_AGREEING and not handheld:
            # The bound is only as good as the focal length it was
            # told, and a wrong lens is a search that cannot reach the
            # answer. Widening costs one more vote and is the
            # difference between a stack and a smear.
            #
            # The DISTANCE is widened and the ANGLE is not, and the
            # difference matters. A wrong focal length makes the disc
            # the wrong size, so searching further is the right
            # response. It does not make the sky turn faster: the
            # sidereal rate is exact and the frames' own clocks say how
            # much of it passed, so the only thing unknown about the
            # turn is its direction, which is already in the list.
            #
            # This used to add twice the angle as well, on the same
            # reasoning, and the reasoning was simply wrong. Measured
            # on two of the user's frames 86 seconds apart: the first
            # pass fell short of agreement, the fallback offered double
            # the sidereal rate, and ten stars out of a hundred and
            # forty carried it -- 0.7186 degrees where the sky can have
            # turned at most 0.3593. At the corner that is forty-seven
            # pixels of turn applied where twenty-three was the ceiling.
            wider = max(radius * 4.0, min(height, width) * 0.25)
            degrees, shift, agreed = _best_placement(
                anchors, moving, angles, centre, wider, None)
            radius = wider
        # A tripod is only as steady as the last hand that touched it.
        # The angles above are the SKY'S -- a fraction of a degree,
        # read off the clock -- and if somebody nudged the head
        # between frames the true roll is nowhere in that list, so the
        # vote falls back on whatever coincidence scores highest.
        # Measured on a real pair whose tripod had rolled a degree:
        # the coincidence won with eleven stars of a hundred and
        # forty, was accepted, and put a second faint copy of every
        # star in the stack. Eleven of a hundred and forty is not a
        # registration; it is the loudest accident in the room.
        #
        # So when agreement is that poor, the pair geometry is asked
        # directly: star pairs of equal length in both frames imply
        # the roll whatever its size, exactly as the handheld path
        # trusts. The rescue must beat the coincidence soundly to be
        # believed, because on a frame that genuinely has few stars a
        # weak honest answer must not be replaced by a confident
        # wrong one.
        few = max(2 * LEAST_AGREEING,
                  int(0.15 * min(len(anchors), len(moving))))
        if agreed < few and not handheld:
            rolled, _pairs = vote_rotation(
                anchors, moving, most=60, bin_degrees=0.2)
            worth = [angle for angle in
                     (rolled, rolled - 180.0, -rolled, 180.0 - rolled)
                     if abs(angle) <= 20.0
                     and all(abs(angle - been) > 0.05 for been in angles)]
            if worth:
                r_deg, r_shift, r_agreed = _best_placement(
                    anchors, moving, worth, centre,
                    max(radius, min(height, width) * 0.25), None)
                if r_agreed >= max(2 * agreed, 2 * LEAST_AGREEING):
                    degrees, shift, agreed = r_deg, r_shift, r_agreed
        if agreed >= LEAST_AGREEING and gap and not handheld:
            rate = (shift[0] / gap, shift[1] / gap)
        placed.append({
            "name": path.name,
            "dx": round(float(shift[0]), 3),
            "dy": round(float(shift[1]), 3),
            "turn": round(float(degrees), 4),
            "agreed": int(agreed),
            "bound_px": round(float(radius), 1),
            # A turn far beyond what the clock allows is a roll of the
            # CAMERA, not of the sky, and worth saying out loud.
            "rolled": bool(gap is not None and abs(float(degrees))
                           > abs(sky_rotation(gap)) + 0.1),
            # What the sky was like while this frame was open: how many
            # stars stood clear of it, and how bright it was. Both come
            # free -- the pass that registers has already read them --
            # and both are what cloud takes away and adds.
            "stars": int(len(moving)),
            "sky": round(sky, 5),
            # How long after the first frame this one was taken. The
            # clock is what says how far the sky turned, so a frame
            # that could not be placed by its stars can still be
            # placed by its timestamp -- given the pole.
            "seconds": (round(float(gap), 3) if gap is not None else None),
            # And how grainy it was. A thin haze or a passing car's
            # headlights raise a frame's noise without hiding its
            # stars, and averaging such a frame in as an equal is
            # letting the worst frame set the stack's floor.
            "noise": round(_frame_noise(grey), 6),
        })
    # Where the sky turned about, solved from the frames that DID
    # register -- and then used to place the ones that did not.
    #
    # A frame with too few stars above its own noise cannot vote for a
    # shift; that is what a thin cloud, or a longer exposure whose
    # stars trailed, leaves you with. But a rigid sky has exactly one
    # pole, and the clock already says how far it turned by the time
    # that frame was open. Together those leave NOTHING to search for.
    # The weak frames stop being a search that fails and become
    # arithmetic that cannot.
    pole = None if handheld else pole_from(placed, centre)
    rescued = 0
    if pole is not None and pole["fit_px"] <= POLE_FIT_PX:
        spot = (pole["x"], pole["y"])
        # Which way the sky went, according to the frames that know.
        ways = [float(item["turn"]) / float(item["seconds"])
                for item in placed
                if int(item.get("agreed", 0)) >= LEAST_AGREEING
                and item.get("seconds") and abs(float(item["turn"])) > 1e-9]
        way = 1.0 if not ways else (1.0 if np.median(ways) >= 0 else -1.0)
        # What a frame in this sequence looks like when it really does
        # register: the middle of what the confident ones managed.
        confident = [int(item.get("agreed", 0)) for item in placed
                     if int(item.get("agreed", 0)) >= LEAST_AGREEING]
        typical = float(np.median(confident)) if confident else 0.0
        for item in placed:
            if item.get("seconds") in (None, 0.0):
                continue
            agreed = int(item.get("agreed", 0))
            if agreed >= LEAST_AGREEING:
                implied = pole_shift(spot, float(item["turn"]), centre)
                off = float(np.hypot(implied[0] - float(item["dx"]),
                                     implied[1] - float(item["dy"])))
                item["pole_off_px"] = round(off, 2)
                # A frame that registered on a handful of stars where
                # its neighbours registered on a hundred, and that
                # landed nowhere near where a rigid sky puts it, did
                # not register: it found a coincidence. The threshold
                # is deliberately both -- a frame is only overruled
                # when it is BOTH unconvincing and wrong, because a
                # confident frame disagreeing with the model is the
                # model's problem, not the frame's.
                if (off <= POLE_FIT_PX * 3.0
                        or agreed >= max(LEAST_AGREEING * 2.0,
                                         typical * 0.25)):
                    continue
                item["overruled_px"] = round(off, 2)
            turn = way * sky_rotation(float(item["seconds"]))
            shift = pole_shift(spot, turn, centre)
            how = "pole"
            # The pole says where the frame goes; its own stars say it
            # more precisely. Having been told where to look, a vote
            # that had nothing to search now has ten pixels to search,
            # and a handful of stars is enough to win inside a disc
            # that small. Where even that finds nothing, the pole's
            # answer stands on its own -- which is still an answer,
            # and the frame had none before.
            moving = seen_stars.get(str(item["name"]))
            if moving is not None and len(moving):
                spun = turned(moving, turn, centre)
                near, agreeing = vote_shift(
                    anchors, spun, REFINE_PX, centre=shift)
                if agreeing >= LEAST_AGREEING:
                    shift, how = near, "pole and stars"
                    item["agreed"] = int(agreeing)
            item["dx"] = round(float(shift[0]), 3)
            item["dy"] = round(float(shift[1]), 3)
            item["turn"] = round(float(turn), 4)
            item["placed_by"] = how
            rescued += 1
    return json.dumps({
        "format": FORMAT, "photos": str(photos), "pattern": pattern,
        "only": only, "focal_mm": focal_mm, "sensor_mm": sensor_mm,
        "demosaic": bool(demosaic), "handheld": bool(handheld),
        "pole": pole, "placed_by_pole": rescued,
        "frames": placed})


def _hints_of(hints: str) -> dict[str, tuple[float, float]]:
    """Coarse per-frame shifts a program worked out, if any."""
    told = str(hints or "").strip()
    if not told:
        return {}
    try:
        value = json.loads(told if told.startswith("{")
                           else Path(told).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    found: dict[str, tuple[float, float]] = {}
    for name, where in (value.get("shifts") or {}).items():
        try:
            found[str(name)] = (float(where[0]), float(where[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return found


def superimpose(register_text: str, mode: str = "trails",
                output: str = "", darks: str = "",
                sigma: float = 2.5, flats: str = "",
                bias: str = "", scale: float = DRIZZLE_SCALE,
                pixfrac: float = DRIZZLE_PIXFRAC) -> str:
    """Lay the frames on each other, the way the mode asks for.

    trails   -- keep whatever is brightest, frames unmoved. The sky's
                motion draws the picture.
    average  -- register and mean. Noise falls as the square root of
                the count; the stars stay points.
    clipped  -- register and mean, but a pixel far from what the stack
                agrees on is left out. Satellites, aeroplanes and
                cosmic rays are exactly that, and they vanish.
    """
    told = json.loads(register_text)
    root = Path(str(told["photos"])).expanduser().resolve()
    frames = told.get("frames") or []
    if not frames:
        return json.dumps({"format": FORMAT, "error": "no frames to stack"})
    mode = str(mode).casefold()
    if mode not in MODES:
        return json.dumps({
            "format": FORMAT, "error": f"unknown mode {mode!r}; "
            f"one of {', '.join(MODES)}"})
    demosaic = bool(told.get("demosaic", True))
    pattern = told.get("pattern", "*.RAF")
    zero = _median_of(bias, pattern, demosaic)
    dark = _master_dark(darks, pattern, demosaic)
    flat = _master_flat(flats, pattern, demosaic, zero)
    aligned = mode != "trails"
    # A frame the screen set aside is not in the stack at all: it was
    # judged unusable, and averaging it in anyway would make the
    # judgement decorative.
    frames = [item for item in frames if item.get("keep", True)]
    if not frames:
        return json.dumps({
            "format": FORMAT,
            "error": "every frame was set aside; nothing to stack"})
    paths = [root / str(item["name"]) for item in frames]
    # What each frame is worth photometrically, against the first. All
    # ones where the frames were shot alike, which is the usual case
    # and costs nothing.
    scales = light_scales(paths)
    shifts = [(float(item.get("dx", 0.0)), float(item.get("dy", 0.0)))
              if aligned else (0.0, 0.0) for item in frames]
    turns = [float(item.get("turn", 0.0)) if aligned else 0.0
             for item in frames]
    fields: list[dict[str, Any] | None] = [None] * len(paths)

    def calibrated(index: int) -> np.ndarray:
        held = _linear(_frame(paths[index], demosaic))
        # The order calibration is done in is not a taste: the dark is
        # what the sensor adds, so it comes off first; the flat is what
        # the light was multiplied by, so it comes off after.
        if zero is not None and zero.shape == held.shape:
            held = held - zero
        if dark is not None and dark.shape == held.shape:
            held = held - dark
        held = np.clip(held, 0.0, None)
        if flat is not None and flat.shape == held.shape:
            held = np.clip(held / flat, 0.0, None)
        # And on to one scale, so that what is averaged below is the
        # same quantity in every frame.
        scale = scales[index]
        return held if scale == 1.0 else (held * scale).astype(np.float32)

    def read(index: int) -> np.ndarray:
        held = calibrated(index)
        if not aligned:
            return held
        shift, turn = shifts[index], turns[index]
        if fields[index] is not None:
            # The knocked-tripod case: rigid move and residual field
            # composed into one resample.
            return _field_warp(held, turn, shift, fields[index])
        whole = (abs(shift[0] - round(shift[0])) < 1e-6
                 and abs(shift[1] - round(shift[1])) < 1e-6)
        if abs(turn) < 1e-6:
            # No turn: the same fractional step everywhere, which is a
            # convolution and can be done separably. A whole-pixel one
            # is not even that -- it is a move, and moving a frame
            # should not cost it any sharpness at all.
            return _shifted(held, shift) if whole \
                else _lanczos_shift(held, shift)
        return _lanczos_warp(held, turn, shift)

    total = len(paths)
    # After the best rigid placement, each frame is asked whether a
    # residual displacement field remains -- the signature of a knock
    # or a lens bending two pointings differently. Only a field the
    # grid can actually see (worst cell beyond FIELD_BAR) is folded
    # back into the frame's single resample; a well-behaved tripod
    # costs nothing extra.
    field_notes: dict[str, Any] = {}
    if aligned and mode in ("average", "clipped") and total >= 2:
        anchor = _grey(_shown(read(0).astype(np.float32)))
        for index in range(1, total):
            _say(43.0, f"trued {index} of {total - 1}")
            placed = _grey(_shown(read(index).astype(np.float32)))
            found = _measure_field(anchor, placed)
            if found is None or (found["typical"] <= FIELD_BAR
                                 and found["spread"] <= FIELD_CORNER):
                continue
            fields[index] = found
            again = _measure_field(anchor, _grey(_shown(
                read(index).astype(np.float32))))
            if again is not None and (again["typical"] > FIELD_DONE
                                      or again["spread"] > FIELD_CORNER):
                fields[index] = _added_fields(found, again)
            field_notes[str(frames[index]["name"])] = {
                "before_px": round(found["worst"], 2),
                "after_px": round(again["worst"], 2) if again else None}
    if mode == "drizzle":
        scale = float(min(max(scale, 1.0), 4.0))
        pixfrac = float(min(max(pixfrac, 0.05), 1.0))
        first = calibrated(0)
        out_h = int(round(first.shape[0] * scale))
        out_w = int(round(first.shape[1] * scale))
        values = np.zeros((out_h, out_w, first.shape[2]), np.float64)
        weights = np.zeros((out_h, out_w), np.float64)
        for index in range(total):
            _say(45 + index * 50.0 / total,
                 f"drizzled {index + 1} of {total}")
            _drizzle_frame(
                calibrated(index) if index else first, values, weights,
                turns[index], shifts[index], scale, pixfrac)
        lit = weights > 1e-6
        stack = np.zeros_like(values, dtype=np.float32)
        stack[lit] = (values[lit] / weights[lit][..., None]).astype(
            np.float32)
        kept = float(np.mean(weights[lit])) if lit.any() else 0.0
        empty = float(1.0 - lit.mean())
    elif mode == "trails":
        empty = 0.0
        stack = read(0).astype(np.float32)
        for index in range(1, total):
            _say(45 + index * 50.0 / total, f"laid {index + 1} of {total}")
            stack = np.maximum(stack, read(index))
        kept = total
    else:
        empty = 0.0
        # Not every frame is worth the same. A thin haze, a warmer
        # sensor or a longer exposure leaves one frame grainier than
        # its neighbours, and averaging it in as an equal lets the
        # worst frame set the floor for all of them. Weighting each by
        # one over its own variance is the least-noise way to combine
        # measurements of the same thing, and the noise was measured
        # while registering, so it costs nothing to ask.
        weights = _frame_weights(frames, scales)
        share = max(float(weights.sum()), 1e-9)
        stack = read(0).astype(np.float32)
        summed = stack.astype(np.float64) * weights[0]
        # The plain sum and the plain sum of squares are kept as well,
        # because the spread below is a question about how far the
        # frames DISAGREE, and that is not a question about what any
        # of them is worth.
        plain_sum = stack.astype(np.float64)
        squared = plain_sum ** 2
        for index in range(1, total):
            _say(45 + index * 25.0 / total,
                 f"gathered {index + 1} of {total}")
            held = read(index).astype(np.float64)
            summed += held * weights[index]
            plain_sum += held
            squared += held ** 2
        mean = summed / share
        kept = total
        if mode == "clipped" and total >= 3:
            # A second pass, now that the stack knows what it agrees
            # on: anything standing far outside it is left out.
            spread = np.sqrt(np.maximum(
                squared / total - (plain_sum / total) ** 2, 0.0))
            band = np.maximum(spread * float(sigma), 1e-4)
            kept_sum = np.zeros_like(mean)
            kept_weight = np.zeros(mean.shape, np.float64)
            kept_count = np.zeros(mean.shape, np.float32)
            for index in range(total):
                _say(70 + index * 25.0 / total,
                     f"weighed {index + 1} of {total}")
                held = read(index).astype(np.float64)
                near = np.abs(held - mean) <= band
                kept_sum += np.where(near, held * weights[index], 0.0)
                kept_weight += np.where(near, weights[index], 0.0)
                kept_count += near
            mean = np.where(kept_weight > 1e-9,
                            kept_sum / np.maximum(kept_weight, 1e-9), mean)
            kept = float(np.mean(kept_count))
        stack = mean.astype(np.float32)
    _say(96, "writing the stack")
    home = (Path(output).expanduser() if output
            else root / ".darkimiya" / "Superimpose")
    home.mkdir(parents=True, exist_ok=True)
    # Back into lightness for anything that has to be looked at or
    # opened elsewhere: the stack is written display-referred, as it
    # always was, and only the arithmetic above changed.
    shown = _shown(stack)
    tiff = home / f"{mode}.tiff"
    _write_tiff(tiff, shown, deep=bool(paths) and _stored_float(paths[0]))
    proof = home / f"{mode}.jpg"
    Image.fromarray((shown * 255.0 + 0.5).astype(np.uint8), "RGB").save(
        proof, quality=95)
    agreed = [int(item.get("agreed", 0)) for item in frames[1:]]
    return json.dumps({
        "format": FORMAT, "mode": mode, "photos": str(root),
        "frames_used": total,
        "registered": sum(1 for count in agreed
                          if count >= LEAST_AGREEING) + 1,
        "kept_per_pixel": round(float(kept), 2),
        "drizzle": ({"scale": scale, "pixfrac": pixfrac,
                     "unfilled": round(empty, 5)}
                    if mode == "drizzle" else None),
        "set_aside": [str(item["name"]) for item in told.get("frames", [])
                      if not item.get("keep", True)],
        "dark_subtracted": dark is not None,
        "flat_divided": flat is not None,
        "bias_subtracted": zero is not None,
        "field_corrected": field_notes or None,
        "stack": str(tiff), "proof": str(proof),
    }, indent=2)


def _stored_float(path: Path) -> bool:
    """Whether this frame arrived as floats rather than whole numbers."""
    if path.suffix.casefold() not in {".tif", ".tiff"}:
        return False
    try:
        import tifffile

        with tifffile.TiffFile(path) as opened:
            return bool(np.issubdtype(opened.pages[0].dtype, np.floating))
    except Exception:                    # noqa: BLE001 - then it is not
        return False


def _write_tiff(path: Path, shown: np.ndarray, deep: bool = False) -> None:
    """The stack, written at the depth its frames were given in.

    Sixteen bits is the right answer for a stack of raw frames: the
    camera never had more than fourteen and the file is meant to be
    opened by anything. But a stack assembled from floats is a
    different object. Somebody who developed every frame first and
    exported floats did that on purpose, and handing back whole
    numbers throws away precision they went out of their way to keep
    -- and precision is exactly what a stack was made to buy.
    """
    import tifffile

    if deep:
        tifffile.imwrite(path, np.asarray(shown, np.float32))
        return
    tifffile.imwrite(path, (shown * 65535.0 + 0.5).astype(np.uint16))


# --- what a model can say about a sky ------------------------------------
#
# Handheld, nothing is bounded: between two frames the sky can have
# gone anywhere and turned any amount, and the clock says nothing
# because the hand moved further than the sky did. The star-pair vote
# still works -- shape survives what a hand does -- but it searches the
# whole frame, and on a poor sky it can settle on a wrong pile.
#
# What a model is genuinely good at, and arithmetic is not, is
# RECOGNITION: it knows what Orion's Belt looks like. It is not asked
# to register anything, only to say roughly where a group it knows sits
# in each frame. That coarse answer bounds the search back down, and
# the deterministic vote does the actual registering to a tenth of a
# pixel -- the same division of labour the named-subject timelapse
# makes, and for the same reason.

def sky_keyframes(photos: str, pattern: str = "*.RAF",
                  every: float = 20, only: str = "") -> list:
    from timelapse_kernel import keyframes

    return keyframes(photos, pattern, every, only)


def sky_proof(photos: str, name: str, directory: str,
              edge: float = 1400) -> str:
    from timelapse_kernel import keyframe_proof

    return keyframe_proof(photos, name, directory, edge)


def group_prompt(proof: str) -> str:
    with Image.open(str(proof)) as opened:
        width, height = opened.size
    return (
        f"This is a {width}x{height} photograph of the night sky. Name "
        "ONE star pattern you actually recognise in it -- a "
        "constellation, an asterism, a distinctive bright pair or "
        "triangle -- and box it. Answer x0, y0, x1, y1 in pixels of "
        "THIS image, origin top-left, and put the pattern's usual name "
        "in 'group'. Prefer the same pattern you would pick in any "
        "other frame of the same sky, because the answer is used to "
        "line the frames up with each other. If you recognise nothing "
        "with confidence, answer visible=false and zeros -- a guess is "
        "worse than nothing here.")


def collect_group(hints_json: str, photos: str, name: str, proof: str,
                  found: Any) -> str:
    """One frame's recognised group, gathered into coarse shifts.

    The model answers in the proof's pixels; the frames are larger, so
    the box's middle is scaled back to the frame's own. Shifts are
    taken against the first frame that showed the SAME group -- two
    frames agreeing about Orion say something, and a frame that saw
    Orion compared against one that saw Cassiopeia says nothing.
    """
    held = json.loads(hints_json or '{"seen": {}, "shifts": {}}')
    told = found if isinstance(found, dict) else {
        key: getattr(found, key, None)
        for key in ("group", "x0", "y0", "x1", "y1", "visible")}
    if not told.get("visible"):
        return json.dumps(held)
    try:
        box = [float(told["x0"]), float(told["y0"]),
               float(told["x1"]), float(told["y1"])]
    except (KeyError, TypeError, ValueError):
        return json.dumps(held)
    if box[2] <= box[0] or box[3] <= box[1]:
        return json.dumps(held)
    with Image.open(str(proof)) as opened:
        proof_w, proof_h = opened.size
    frame = Path(str(photos)).expanduser().resolve() / str(name)
    try:
        full = _frame(frame, demosaic=False)
        full_h, full_w = full.shape[:2]
    except Exception:                        # noqa: BLE001 - proof's own
        full_w, full_h = proof_w, proof_h
    scale_x = full_w / max(proof_w, 1)
    scale_y = full_h / max(proof_h, 1)
    middle = [((box[0] + box[2]) / 2.0) * scale_x,
              ((box[1] + box[3]) / 2.0) * scale_y]
    # A model writes "Orion's Belt" once and "Orions Belt" the next
    # time; the same pattern under two spellings must still be one
    # anchor, so only letters and digits are kept.
    import re

    group = re.sub(r"[^a-z0-9]+", "",
                   str(told.get("group") or "a group").casefold())
    seen = held.setdefault("seen", {})
    if group not in seen:
        seen[group] = {"name": str(name), "at": middle}
        held.setdefault("shifts", {})
        return json.dumps(held)
    first = seen[group]
    # The correction that would carry this frame back onto that one.
    held.setdefault("shifts", {})[str(name)] = [
        round(first["at"][0] - middle[0], 1),
        round(first["at"][1] - middle[1], 1)]
    return json.dumps(held)


def hints_usable(hints_json: str) -> bool:
    """Whether anything was recognised well enough to bound a search."""
    try:
        held = json.loads(hints_json or "{}")
    except ValueError:
        return False
    return bool(held.get("seen"))


def hints_note(hints_json: str) -> str:
    held = json.loads(hints_json or "{}")
    seen = held.get("seen") or {}
    shifts = held.get("shifts") or {}
    if not seen:
        return ("No star pattern was recognised in any keyframe, so "
                "nothing bounds the search; the star-pair vote will "
                "work over the whole frame on its own.")
    return (f"Recognised {', '.join(sorted(seen))} across the "
            f"keyframes; {len(shifts)} frame(s) carry a coarse "
            "position from it, which narrows the registration. The "
            "stars themselves still settle it to a fraction of a "
            "pixel.")


# --- which frames the sky ruined -----------------------------------------

# A frame with this share of the sequence's usual star count is not a
# frame of the same sky; below the doubtful line it is cloud.
CLEARLY_CLOUDED = 0.45
CLEARLY_CLEAR = 0.75


def screen_frames(register_text: str, clouded: float = CLEARLY_CLOUDED,
                  clear: float = CLEARLY_CLEAR) -> str:
    """Set aside the frames the sky ruined, by what the stars say.

    Cloud does two things at once and both are already measured: it
    hides stars, and it brightens the background by scattering
    whatever light is around. So the count of stars standing clear of
    the sky, against what the sequence usually manages, is a free and
    honest verdict -- no model, no extra pass.

    Three bands rather than two. Well below the usual count is cloud
    and is set aside; near the usual count is clear and is kept; the
    band between is DOUBTFUL, kept but marked, because that is exactly
    where a threshold is a coin toss and where asking something that
    can actually look is worth the money.
    """
    told = json.loads(register_text)
    frames = told.get("frames") or []
    counts = [int(item.get("stars", 0)) for item in frames]
    if not counts:
        return register_text
    usual = float(np.median(counts)) or 1.0
    for item in frames:
        share = int(item.get("stars", 0)) / usual
        if share < clouded:
            item["keep"] = False
            item["verdict"] = "clouded"
        elif share < clear:
            item["keep"] = True
            item["verdict"] = "doubtful"
        else:
            item["keep"] = True
            item["verdict"] = "clear"
        item["share"] = round(share, 3)
    told["frames"] = frames
    told["screened"] = True
    return json.dumps(told)


def doubtful(screened_text: str) -> list:
    """The frames a threshold cannot honestly call, by name."""
    told = json.loads(screened_text)
    return [str(item["name"]) for item in (told.get("frames") or [])
            if item.get("verdict") == "doubtful"]


def screen_note(screened_text: str) -> str:
    told = json.loads(screened_text)
    frames = told.get("frames") or []
    aside = [item for item in frames if not item.get("keep", True)]
    unsure = [item for item in frames
              if item.get("verdict") == "doubtful"]
    if not aside and not unsure:
        return (f"All {len(frames)} frames show the sky the sequence "
                "usually shows; none set aside.")
    parts = []
    if aside:
        parts.append(
            f"{len(aside)} frame(s) set aside as clouded "
            f"({', '.join(str(item['name']) for item in aside[:4])}"
            + (", …" if len(aside) > 4 else "") + ")")
    if unsure:
        parts.append(f"{len(unsure)} doubtful, kept but marked")
    return "; ".join(parts) + "."


def cloud_prompt(proof: str) -> str:
    return (
        "This is one frame of a night-sky sequence being stacked. Is "
        "its sky USABLE? Say usable=false when cloud, haze or a "
        "brightening sky has taken the stars away or veiled them -- "
        "such a frame poisons an average and is better left out. Say "
        "usable=true for a clear sky, even a faint or noisy one, and "
        "for thin high cloud that leaves the stars plainly visible. A "
        "few passing clouds at one edge of an otherwise clear frame "
        "are usable. In 'why', say in a few words what you saw, so a "
        "wrong call is readable afterwards.")


def judge_frame(screened_text: str, name: str, found: Any) -> str:
    """One frame's second opinion, written where the stack will read it."""
    told = json.loads(screened_text)
    said = found if isinstance(found, dict) else {
        key: getattr(found, key, None) for key in ("usable", "why")}
    for item in told.get("frames") or []:
        if str(item.get("name")) != str(name):
            continue
        item["keep"] = bool(said.get("usable"))
        item["verdict"] = "kept by eye" if item["keep"] else "clouded by eye"
        item["why"] = str(said.get("why") or "")[:120]
    return json.dumps(told)


def _drizzle_frame(frame: np.ndarray, values: np.ndarray,
                   weights: np.ndarray, degrees: float,
                   shift: tuple[float, float], scale: float,
                   pixfrac: float) -> None:
    """Drop one frame's shrunken pixels on to the finer grid.

    Fruchter and Hook's reconstruction, and the reason it recovers
    what interpolation cannot: an input pixel is not asked what its
    neighbours are doing. It is shrunk to a smaller square -- a drop --
    carried through the frame's own transform, and its light is shared
    among the output pixels it actually lands on, in proportion to how
    much of it lands there. Nothing is interpolated, so nothing is
    blurred; what makes the detail appear is that different frames
    land their drops in different places, which is what dithering IS.

    The drop is treated as square in the output's own axes. That is
    exact when the frame is only shifted and a good approximation
    while the turn is small, which is what a night's registration
    gives; a frame rolled far enough for the corner of a drop to
    matter is past what this is honest about.
    """
    height, width = frame.shape[:2]
    out_h, out_w = weights.shape
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    angle = np.radians(degrees)
    cos, sin = np.cos(angle), np.sin(angle)
    # Where this pixel's centre lands on the reference frame, then on
    # the finer grid the stack is being built on.
    rx = cx + (xx - cx) * cos - (yy - cy) * sin + shift[0]
    ry = cy + (xx - cx) * sin + (yy - cy) * cos + shift[1]
    ox = (rx + 0.5) * scale
    oy = (ry + 0.5) * scale
    half = max(pixfrac, 0.01) * scale / 2.0
    left = np.floor(ox - half).astype(np.int32)
    top = np.floor(oy - half).astype(np.int32)
    span = int(np.ceil(2.0 * half)) + 1
    flat_values = values.reshape(-1, values.shape[2])
    flat_weights = weights.reshape(-1)
    for step_x in range(span):
        px = left + step_x
        wide = np.clip(np.minimum(ox + half, px + 1.0)
                       - np.maximum(ox - half, px), 0.0, None)
        for step_y in range(span):
            py = top + step_y
            tall = np.clip(np.minimum(oy + half, py + 1.0)
                           - np.maximum(oy - half, py), 0.0, None)
            share = wide * tall
            lands = ((px >= 0) & (px < out_w) & (py >= 0)
                     & (py < out_h) & (share > 0))
            if not lands.any():
                continue
            where = (py[lands] * out_w + px[lands]).astype(np.int64)
            part = share[lands].astype(np.float64)
            flat_weights += np.bincount(
                where, weights=part, minlength=out_h * out_w)
            for channel in range(values.shape[2]):
                flat_values[:, channel] += np.bincount(
                    where,
                    weights=frame[..., channel][lands].astype(np.float64)
                    * part,
                    minlength=out_h * out_w)


def stack_valid(told: Any) -> bool:
    """A stack worth keeping: it was written, and frames went into it."""
    try:
        value = json.loads(told) if isinstance(told, str) else told
    except ValueError:
        return False
    if not isinstance(value, dict) or value.get("error"):
        return False
    if int(value.get("frames_used", 0)) < 2:
        return False
    return bool(value.get("stack")) and Path(str(value["stack"])).is_file()


def stack_note(told: Any) -> str:
    try:
        value = json.loads(told) if isinstance(told, str) else told
    except ValueError:
        return "the stack returned something unreadable"
    if value.get("error"):
        return str(value["error"])
    mode = str(value.get("mode"))
    if mode == "trails":
        return (f"{value.get('frames_used')} frames laid brightest-wins "
                f"into {Path(str(value.get('stack'))).name}. The sky's "
                "own motion drew it.")
    return (f"{value.get('frames_used')} frames registered "
            f"({value.get('registered')} agreed on the stars) and "
            f"averaged into {Path(str(value.get('stack'))).name}"
            + (", darks subtracted" if value.get("dark_subtracted") else "")
            + f"; {value.get('kept_per_pixel')} frames survived the "
              "rejection at the average pixel.")


def stack_home(told: str) -> str:
    """Where the report lands, beside the stack it describes."""
    value = json.loads(told)
    return str(Path(str(value["stack"])).with_suffix(".json"))


def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        mode = argv[0]
        photos = argv[1]
        pattern = argv[2] if len(argv) > 2 else "*.RAF"
        focal = float(argv[3]) if len(argv) > 3 else 0.0
        placed = register(photos, pattern, focal_mm=focal)
        told = superimpose(placed, mode)
        print(stack_note(told))
        return 0 if stack_valid(told) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
