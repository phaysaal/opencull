"""One click from a folder of night frames to the finished picture.

Everything this kernel does was learned the slow way on real skies:
register with the roll rescue, screen the clouded frames, stack
sigma-clipped in light with exposures normalised -- and then refuse to
continue unless the stack passes the photographer's own test, the one
that caught what every metric missed: photograph the same sky twice,
superimpose, and a star must appear ONCE.

Only after the stack proves itself is it developed: the measured night
start, the stretch lifting luminance alone so the stars keep their own
colour, a breath of vibrance for the palest of them, one gentle
black level capped by the sky itself, and the photographer's last
tune: two more black input, after everything.

Deterministic end to end: no model is asked for anything.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

import superimpose_kernel as _sky
from superimpose_kernel import LEAST_AGREEING, _frame, frames_of

# The photographer's finishing black level, in this engine's units.
# Calibrated on the hand-finished passes that won the panels blind:
# their first touch measured 4.5 of this op, their second 3.7 more --
# together the depth of the consensus champion. The champion's faint
# warm cast is deliberately NOT copied; neutrality was the one thing
# every judge scored in its favour.
FINISH_LEVEL = 8.2


# The compiler reads what a kernel DEFINES, not what it imports, so
# the stacking stages are spoken for here in name and handed straight
# to the kernel that owns them.
def register(photos, pattern="*.RAF", only="", focal_mm=0.0,
             sensor_mm=23.5, demosaic=True):
    return _sky.register(photos, pattern, only, focal_mm,
                         sensor_mm, demosaic)


def screen_frames(placed):
    return _sky.screen_frames(placed)


def screen_note(screened):
    return _sky.screen_note(screened)


def superimpose(screened, mode="clipped", output="", darks="",
                sigma=2.5, flats="", bias="", scale=2.0, pixfrac=0.8):
    return _sky.superimpose(screened, mode, output, darks, sigma,
                            flats, bias, scale, pixfrac)


def stack_valid(stacked):
    return _sky.stack_valid(stacked)


def stack_note(stacked):
    return _sky.stack_note(stacked)


# A frame must agree this convincingly to be believed into the stack:
# twice the registration floor, or fifteen percent of its own stars,
# whichever asks more. Measured on the album that taught the lesson: a
# real registration ran 38 to 140 agreeing stars, and the coincidence
# that put a faint second copy of every star into a stack ran 8 to 16.
def _confident(item: dict) -> bool:
    if item.get("placed_by"):
        return True                        # the pole vouches for it
    stars = int(item.get("stars", 0) or 0)
    floor = max(2 * LEAST_AGREEING, math.ceil(0.15 * max(stars, 1)))
    return int(item.get("agreed", 0)) >= floor


def frames_confident(register_text: str) -> bool:
    """Every frame going into the stack really registered.

    The bar is deliberately higher than the stacker's own: the stacker
    asks whether a placement exists, this asks whether it deserves to
    be believed. A frame that scraped past on a handful of stars is
    exactly how a ghosted stack begins.
    """
    told = json.loads(register_text)
    frames = [item for item in told.get("frames", [])
              if item.get("keep", True)]
    if len(frames) < 2:
        return False
    return all(_confident(item) for item in frames[1:])


def _stack_pixels(told: dict) -> np.ndarray:
    import tifffile

    held = np.asarray(tifffile.imread(told["stack"]))
    if np.issubdtype(held.dtype, np.integer):
        held = held.astype(np.float32) / float(np.iinfo(held.dtype).max)
    return np.clip(held.astype(np.float32), 0.0, 1.0)


def _corner_roundness(shown: np.ndarray) -> dict[str, float]:
    """How oval the stars are, in each corner and the middle."""
    from development_engine import _box_mean
    from opencull_gui.starfield import _peaks, sky_grain

    grey = (shown[..., 0] * 0.2126 + shown[..., 1] * 0.7152
            + shown[..., 2] * 0.0722).astype(np.float32)
    height, width = grey.shape
    span = min(height, width) // 3
    out: dict[str, float] = {}
    places = (("top-left", 40, 40), ("top-right", 40, width - span - 40),
              ("bottom-left", height - span - 40, 40),
              ("bottom-right", height - span - 40, width - span - 40),
              ("middle", (height - span) // 2, (width - span) // 2))
    for label, y0, x0 in places:
        tile = grey[y0:y0 + span, x0:x0 + span]
        over = tile - _box_mean(tile, 40)
        found = _peaks(over, sky_grain(over) * 25.0)
        found = found[(found[:, 0] > 8) & (found[:, 0] < span - 8)
                      & (found[:, 1] > 8) & (found[:, 1] < span - 8)]
        found = found[np.argsort(
            over[found[:, 0], found[:, 1]])[::-1][:150]]
        if len(found) < 20:
            continue
        yy, xx = np.mgrid[-8:9, -8:9]
        sxx = syy = sxy = 0.0
        counted = 0
        for down, across in found:
            patch = np.clip(over[down - 8:down + 9,
                                 across - 8:across + 9], 0.0, None)
            total = float(patch.sum())
            if total <= 0.0:
                continue
            share = patch / total
            mid_x = float((xx * share).sum())
            mid_y = float((yy * share).sum())
            sxx += float((((xx - mid_x) ** 2) * share).sum())
            syy += float((((yy - mid_y) ** 2) * share).sum())
            sxy += float((((xx - mid_x) * (yy - mid_y)) * share).sum())
            counted += 1
        if not counted:
            continue
        sxx /= counted
        syy /= counted
        sxy /= counted
        trace = sxx + syy
        root = float(np.sqrt(max(trace * trace / 4.0
                                 - (sxx * syy - sxy * sxy), 0.0)))
        out[label] = float(np.sqrt((trace / 2.0 + root)
                                   / max(trace / 2.0 - root, 1e-9)))
    return out


def stack_true(stacked: str, pattern: str = "*.RAF") -> bool:
    """The photographer's own test: a star must appear once.

    A stack that did not line up wears it as stars more oval than one
    frame's -- a doubled star close in is an oval, and stacking done
    right makes stars ROUNDER, never longer. So every corner of the
    stack is held against the same corner of a single frame, and the
    stack must not be more than a tenth worse anywhere.
    """
    told = json.loads(stacked)
    if told.get("error"):
        return False
    paths = frames_of(told.get("photos", "."), pattern)
    if not paths:
        return True                        # nothing to compare against
    one = np.clip(np.asarray(
        _frame(paths[0], demosaic=True), np.float32), 0.0, 1.0)
    stack = _stack_pixels(told)
    if stack.shape[:2] != one.shape[:2]:
        one = one[:stack.shape[0], :stack.shape[1]]
    base = _corner_roundness(one)
    got = _corner_roundness(stack)
    for label, value in got.items():
        if label in base and value > base[label] * 1.10 + 0.02:
            return False
    return True


def coverage_inset(register_text: str) -> int:
    """How far in from the edge every frame is actually present.

    A shifted frame leaves a margin of the reference it never covered,
    and a rolled one leaves wedges; inside that band the stack is
    thinner than it claims, and the boundary shows as a faint step a
    viewer can find even when a detector does not -- one did. Cropping
    to the ground every frame stands on removes the band entirely.
    """
    try:
        told = json.loads(register_text)
    except (TypeError, ValueError):
        return 0
    frames = [item for item in told.get("frames", [])
              if item.get("keep", True)]
    if not frames:
        return 0
    reach = 0.0
    for item in frames:
        shift = max(abs(float(item.get("dx", 0.0))),
                    abs(float(item.get("dy", 0.0))))
        wedge = 0.0
        turn = abs(float(item.get("turn", 0.0)))
        if turn > 1e-6:
            # A turn about the middle carries the far edge sideways by
            # the half-diagonal times the angle; that is the widest
            # sliver it can leave uncovered.
            wedge = math.sin(math.radians(turn)) * 4000.0
        reach = max(reach, shift + wedge)
    return int(math.ceil(reach)) + 4


def coverage_insets(register_text: str, height: int, width: int,
                    slack: float = 0.0) -> tuple[int, int, int, int]:
    """How far in from EACH edge every frame is actually present.

    The uniform inset takes the worst displacement any frame ever had
    and charges it to all four sides -- but a knock pushes frames ONE
    way, and the wedge a roll leaves sits in particular corners. Here
    every frame's footprint is laid into the reference geometry the
    same way the resampler lays the frame itself, the footprints are
    intersected, and each side gives up only what the intersection
    actually demands. On the album that asked for this, the uniform
    formula took 127 px all round; two of those sides owed almost
    nothing -- and a star the photographer cared about lived there.

    slack widens the safety margin, for frames whose field correction
    reached beyond their rigid footprint.
    """
    top, bottom, left, right = (4, 4, 4, 4)
    try:
        told = json.loads(register_text)
    except (TypeError, ValueError):
        return (0, 0, 0, 0)
    frames = [item for item in told.get("frames", [])
              if item.get("keep", True)]
    if not frames:
        return (0, 0, 0, 0)
    step = 8
    ys = np.arange(0, height, step, dtype=np.float64) + 0.5
    xs = np.arange(0, width, step, dtype=np.float64) + 0.5
    grid_x, grid_y = np.meshgrid(xs, ys)
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    margin = 3.0 + max(float(slack), 0.0)
    covered = np.ones(grid_x.shape, bool)
    for item in frames:
        dx = float(item.get("dx", 0.0))
        dy = float(item.get("dy", 0.0))
        angle = np.radians(-float(item.get("turn", 0.0)))
        cos, sin = float(np.cos(angle)), float(np.sin(angle))
        px = grid_x - dx - cx
        py = grid_y - dy - cy
        sx = cx + px * cos - py * sin
        sy = cy + px * sin + py * cos
        covered &= ((sx >= margin) & (sx <= width - 1 - margin)
                    & (sy >= margin) & (sy <= height - 1 - margin))
    rows, cols = covered.shape
    t = b = left_ = r = 0
    while True:
        sub = covered[t:rows - b, left_:cols - r]
        if sub.size == 0 or bool(sub.all()):
            break
        bad = {"t": int((~sub[0]).sum()), "b": int((~sub[-1]).sum()),
               "l": int((~sub[:, 0]).sum()), "r": int((~sub[:, -1]).sum())}
        worst = max(bad, key=lambda side: bad[side])
        if bad[worst] == 0:
            t += 1
            b += 1
            left_ += 1
            r += 1
            continue
        if worst == "t":
            t += 1
        elif worst == "b":
            b += 1
        elif worst == "l":
            left_ += 1
        else:
            r += 1
    return (top + t * step, bottom + b * step,
            left + left_ * step, right + r * step)


def develop_show(stacked: str, placed: str = "",
                 pattern: str = "*.RAF",
                 colour: float = 100.0,
                 glow_level: float = 18.0,
                 flatten: float = 100.0,
                 velvet: float = 0.0) -> str:
    """The stack developed the way the good one was, and measured.

    The recipe is not a taste, it is the one that won a week of
    measured comparisons: the night start read off this very stack's
    own sky, the stretch carrying colour by ratio, vibrance feeding
    only the palest stars, and a single gentle black level at the very
    end -- because every ruined picture in that week was ruined by a
    clip, and every clip was either early, repeated, or deep.
    """
    from PIL import Image

    from development_engine import _apply_global, _decoded, _encoded
    from opencull_gui.nightstart import night_start
    from opencull_gui.starfield import assess, honesty

    told = json.loads(stacked)
    if told.get("error"):
        return json.dumps({"error": told["error"]})
    stack = _stack_pixels(told)
    # Frames whose field correction reached beyond their rigid
    # footprint widen the safety margin by exactly that reach.
    trued = told.get("field_corrected") or {}
    slack = max((float(note.get("before_px", 0.0) or 0.0)
                 for note in trued.values()), default=0.0)
    top, bottom, left, right = coverage_insets(
        placed, stack.shape[0], stack.shape[1], slack)
    if (top + bottom) * 2 < stack.shape[0] \
            and (left + right) * 2 < stack.shape[1]:
        stack = stack[top:stack.shape[0] - bottom,
                      left:stack.shape[1] - right]
    inset = max(top, bottom, left, right)
    frames = frames_of(told.get("photos", "."), pattern)
    if not frames:
        return json.dumps({"error": "no frames to read the camera from"})
    keep = min(max(float(colour or 0.0), 0.0), 100.0)
    _sky._say(97, "measuring the night start")
    measured = night_start(frames[0], shown=stack,
                           frames=int(told.get("frames_used", 1)))
    # How much of the sky's own large-scale light to take out. At a
    # hundred the background is flattened as the night start measured
    # it; at zero the sky keeps its weather -- the glow and the band a
    # real night actually had, which one album's viewers mistook for
    # the Milky Way and liked better than the flat version. The fit
    # cannot tell a slope from a galaxy, so the choice belongs to a
    # dial, not to the arithmetic.
    even = min(max(float(flatten if flatten is not None else 100.0),
                   0.0), 100.0)
    operations = []
    for item in measured["operations"]:
        if item.get("op") == "color.background":
            if even <= 0.0:
                continue
            value = dict(item.get("value") or {})
            value["strength"] = round(even, 1)
            item = dict(item, value=value)
        if item.get("op") == "tone.preserve":
            item = dict(item, value=keep)
        operations.append(item)
    if keep > 0.0:
        operations.append({
            "op": "color.vibrance", "unit": "percent", "mode": "delta",
            "value": round(22.0 * keep / 100.0, 1),
            "source_instruction": "a breath for the palest stars",
            "enabled": True})
    # The level is asked for, then CAPPED by the picture itself:
    # clips last, once, early. However dark a look is wanted, the cut
    # may reach no deeper than the darkest percent of the developed
    # sky -- on an album with strong vignetting the corners arrive
    # lower than the middle ever suggests, and a level tuned on one
    # album quietly beheads the next. Asked eighteen, one real album
    # needed eleven; the cap found that out so nobody had to.
    _sky._say(98, "developing the show")
    grown = np.clip(_encoded(np.clip(_apply_global(
        _decoded(stack).astype(np.float32), operations), 0.0, None)),
        0.0, 1.0)
    asked = min(max(float(glow_level or 0.0), 0.0), 64.0)
    lum = (grown * np.array([0.2126, 0.7152, 0.0722],
                            np.float32)).sum(axis=2)
    allowed = float(np.percentile(lum, 1.0)) * 255.0 * 0.95
    # The cap governs the TOTAL cut -- the asked level and the
    # finishing touch below share it, the touch served first.
    level = min(asked, max(allowed - FINISH_LEVEL, 0.0))
    if level > 0.0:
        grown = np.clip(_encoded(np.clip(_apply_global(
            _decoded(grown).astype(np.float32), [{
                "op": "levels.black_input", "unit": "level-8bit",
                "mode": "absolute", "value": round(level, 1),
                "enabled": True}]), 0.0, None)), 0.0, 1.0)
        operations.append({
            "op": "levels.black_input", "unit": "level-8bit",
            "mode": "absolute", "value": round(level, 1),
            "source_instruction": "one gentle level, last, capped by "
                                  "the sky's own darkest percent",
            "enabled": True})
    # The photographer's last tune, learned from the blind panels: a
    # final touch of black input, applied after everything else. Small
    # enough to deepen the sky without beheading it; it is what
    # separated the winning hand-finished pass from the recipe alone.
    # Their editor called it "+2.0"; measured against the very file
    # that hand produced, it is 4.5 of THIS op -- level tools do not
    # agree on what a step of black means, so the picture arbitrates.
    # On a sky too dark to afford it, the touch shrinks with the cap.
    finish = min(FINISH_LEVEL, max(allowed - level, 0.0))
    if finish > 0.0:
        grown = np.clip(_encoded(np.clip(_apply_global(
            _decoded(grown).astype(np.float32), [{
                "op": "levels.black_input", "unit": "level-8bit",
                "mode": "absolute", "value": round(finish, 1),
                "enabled": True}]), 0.0, None)), 0.0, 1.0)
        operations.append({
            "op": "levels.black_input", "unit": "level-8bit",
            "mode": "absolute", "value": round(finish, 1),
            "source_instruction": "the photographer's last tune",
            "enabled": True})
    # The velvet, if asked for: the sky dimmed UNDER the stars the
    # matched filter vouches for, after everything else -- darkness
    # without deletion. Off by default; the dial is the choice.
    soft = min(max(float(velvet or 0.0), 0.0), 100.0)
    if soft > 0.0:
        shape = next((dict(item) for item in operations
                      if item.get("op") == "detail.star_shape"), None)
        hush = ([shape] if shape else []) + [{
            "op": "detail.velvet", "unit": "percent", "mode": "delta",
            "value": round(soft, 1),
            "source_instruction": "the sky dimmed under the stars",
            "enabled": True}]
        grown = np.clip(_encoded(np.clip(_apply_global(
            _decoded(grown).astype(np.float32), hush), 0.0, None)),
            0.0, 1.0)
        operations.append(hush[-1])
    shown = grown
    home = Path(told["stack"]).parent
    picture = home / "show.jpg"
    deep = home / "show.tiff"
    Image.fromarray((shown * 255.0 + 0.5).astype(np.uint8)).save(
        picture, quality=94, optimize=True)
    import tifffile

    tifffile.imwrite(deep, (shown * 65535.0 + 0.5).astype(np.uint16))
    # The recipe, portable: every operation with its measured value,
    # bound to the very stack it was measured on. This is what lets
    # the app's Fine Tune open the show with the program's own dials
    # live -- the report below names the operations, this file IS them.
    stack_file = Path(told["stack"]).resolve()
    photos_root = Path(str(told.get("photos") or ".")).resolve()
    try:
        bound = stack_file.relative_to(photos_root).as_posix()
    except ValueError:
        bound = str(stack_file)
    recipe_file = home / "show-recipe.json"
    recipe_file.write_text(json.dumps({
        "name": "The show, as measured",
        "photo": bound,
        "recipe": {"title": "The show, as measured",
                   "operations": operations}}, indent=2))
    # Measured as DELIVERED: the report describes the very file a
    # person will open -- read back from disk, its eight bits and its
    # compression included -- because a report about a finer picture
    # than the one being handed over is a report about nothing.
    _sky._say(99, "measuring the delivered file")
    delivered = np.asarray(Image.open(picture).convert("RGB"),
                           np.float32) / 255.0
    seen = assess(delivered, name=picture.name)
    truth = honesty(delivered, stack)
    return json.dumps({
        "format": "darkimiya-night-show-v1",
        "picture": str(picture), "deep": str(deep),
        "recipe": str(recipe_file),
        "stack": told["stack"],
        "cropped_border_px": int(inset),
        "cropped_insets": {"top": int(top), "bottom": int(bottom),
                           "left": int(left), "right": int(right)},
        "flatten": round(even, 1),
        "level_asked": round(float(min(max(float(glow_level or 0.0),
                                           0.0), 64.0)), 1),
        "level_used": round(float(level), 1),
        "finish_level": round(float(finish), 1),
        "velvet": round(soft, 1),
        "frames_used": told.get("frames_used"),
        "operations": [item["op"] for item in operations],
        "sky": seen["sky"], "shape": seen["shape"],
        "depth": seen["depth"], "range": seen["range"],
        "honesty": truth,
    }, indent=2)


def shown_well(shown: str) -> bool:
    """The picture is not ruined, by the ways pictures actually get ruined.

    A week of measured comparisons ruined pictures exactly three ways:
    blacks clipped away, a sky crushed to nothing, highlights blown.
    Those are gated hard. The honesty figure -- how much of what the
    picture shows is verified against its own stack -- is a STYLE
    number: a gentle development runs it high and a showy stretch
    spends some of it on purpose, so it is printed in the report and
    only backstopped here, at the level where the worst ruin ever
    measured still fails and every legitimate rendering passes.
    """
    told = json.loads(shown)
    if told.get("error"):
        return False
    if float(told["range"]["black_clipped_percent"]) > 2.0:
        return False
    if float(told["range"]["white_clipped_percent"]) > 0.5:
        return False
    if float(told["sky"]["level"]) < 0.01:
        return False
    return float(told.get("honesty", {}).get("honesty", 0.0)) >= 0.30


def show_note(shown: str) -> str:
    told = json.loads(shown)
    if told.get("error"):
        return f"nothing to show: {told['error']}"
    sky = told["sky"]
    truth = told.get("honesty", {})
    return (f"{told['picture']}: {told['depth']['over_10_sigma']:,} stars "
            f"standing ten times clear of a sky of {sky['level']:.3f}, "
            f"{truth.get('honesty', 0.0):.0%} of every claim verified "
            f"against the stack, "
            f"{told['range']['black_clipped_percent']:.2f}% touched black.")


def show_home(shown: str) -> str:
    told = json.loads(shown)
    home = Path(told.get("picture", "show.jpg")).parent
    return str(home / "show-report.json")
