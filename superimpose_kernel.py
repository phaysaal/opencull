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


def _master_dark(folder: str, pattern: str,
                 demosaic: bool) -> np.ndarray | None:
    """The camera's own fixed pattern, from frames of the lens cap on.

    The median rather than the mean: a dark frame's job is to describe
    what the sensor does with no light, and one cosmic ray should not
    become part of that description.
    """
    if not str(folder).strip():
        return None
    darks = frames_of(folder, pattern)
    if len(darks) < 2:
        return None
    held = np.stack([_frame(path, demosaic) for path in darks])
    return np.median(held, axis=0).astype(np.float32)


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


def register(photos: str, pattern: str = "*.RAF", only: str = "",
             focal_mm: float = 0.0, sensor_mm: float = DEFAULT_SENSOR_MM,
             demosaic: bool = True) -> str:
    """Where every frame sits relative to the first, in pixels."""
    paths = frames_of(photos, pattern, only)
    if len(paths) < 2:
        return json.dumps({
            "format": FORMAT, "photos": str(photos), "pattern": pattern,
            "frames": [{"name": path.name, "dx": 0.0, "dy": 0.0,
                        "agreed": 0} for path in paths],
            "note": "one frame or none: nothing to register"})
    taken = _capture_seconds(paths)
    reference_grey = _grey(_frame(paths[0], demosaic))
    height, width = reference_grey.shape
    anchors = stars_in(reference_grey)
    placed = [{"name": paths[0].name, "dx": 0.0, "dy": 0.0,
               "agreed": int(len(anchors))}]
    rate: tuple[float, float] | None = None
    for index, path in enumerate(paths[1:], start=1):
        _say(index * 45.0 / max(len(paths) - 1, 1),
             f"registering {index} of {len(paths) - 1}")
        moving = stars_in(_grey(_frame(path, demosaic)))
        gap = ((taken[index] - taken[0])
               if taken[index] is not None and taken[0] is not None
               else None)
        bound = (drift_bound(gap, focal_mm, width, sensor_mm)
                 if gap is not None and focal_mm > 0 else 0.0)
        # The sky's own bound where it is knowable; otherwise a
        # generous fraction of the frame, which is still a disc.
        radius = bound if bound > 0 else min(height, width) * 0.15
        shift, agreed = (0.0, 0.0), 0
        if rate is not None and gap is not None:
            # Constant rate: the shift is predicted, and only a small
            # window around the prediction is examined.
            guess = (rate[0] * gap, rate[1] * gap)
            shift, agreed = vote_shift(
                anchors, moving, REFINE_PX, centre=guess)
        if agreed < LEAST_AGREEING:
            shift, agreed = vote_shift(anchors, moving, radius)
        if agreed >= LEAST_AGREEING and gap:
            rate = (shift[0] / gap, shift[1] / gap)
        placed.append({
            "name": path.name,
            "dx": round(float(shift[0]), 3),
            "dy": round(float(shift[1]), 3),
            "agreed": int(agreed),
            "bound_px": round(float(radius), 1),
        })
    return json.dumps({
        "format": FORMAT, "photos": str(photos), "pattern": pattern,
        "only": only, "focal_mm": focal_mm, "sensor_mm": sensor_mm,
        "demosaic": bool(demosaic), "frames": placed})


def superimpose(register_text: str, mode: str = "trails",
                output: str = "", darks: str = "",
                sigma: float = 2.5) -> str:
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
    dark = _master_dark(darks, told.get("pattern", "*.RAF"), demosaic)
    aligned = mode != "trails"
    paths = [root / str(item["name"]) for item in frames]
    shifts = [(float(item.get("dx", 0.0)), float(item.get("dy", 0.0)))
              if aligned else (0.0, 0.0) for item in frames]

    def read(index: int) -> np.ndarray:
        held = _frame(paths[index], demosaic)
        if dark is not None and dark.shape == held.shape:
            held = np.clip(held - dark, 0.0, None)
        return _shifted(held, shifts[index]) if aligned else held

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
        "dark_subtracted": dark is not None,
        "stack": str(tiff), "proof": str(proof),
    }, indent=2)


def _write_tiff(path: Path, shown: np.ndarray) -> None:
    import tifffile

    tifffile.imwrite(path, (shown * 65535.0 + 0.5).astype(np.uint16))


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
