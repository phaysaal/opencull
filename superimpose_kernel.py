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

MODES = ("trails", "average", "clipped")


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
    from timelapse_kernel import _preview

    return np.asarray(_preview(path).convert("RGB"),
                      dtype=np.float32) / 255.0


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
    return np.median(
        np.stack([_frame(path, demosaic) for path in frames]),
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
               "turn": 0.0, "agreed": int(len(anchors)),
               "stars": int(len(anchors)),
               "sky": round(float(np.median(reference_grey)), 5)}]
    rate: tuple[float, float] | None = None
    for index, path in enumerate(paths[1:], start=1):
        _say(index * 45.0 / max(len(paths) - 1, 1),
             f"registering {index} of {len(paths) - 1}")
        grey = _grey(_frame(path, demosaic))
        moving = stars_in(grey)
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
            wider = max(radius * 4.0, min(height, width) * 0.25)
            spread = list(angles)
            if abs(spun) >= 0.02:
                spread += [-spun * 2, spun * 2]
            degrees, shift, agreed = _best_placement(
                anchors, moving, spread, centre, wider, None)
            radius = wider
        if agreed >= LEAST_AGREEING and gap and not handheld:
            rate = (shift[0] / gap, shift[1] / gap)
        placed.append({
            "name": path.name,
            "dx": round(float(shift[0]), 3),
            "dy": round(float(shift[1]), 3),
            "turn": round(float(degrees), 4),
            "agreed": int(agreed),
            "bound_px": round(float(radius), 1),
            # What the sky was like while this frame was open: how many
            # stars stood clear of it, and how bright it was. Both come
            # free -- the pass that registers has already read them --
            # and both are what cloud takes away and adds.
            "stars": int(len(moving)),
            "sky": round(sky, 5),
        })
    return json.dumps({
        "format": FORMAT, "photos": str(photos), "pattern": pattern,
        "only": only, "focal_mm": focal_mm, "sensor_mm": sensor_mm,
        "demosaic": bool(demosaic), "handheld": bool(handheld),
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
                bias: str = "") -> str:
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
    shifts = [(float(item.get("dx", 0.0)), float(item.get("dy", 0.0)))
              if aligned else (0.0, 0.0) for item in frames]
    turns = [float(item.get("turn", 0.0)) if aligned else 0.0
             for item in frames]

    def read(index: int) -> np.ndarray:
        held = _frame(paths[index], demosaic)
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
        if not aligned:
            return held
        shift, turn = shifts[index], turns[index]
        whole = (abs(turn) < 1e-6
                 and abs(shift[0] - round(shift[0])) < 1e-6
                 and abs(shift[1] - round(shift[1])) < 1e-6)
        return (_shifted(held, shift) if whole
                else _warped(held, turn, shift))

    total = len(paths)
    stack = read(0).astype(np.float32)
    if mode == "trails":
        for index in range(1, total):
            _say(45 + index * 50.0 / total, f"laid {index + 1} of {total}")
            stack = np.maximum(stack, read(index))
        kept = total
    else:
        summed = stack.astype(np.float64)
        squared = summed ** 2
        for index in range(1, total):
            _say(45 + index * 25.0 / total,
                 f"gathered {index + 1} of {total}")
            held = read(index).astype(np.float64)
            summed += held
            squared += held ** 2
        mean = summed / total
        kept = total
        if mode == "clipped" and total >= 3:
            # A second pass, now that the stack knows what it agrees
            # on: anything standing far outside it is left out.
            spread = np.sqrt(np.maximum(squared / total - mean ** 2, 0.0))
            band = np.maximum(spread * float(sigma), 1e-4)
            kept_sum = np.zeros_like(mean)
            kept_count = np.zeros(mean.shape, np.float32)
            for index in range(total):
                _say(70 + index * 25.0 / total,
                     f"weighed {index + 1} of {total}")
                held = read(index).astype(np.float64)
                near = np.abs(held - mean) <= band
                kept_sum += np.where(near, held, 0.0)
                kept_count += near
            mean = np.where(kept_count > 0,
                            kept_sum / np.maximum(kept_count, 1), mean)
            kept = float(np.mean(kept_count))
        stack = mean.astype(np.float32)
    _say(96, "writing the stack")
    home = (Path(output).expanduser() if output
            else root / ".darkimiya" / "Superimpose")
    home.mkdir(parents=True, exist_ok=True)
    shown = np.clip(stack, 0.0, 1.0)
    tiff = home / f"{mode}.tiff"
    _write_tiff(tiff, shown)
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
        "set_aside": [str(item["name"]) for item in told.get("frames", [])
                      if not item.get("keep", True)],
        "dark_subtracted": dark is not None,
        "flat_divided": flat is not None,
        "bias_subtracted": zero is not None,
        "stack": str(tiff), "proof": str(proof),
    }, indent=2)


def _write_tiff(path: Path, shown: np.ndarray) -> None:
    import tifffile

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
