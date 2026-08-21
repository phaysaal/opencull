"""What a photograph of stars is actually like, measured.

Every question worth asking about a starfield has been asked in this
project already -- how many stars stand clear, how grainy the sky is,
whether the stars came out round, whether the sky is even -- and each
time it was asked in a throwaway script with its own thresholds. That
is how two measurements of the same picture end up disagreeing, and it
happened more than once.

So the questions live here, once, with their answers defined:

  DEPTH        how many stars stand clear of the sky, and how faint the
               faintest of them is
  SKY          its level, its grain, its evenness, its colour
  SHAPE        how wide the stars are, how round, and which way they
               lean when they are not
  RANGE        what is clipped at either end

Two counts are reported and they are not interchangeable. One counts
stars standing clear of THIS picture's own grain, which is the fair way
to compare pictures stretched differently -- a harder stretch lifts the
grain along with the stars, so the ratio survives it. The other counts
stars above a FIXED brightness over the local sky, which is the fair
way to ask what a viewer will actually see. They can disagree sharply:
a grainy picture wins the fixed count by counting its own noise, and
loses the significance count for the same reason. Both are printed
because either alone can mislead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

# What counts as a detection, in deviations of the sky's own grain.
# Five is the astronomer's usual floor; the brighter bars say how the
# population thins out, which is a better description of depth than any
# single number.
SIGNIFICANCE = (5.0, 10.0, 20.0)
# And the fixed bars, as a fraction above the local sky. These are what
# an eye sees rather than what a detector counts.
PLAIN_STEPS = (0.02, 0.05, 0.10)
# How far out the local sky is measured, in pixels of the picture.
SKY_REACH = 40
# The window a star's moments are taken over. Wide enough for the
# skirts of a bright one, narrow enough that two stars rarely share it.
STAR_REACH = 8
# How many of the brightest stars the shape figures are taken from.
SHAPE_SAMPLE = 300


def _luma(rgb: np.ndarray) -> np.ndarray:
    if rgb.ndim == 2:
        return np.asarray(rgb, np.float32)
    return (rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152
            + rgb[..., 2] * 0.0722).astype(np.float32)


def _peaks(field: np.ndarray, level: float) -> np.ndarray:
    """Local maxima standing above a level: one point per star."""
    lit = field > level
    top = np.ones(field.shape, bool)
    for down in (-1, 0, 1):
        for across in (-1, 0, 1):
            if down or across:
                top &= field >= np.roll(np.roll(field, down, 0), across, 1)
    return np.argwhere(lit & top)


def sky_grain(over: np.ndarray) -> float:
    """How big a nothing is, measured where there is nothing.

    From the quietest four fifths of the picture, so a field thick with
    stars does not report its stars as noise -- which a plain deviation
    would, and which is how a good photograph gets called grainy.
    """
    size = np.abs(over)
    quiet = size[size < np.percentile(size, 80)]
    if not quiet.size:
        return float("nan")
    return float(np.median(quiet)) * 1.4826


def _shape_of(over: np.ndarray, found: np.ndarray) -> dict[str, float]:
    """How wide the stars are, how round, and which way they lean."""
    reach = STAR_REACH
    grid_y, grid_x = np.mgrid[-reach:reach + 1, -reach:reach + 1]
    widths: list[float] = []
    sxx = syy = sxy = 0.0
    counted = 0
    for down, across in found:
        patch = np.clip(
            over[down - reach:down + reach + 1,
                 across - reach:across + reach + 1], 0.0, None)
        total = float(patch.sum())
        if total <= 0.0:
            continue
        share = patch / total
        mid_x = float((grid_x * share).sum())
        mid_y = float((grid_y * share).sum())
        one = float((((grid_x - mid_x) ** 2) * share).sum())
        two = float((((grid_y - mid_y) ** 2) * share).sum())
        cross = float((((grid_x - mid_x) * (grid_y - mid_y)) * share).sum())
        widths.append(float(np.sqrt(max((one + two) / 2.0, 0.0))))
        sxx += one
        syy += two
        sxy += cross
        counted += 1
    if not counted:
        return {"width_px": float("nan"), "roundness": float("nan"),
                "leans_degrees": float("nan"), "stars_measured": 0}
    sxx /= counted
    syy /= counted
    sxy /= counted
    across_it = sxx + syy
    area = sxx * syy - sxy * sxy
    root = float(np.sqrt(max(across_it * across_it / 4.0 - area, 0.0)))
    major = across_it / 2.0 + root
    minor = max(across_it / 2.0 - root, 1e-9)
    return {
        "width_px": round(float(np.median(widths)), 3),
        # One means round. Higher means oval, and a stack that is more
        # oval than its own frames is a stack that did not line up.
        "roundness": round(float(np.sqrt(major / minor)), 3),
        "leans_degrees": round(
            float(0.5 * np.degrees(np.arctan2(2 * sxy, sxx - syy))), 1),
        "stars_measured": int(counted),
    }


def assess(source: Any, name: str = "") -> dict[str, Any]:
    """Measure a starfield. Takes a path or an array in 0..1."""
    from development_engine import _box_mean

    if isinstance(source, (str, Path)):
        from superimpose_kernel import _frame

        path = Path(source)
        name = name or path.name
        held = _frame(path, demosaic=True)
    else:
        held = np.asarray(source, np.float32)
    held = np.clip(np.asarray(held, np.float32), 0.0, 1.0)
    if held.ndim == 2:
        held = np.stack([held] * 3, axis=2)
    height, width = held.shape[:2]
    megapixels = height * width / 1e6

    grey = _luma(held)
    over = grey - _box_mean(grey, SKY_REACH)
    grain = sky_grain(over)

    # --- how many stars, two ways -----------------------------------
    depth: dict[str, Any] = {}
    for bar in SIGNIFICANCE:
        found = _peaks(over, grain * bar)
        depth[f"over_{bar:g}_sigma"] = int(len(found))
        depth[f"over_{bar:g}_sigma_per_mp"] = round(len(found) / megapixels, 1)
    plain: dict[str, Any] = {}
    for step in PLAIN_STEPS:
        plain[f"over_{step:g}"] = int(len(_peaks(over, step)))

    # --- the shape of the ones worth measuring ----------------------
    found = _peaks(over, grain * 10.0)
    inside = ((found[:, 0] > STAR_REACH)
              & (found[:, 0] < height - STAR_REACH)
              & (found[:, 1] > STAR_REACH)
              & (found[:, 1] < width - STAR_REACH)) if len(found) else None
    if inside is not None and inside.any():
        found = found[inside]
        brightest = np.argsort(over[found[:, 0], found[:, 1]])[::-1]
        found = found[brightest[:SHAPE_SAMPLE]]
    shape = _shape_of(over, found) if len(found) else _shape_of(
        over, np.zeros((0, 2), int))

    # --- the sky itself ---------------------------------------------
    channels = [float(np.median(held[..., band])) for band in range(3)]
    tiles = []
    for row in range(3):
        for column in range(3):
            piece = grey[row * height // 3:(row + 1) * height // 3,
                         column * width // 3:(column + 1) * width // 3]
            tiles.append(float(np.percentile(piece, 20)))
    level = float(np.median(grey))
    sky = {
        "level": round(level, 5),
        "grain": round(grain, 6),
        # How far the background wanders across the frame, against how
        # big its own grain is. Under about two it cannot be seen.
        "unevenness": round(max(tiles) - min(tiles), 5),
        "unevenness_in_grain": round((max(tiles) - min(tiles))
                                     / max(grain, 1e-9), 1),
        "red_green_blue": [round(value, 5) for value in channels],
        "colour_cast": round(max(channels) - min(channels), 5),
    }

    # --- what is clipped at either end ------------------------------
    span = {
        "black_clipped_percent": round(
            100.0 * float((grey <= 0.0).mean()), 4),
        "white_clipped_percent": round(
            100.0 * float((grey >= 1.0).mean()), 4),
        "brightest": round(float(np.percentile(grey, 99.999)), 5),
    }
    if len(found):
        peaks = over[found[:, 0], found[:, 1]]
        span["star_peak_median"] = round(float(np.median(peaks)), 5)
        span["star_peak_over_grain"] = round(
            float(np.median(peaks)) / max(grain, 1e-9), 1)

    return {
        "name": name,
        "pixels": f"{width}x{height}",
        "megapixels": round(megapixels, 1),
        "depth": depth,
        "plain_count": plain,
        "shape": shape,
        "sky": sky,
        "range": span,
    }


# Past this much of the picture sitting at pure black, the sky has not
# been darkened but deleted, and the faint stars in it with it.
BLACK_ALARM = 1.0
# A star this wide has usually been smeared rather than drawn.
WIDE_ALARM = 4.0
# And this oval.
OVAL_ALARM = 1.20


def concerns(seen: dict[str, Any]) -> list[str]:
    """What is wrong with this picture, in the order it matters.

    Measurements alone let a picture look good by one number while
    being ruined by another -- most of all the star counts, which rise
    handsomely as a sky is crushed toward black, right up until the
    sky and everything faint in it is gone.
    """
    sky, shape, span = seen["sky"], seen["shape"], seen["range"]
    said = []
    black = span["black_clipped_percent"]
    if black >= 50.0:
        said.append(
            f"{black:.0f}% of this picture is pure black. The sky has not "
            "been darkened, it has been deleted, and every star that was "
            "faint enough to sit near it has gone with it. The star "
            "counts below are measured against what little grain "
            "survives and read far better than the picture is.")
    elif black >= BLACK_ALARM:
        said.append(
            f"{black:.1f}% of the picture is clipped to pure black. "
            "Whatever was faint there cannot be brought back.")
    if span["white_clipped_percent"] >= 0.5:
        said.append(
            f"{span['white_clipped_percent']:.1f}% is clipped to white; "
            "the brightest stars have lost their shape and their colour.")
    if shape["roundness"] >= OVAL_ALARM:
        said.append(
            f"the stars are {shape['roundness']:.2f} times longer one way "
            f"than the other, leaning {shape['leans_degrees']:+.0f} degrees "
            "-- either the frames did not line up, or the sky moved while "
            "the shutter was open.")
    if shape["width_px"] >= WIDE_ALARM:
        said.append(
            f"the stars are {shape['width_px']:.1f} px wide, which is "
            "broad enough to be smearing rather than focus.")
    if sky["unevenness_in_grain"] >= 3.0:
        said.append(
            f"the sky wanders {sky['unevenness_in_grain']:.1f} times its own "
            "grain across the frame -- vignetting, or a town on the horizon.")
    if sky["colour_cast"] >= 0.004:
        said.append(
            f"the sky is not grey: {sky['colour_cast']:.4f} between its "
            "strongest and weakest channel.")
    return said


# A star must be seen in at least this many frames to be believed.
# Three of five: a noise spike that fools one frame with chance q fools
# three with chance of order q cubed, while a real star sitting at the
# detection limit -- a coin toss per frame -- still passes half the
# time. Demanding all five would throw away exactly the faint stars
# the vote exists to rescue.
LEAST_VOTES = 3
# How far apart two sightings can land and still be one star. The
# frames shift by fractions of a pixel, so the same star's peak walks
# a little; two pixels covers that walk without letting neighbours
# merge.
VOTE_RADIUS = 2


def cast_votes(positions_by_frame: list[np.ndarray],
               shape: tuple[int, int],
               radius: int = VOTE_RADIUS) -> tuple[np.ndarray, np.ndarray]:
    """Every sighting across all frames, and how many frames agree.

    A star is in the same place in every exposure ever made of it; a
    speck of noise is in one frame and nowhere else. So position is
    what votes -- NOT brightness, which honestly varies ten or twenty
    percent frame to frame as a star lands centred on a pixel or
    astride two. Returns the candidate positions (one per distinct
    sighting) and each candidate's vote count.
    """
    height, width = shape
    stamped = []
    for found in positions_by_frame:
        seen = np.zeros(shape, bool)
        if len(found):
            seen[found[:, 0], found[:, 1]] = True
        for _ in range(radius):
            seen |= (np.roll(seen, 1, 0) | np.roll(seen, -1, 0)
                     | np.roll(seen, 1, 1) | np.roll(seen, -1, 1))
        stamped.append(seen)
    claimed = np.zeros(shape, bool)
    candidates = []
    for found in positions_by_frame:
        for down, across in found:
            if claimed[down, across]:
                continue
            candidates.append((down, across))
            low_d = max(down - radius, 0)
            low_a = max(across - radius, 0)
            claimed[low_d:down + radius + 1,
                    low_a:across + radius + 1] = True
    spots = np.asarray(candidates, int) if candidates \
        else np.zeros((0, 2), int)
    votes = np.zeros(len(spots), int)
    for seen in stamped:
        if len(spots):
            votes += seen[spots[:, 0], spots[:, 1]].astype(int)
    return spots, votes


def vote_stars(frames: list[Any],
               transforms: list[tuple[float, tuple[float, float]]],
               bar: float = 5.0, least: int = LEAST_VOTES,
               radius: int = VOTE_RADIUS) -> dict[str, Any]:
    """The stars several frames agree on, and the sightings they do not.

    Each frame is asked for its own detections against its own grain;
    the positions ride the registration's transforms onto one grid,
    and repetition decides. Nothing is deleted: what failed the vote
    is returned alongside what passed, because a sighting seen once
    might be noise or might be a meteor, and that is the
    photographer's call, not arithmetic's.

    One caution the arithmetic cannot remove: a hot pixel is in the
    same SENSOR place every frame, so it defeats the vote exactly when
    the frames barely moved. The frames' spread is measured and
    reported so a still tripod is not mistaken for proof.
    """
    from development_engine import _box_mean
    from superimpose_kernel import _frame, turned

    held = []
    for source in frames:
        if isinstance(source, (str, Path)):
            held.append(np.clip(np.asarray(
                _frame(Path(source), demosaic=True), np.float32), 0.0, 1.0))
        else:
            held.append(np.clip(np.asarray(source, np.float32), 0.0, 1.0))
    shape = held[0].shape[:2]
    centre = ((shape[1] - 1) / 2.0, (shape[0] - 1) / 2.0)
    positions_by_frame = []
    grains = []
    for picture, (turn, shift) in zip(held, transforms):
        over = _luma(picture) - _box_mean(_luma(picture), SKY_REACH)
        grain = sky_grain(over)
        grains.append(grain)
        found = _peaks(over, grain * bar)
        if len(found):
            # Detections speak (row, column); the transform speaks
            # (x, y). Carried across and back.
            spots = np.stack([found[:, 1], found[:, 0]],
                             axis=1).astype(np.float32)
            landed = turned(spots, turn, centre) + np.asarray(
                shift, np.float32)
            rows = np.clip(np.round(landed[:, 1]), 0, shape[0] - 1)
            cols = np.clip(np.round(landed[:, 0]), 0, shape[1] - 1)
            positions_by_frame.append(
                np.stack([rows, cols], axis=1).astype(int))
        else:
            positions_by_frame.append(np.zeros((0, 2), int))
    spots, votes = cast_votes(positions_by_frame, shape, radius)
    counted = {f"{n}_of_{len(held)}": int((votes == n).sum())
               for n in range(len(held), 0, -1)}
    moves = [np.hypot(shift[0], shift[1]) for _turn, shift in transforms]
    return {
        "frames": len(held),
        "bar_sigma": bar,
        "least": least,
        "radius_px": radius,
        "candidates": spots,
        "votes": votes,
        "confirmed": spots[votes >= least],
        "flagged": spots[votes < least],
        "by_votes": counted,
        "per_frame_detections": [int(len(f)) for f in positions_by_frame],
        "per_frame_grain": [round(g, 6) for g in grains],
        # Under about twice the vote radius, a hot pixel can follow
        # itself from frame to frame and vote for itself.
        "frame_spread_px": round(float(max(moves) - min(moves)), 2),
        "hot_pixels_screened": float(max(moves) - min(moves))
                               > 2.0 * radius,
    }


def honesty(picture: Any, against: Any,
            bar: float = 5.0, slack: int = 2) -> dict[str, Any]:
    """How many of a picture's "stars" are stars.

    The only way to know is to ask a deeper picture of the same sky.
    A star is in the same place in every exposure ever made of it; a
    speck of noise is in one frame and nowhere else. So the deeper
    picture -- a stack of the same frames, which has the same stars and
    less of the grain -- is asked where its own stars are, and every
    detection in the first picture is looked up in that list.

    This is the check that catches a stretch which looks marvellous
    because it has amplified its own grain into a sky full of things
    that were never there. Both pictures must be of the same sky in
    the same pixels; a stack and one of its own frames qualify.
    """
    from development_engine import _box_mean

    def over_of(source):
        if isinstance(source, (str, Path)):
            from superimpose_kernel import _frame

            held = _frame(Path(source), demosaic=True)
        else:
            held = source
        held = np.clip(np.asarray(held, np.float32), 0.0, 1.0)
        grey = _luma(held)
        return grey - _box_mean(grey, SKY_REACH)

    mine, deeper = over_of(picture), over_of(against)
    if mine.shape != deeper.shape:
        return {"error": "the two pictures are not the same shape"}
    truth = np.zeros(deeper.shape, bool)
    for down, across in _peaks(deeper, sky_grain(deeper) * bar):
        truth[down, across] = True
    for _ in range(slack):
        truth |= (np.roll(truth, 1, 0) | np.roll(truth, -1, 0)
                  | np.roll(truth, 1, 1) | np.roll(truth, -1, 1))
    found = _peaks(mine, sky_grain(mine) * bar)
    if not len(found):
        return {"claimed": 0, "real": 0, "invented": 0, "honesty": 1.0}
    real = int(truth[found[:, 0], found[:, 1]].sum())
    return {
        "claimed": int(len(found)),
        "real": real,
        "invented": int(len(found) - real),
        # What share of what it says it found is actually there.
        "honesty": round(real / len(found), 3),
        "deeper_has": int(truth.sum() and len(_peaks(
            deeper, sky_grain(deeper) * bar))),
    }


def report(seen: dict[str, Any]) -> str:
    """The assessment as a person would read it."""
    sky, shape, span = seen["sky"], seen["shape"], seen["range"]
    lines = [f"{seen['name']}  ({seen['pixels']}, {seen['megapixels']} MP)"]
    lines.append(
        "  stars standing clear of the sky's own grain: "
        + ", ".join(f"{seen['depth'][f'over_{bar:g}_sigma']:,} at {bar:g}x"
                    for bar in SIGNIFICANCE))
    lines.append(
        "  stars above a fixed brightness over the local sky: "
        + ", ".join(f"{seen['plain_count'][f'over_{step:g}']:,} over {step:g}"
                    for step in PLAIN_STEPS))
    lines.append(
        f"  the sky sits at {sky['level']:.4f} with grain {sky['grain']:.5f}"
        f"; it wanders {sky['unevenness']:.4f} across the frame "
        f"({sky['unevenness_in_grain']:.1f}x its grain)")
    lines.append(
        f"  its colour is {'/'.join(f'{v:.4f}' for v in sky['red_green_blue'])}"
        f" in red/green/blue, a cast of {sky['colour_cast']:.4f}")
    lines.append(
        f"  stars are {shape['width_px']:.2f} px wide and "
        f"{shape['roundness']:.3f} round"
        + (f", leaning {shape['leans_degrees']:+.0f} degrees"
           if shape["roundness"] > 1.05 else "")
        + f" (from {shape['stars_measured']:,} of them)")
    if "star_peak_over_grain" in span:
        lines.append(
            f"  the middling star stands {span['star_peak_over_grain']:.0f}x "
            f"the grain above the sky")
    lines.append(
        f"  clipped: {span['black_clipped_percent']:.3f}% black, "
        f"{span['white_clipped_percent']:.3f}% white")
    for worry in concerns(seen):
        lines.append(f"  ! {worry}")
    return "\n".join(lines)


def compare(seen: list[dict[str, Any]]) -> str:
    """Several assessments side by side, one row each."""
    if not seen:
        return "nothing to compare"
    head = (f"  {'picture':30s} {'sky':>8s} {'grain':>9s} "
            f"{'5 sigma':>9s} {'10 sigma':>9s} {'>0.02':>9s} "
            f"{'width':>7s} {'round':>7s} {'even':>7s} {'white%':>7s}")
    rows = [head, "  " + "-" * (len(head) - 2)]
    for item in seen:
        sky, shape, span = item["sky"], item["shape"], item["range"]
        rows.append(
            f"  {item['name'][:30]:30s} {sky['level']:8.4f} "
            f"{sky['grain']:9.5f} "
            f"{item['depth']['over_5_sigma']:9,d} "
            f"{item['depth']['over_10_sigma']:9,d} "
            f"{item['plain_count']['over_0.02']:9,d} "
            f"{shape['width_px']:7.2f} {shape['roundness']:7.3f} "
            f"{sky['unevenness_in_grain']:7.1f} "
            f"{span['white_clipped_percent']:7.3f}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    files = [item for item in argv if not item.startswith("-")]
    if not files:
        print("usage: python -m opencull_gui.starfield <picture>... [--json]")
        return 2
    seen = []
    for item in files:
        try:
            seen.append(assess(item))
        except Exception as exc:                 # noqa: BLE001 - report it
            print(f"{item}: cannot be read -- {type(exc).__name__}: {exc}")
    if not seen:
        return 1
    if as_json:
        print(json.dumps(seen, indent=2))
        return 0
    for item in seen:
        print(report(item))
        print()
    if len(seen) > 1:
        print(compare(seen))
    return 0


__all__ = ["assess", "report", "compare", "concerns", "honesty",
           "cast_votes", "vote_stars", "LEAST_VOTES", "VOTE_RADIUS",
           "sky_grain",
           "SIGNIFICANCE", "PLAIN_STEPS"]


if __name__ == "__main__":
    raise SystemExit(main())
