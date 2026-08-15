"""Aligning an eclipse sequence shot by hand, for a timelapse.

The photographs were not taken from a tripod, so the sun drifts frame
to frame. The cure is the photographer's own algorithm: find the sun in
every frame, then take the LARGEST COMMON CROP that holds it at a fixed
offset -- minimum margins across the whole sequence, so no frame ever
needs padding or invented pixels, and every crop has identical
dimensions, ready to assemble straight into video.

Two amendments to the algorithm as first stated, both agreed:

  * The sun's centre and diameter come from a robust circle fit to the
    visible limb -- the sun's own uneclipsed edge, which stays a clean
    circular arc through every phase -- rather than from the top-most
    and side-most points of the bright region. The original's
    L-or-R-by-visibility case analysis generalises to every direction
    for free, and the top no longer has to be visible (in this series
    it always is; the fit does not care).
  * Time is used for rejection, never for placement. Each crop is
    pinned to its own frame's fitted centre -- hand motion between
    frames is the thing being corrected, and smoothing the applied
    centres would quietly put it back. What the smoothed track is for
    is catching a fit that disagrees wildly with its neighbours, which
    is a detection failure wearing a sun's costume; such frames are
    set aside by name.

The colour shift is the house's own: a recipe's global operations
applied with the development engine's primitives -- the same math the
develop page runs -- so the look cannot drift from what the recipes
mean elsewhere. Masks in a recipe are skipped with a note: on a
sun-locked crop a spatial mask aims at nothing stable.

No model is asked for anything. The whole pipeline is deterministic,
so a 300-frame run costs rendering time and nothing else.
"""

from __future__ import annotations

import io
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from development_engine import _apply_global, _decoded, _encoded, active

FORMAT = "darkimiya-timelapse-plan-v1"

# A fitted diameter this far from the sequence's median means a
# different lens or a failed fit; either way the frame cannot share a
# pixel-space crop with the rest and is set aside by name.
SCALE_TOLERANCE = 0.12

# The limb is the edge of the truly bright thing, not of its glow.
BRIGHT_FRACTION = 0.88


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# --- reading the sequence -------------------------------------------------

def _preview(path: Path) -> Image.Image:
    """The frame's own full-size embedded rendering, or the file itself."""
    if path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
        return Image.open(path).convert("RGB")
    import rawpy

    with rawpy.imread(str(path)) as raw:
        thumb = raw.extract_thumb()
    if thumb.format.name.lower() == "jpeg":
        return Image.open(io.BytesIO(thumb.data)).convert("RGB")
    return Image.fromarray(thumb.data).convert("RGB")


def _taken_at(path: Path) -> str:
    """Capture time, because file names wrap: DSCF9999 precedes DSCF1001."""
    try:
        from PIL import ExifTags

        image = _preview(path)
        exif = image.getexif() or {}
        for tag, value in exif.items():
            if ExifTags.TAGS.get(tag) in ("DateTimeOriginal", "DateTime"):
                return str(value)
    except Exception:                                # noqa: BLE001 - mtime
        pass
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def list_frames(photos: str, pattern: str = "*.RAF") -> list[dict[str, str]]:
    root = Path(str(photos)).expanduser().resolve()
    found = sorted(root.glob(str(pattern)))
    ordered = sorted(
        ({"name": path.name, "taken": _taken_at(path)} for path in found),
        key=lambda item: (item["taken"], item["name"]))
    return ordered


# --- finding the sun -------------------------------------------------------

def _limb_points(grey: np.ndarray) -> np.ndarray:
    """Edge pixels of the bright region: the limb, plus the moon's bite."""
    top = float(grey.max())
    if top <= 0:
        return np.zeros((0, 2))
    bright = grey >= BRIGHT_FRACTION * top
    if bright.sum() < 20:
        return np.zeros((0, 2))
    inner = bright.copy()
    inner[1:-1, 1:-1] = (
        bright[1:-1, 1:-1] & bright[:-2, 1:-1] & bright[2:, 1:-1]
        & bright[1:-1, :-2] & bright[1:-1, 2:])
    edge = bright & ~inner
    ys, xs = np.nonzero(edge)
    return np.column_stack([xs, ys]).astype(np.float64)


def _circle_through(a, b, c) -> tuple[float, float, float] | None:
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1])
             + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-9:
        return None
    ux = ((a[0] ** 2 + a[1] ** 2) * (b[1] - c[1])
          + (b[0] ** 2 + b[1] ** 2) * (c[1] - a[1])
          + (c[0] ** 2 + c[1] ** 2) * (a[1] - b[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (c[0] - b[0])
          + (b[0] ** 2 + b[1] ** 2) * (a[0] - c[0])
          + (c[0] ** 2 + c[1] ** 2) * (b[0] - a[0])) / d
    r = float(np.hypot(a[0] - ux, a[1] - uy))
    return float(ux), float(uy), r


def fit_limb(grey: np.ndarray) -> dict[str, float] | None:
    """The solar disc, fitted to the limb and refined on its inliers.

    RANSAC chooses among the arcs -- and on a thin crescent there are
    two, near-equal in length AND radius, because the moon matches the
    sun's angular size; that is what an eclipse is. Inlier count alone
    elected the moon on the thin frames, and the crescent wandered
    around the crops by the bite's direction. The tiebreak is
    containment, which cannot confuse them: every bright pixel lies
    inside the solar circle, and almost none inside the lunar one --
    the bite is exactly where the moon is. Seeded, so the same frame
    always fits the same circle.
    """
    points = _limb_points(grey)
    if len(points) < 12:
        return None
    top = float(grey.max())
    bys, bxs = np.nonzero(grey >= BRIGHT_FRACTION * top)
    stride = max(1, len(bxs) // 800)
    mass_x = bxs[::stride].astype(np.float64)
    mass_y = bys[::stride].astype(np.float64)
    chooser = random.Random(20260812)
    best, best_score = None, 0
    tolerance = max(2.0, 0.01 * max(grey.shape))
    for _ in range(220):
        a, b, c = (points[chooser.randrange(len(points))] for _ in range(3))
        made = _circle_through(a, b, c)
        if made is None:
            continue
        cx, cy, r = made
        if not 6 <= r <= max(grey.shape):
            continue
        held = float((np.hypot(mass_x - cx, mass_y - cy)
                      <= r + tolerance).mean()) if len(mass_x) else 0.0
        if held < 0.9:
            continue          # the moon's circle: the light is not in it
        spread = np.abs(np.hypot(points[:, 0] - cx, points[:, 1] - cy) - r)
        score = int((spread < tolerance).sum())
        if score > best_score:
            best, best_score = made, score
    if best is None or best_score < max(12, 0.25 * len(points)):
        return None
    cx, cy, r = best
    for _ in range(3):
        spread = np.abs(np.hypot(points[:, 0] - cx, points[:, 1] - cy) - r)
        inliers = points[spread < tolerance]
        if len(inliers) < 8:
            break
        # Kasa least squares on the inliers.
        x, y = inliers[:, 0], inliers[:, 1]
        column = np.column_stack([x, y, np.ones(len(inliers))])
        target = x ** 2 + y ** 2
        try:
            (a2, b2, c2), *_rest = np.linalg.lstsq(column, target, rcond=None)
        except np.linalg.LinAlgError:
            break
        cx, cy = a2 / 2.0, b2 / 2.0
        r = float(np.sqrt(max(c2 + cx ** 2 + cy ** 2, 1.0)))
    return {"cx": float(cx), "cy": float(cy), "r": float(r),
            "inliers": int(best_score)}


def _smoothed(values: list[float], window: int = 5) -> list[float]:
    """A running median: jitter dies, real drift survives."""
    half = window // 2
    out = []
    for index in range(len(values)):
        lo = max(0, index - half)
        hi = min(len(values), index + half + 1)
        out.append(float(np.median(values[lo:hi])))
    return out


# --- the survey and the plan ------------------------------------------------

def survey(photos: str, pattern: str = "*.RAF",
           detect_edge: float = 1200) -> str:
    """Every frame found, timed, and fitted; the excluded named.

    Detection runs on a downscaled copy and the fit is scaled back, so
    300 frames survey in minutes; the crop itself is always taken from
    the full-resolution preview.
    """
    root = Path(str(photos)).expanduser().resolve()
    frames = []
    for item in list_frames(photos, pattern):
        path = root / item["name"]
        try:
            image = _preview(path)
        except Exception as exc:                     # noqa: BLE001 - named
            frames.append({**item, "excluded": f"unreadable: {exc}"})
            continue
        width, height = image.size
        scale = min(1.0, float(detect_edge) / max(width, height))
        probe = image if scale >= 1.0 else image.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.BILINEAR)
        grey = np.asarray(probe.convert("L"), dtype=np.float32)
        # A disc touching the frame edge is clipped, and a clipped disc
        # does not fail loudly -- it fits a wrong circle with a straight
        # face. The bright mask against the border is the honest test,
        # made before any fit is believed.
        top = float(grey.max())
        bright = grey >= BRIGHT_FRACTION * top if top > 0 else grey > 1e9
        if (bright[:2].any() or bright[-2:].any()
                or bright[:, :2].any() or bright[:, -2:].any()):
            frames.append({**item, "excluded": (
                "the sun touches the frame edge; a clipped disc fits a "
                "wrong circle rather than none")})
            continue
        fitted = fit_limb(grey)
        if fitted is None:
            frames.append({**item, "excluded": "no sun found to fit"})
            continue
        frames.append({
            **item, "width": width, "height": height,
            "cx": fitted["cx"] / scale, "cy": fitted["cy"] / scale,
            "r": fitted["r"] / scale, "inliers": fitted["inliers"]})
    fitted_frames = [f for f in frames if "excluded" not in f]
    if fitted_frames:
        median_d = float(np.median([2 * f["r"] for f in fitted_frames]))
        for frame in fitted_frames:
            if abs(2 * frame["r"] - median_d) > SCALE_TOLERANCE * median_d:
                frame["excluded"] = (
                    f"sun diameter {2 * frame['r']:.0f}px sits outside "
                    f"{SCALE_TOLERANCE:.0%} of the sequence's "
                    f"{median_d:.0f}px -- another lens, or a failed fit")
    kept = [f for f in frames if "excluded" not in f]
    # A fit that leaps away from its neighbours' track is a detection
    # failure, not a hand movement: hands drift, they do not teleport.
    # Only meaningful once the sequence is long enough to have a track.
    if len(kept) >= 7:
        track_x = _smoothed([f["cx"] for f in kept])
        track_y = _smoothed([f["cy"] for f in kept])
        for frame, at_x, at_y in zip(kept, track_x, track_y):
            leap = float(np.hypot(frame["cx"] - at_x, frame["cy"] - at_y))
            if leap > 1.5 * frame["r"]:
                frame["excluded"] = (
                    f"the fit sits {leap:.0f}px from its neighbours' "
                    "track -- a detection failure, not a hand movement")
        kept = [f for f in frames if "excluded" not in f]
    return json.dumps({
        "format": FORMAT, "photos": str(root), "pattern": str(pattern),
        "frames": frames,
        "kept": len(kept),
        "excluded": [
            {"name": f["name"], "why": f["excluded"]}
            for f in frames if "excluded" in f],
    }, sort_keys=True)


def crop_plan(survey_text: str) -> str:
    """The photographer's crop, on the fitted centres, both bugs fixed.

    Diameter is the diameter; the right edge carries all three of
    MinL + D + MinR, so the crop can never cut into the disc; and the
    margins come off the smoothed track. Minimum margins make it the
    largest common crop: every frame contains its box entirely.
    """
    told = json.loads(survey_text)
    kept = [f for f in told["frames"] if "excluded" not in f]
    if not kept:
        return json.dumps({**told, "error": "no frames survived the survey"})
    diameter = max(2 * f["r"] for f in kept)
    min_t = min(f["cy"] - f["r"] for f in kept)
    min_l = min(f["cx"] - f["r"] for f in kept)
    min_r = min(f["width"] - (f["cx"] + f["r"]) for f in kept)
    min_b = min(f["height"] - (f["cy"] + f["r"]) for f in kept)
    if min(min_t, min_l, min_r, min_b) < 0:
        worst = min(kept, key=lambda f: min(
            f["cy"] - f["r"], f["cx"] - f["r"],
            f["width"] - f["cx"] - f["r"], f["height"] - f["cy"] - f["r"]))
        return json.dumps({**told, "error": (
            "the sun touches the frame edge in at least one photograph "
            f"(worst: {worst['name']}); exclude it and survey again")})
    # Constant size; even, because video codecs insist.
    crop_w = int(min_l + diameter + min_r) // 2 * 2
    crop_h = int(min_t + diameter + min_b) // 2 * 2
    boxes = []
    for frame in kept:
        left = int(round(frame["cx"] - frame["r"] - min_l))
        top = int(round(frame["cy"] - frame["r"] - min_t))
        left = max(0, min(left, frame["width"] - crop_w))
        top = max(0, min(top, frame["height"] - crop_h))
        boxes.append({"name": frame["name"], "taken": frame["taken"],
                      "left": left, "top": top})
    return json.dumps({
        "format": FORMAT, "photos": told["photos"],
        "diameter": diameter, "width": crop_w, "height": crop_h,
        "margins": {"top": min_t, "left": min_l,
                    "right": min_r, "bottom": min_b},
        "boxes": boxes,
        "excluded": told["excluded"],
    }, sort_keys=True)


def plan_valid(plan_text: str) -> bool:
    plan = json.loads(plan_text)
    if plan.get("error") or plan.get("format") != FORMAT:
        return False
    boxes = plan.get("boxes") or []
    return (bool(boxes)
            and plan.get("width", 0) >= 16 and plan.get("height", 0) >= 16
            and plan["width"] % 2 == 0 and plan["height"] % 2 == 0)


def plan_note(plan_text: str) -> str:
    """One line a queue or a certificate can carry."""
    plan = json.loads(plan_text)
    if plan.get("error"):
        return str(plan["error"])
    return (f"{len(plan.get('boxes') or [])} frames at "
            f"{plan.get('width')}x{plan.get('height')}, "
            f"{len(plan.get('excluded') or [])} set aside")


# --- rendering the sequence ---------------------------------------------------

def _shift_operations(recipe_path: str) -> list[dict[str, Any]]:
    """The colour shift's global operations, read from any house recipe."""
    if not str(recipe_path).strip():
        return []
    value = json.loads(Path(recipe_path).expanduser().read_text(
        encoding="utf-8"))
    recipe = value.get("recipe", value)
    operations = [item for item in recipe.get("operations", [])
                  if isinstance(item, dict)
                  and not str(item.get("op", "")).startswith("mask.")]
    return active(operations)


def render_sequence(photos: str, plan_text: str, directory: str,
                    recipe: str = "") -> str:
    """Crop every planned frame, shift its colour, and file the sequence.

    Frames are numbered in capture order, so the directory is the
    timelapse; the report carries the one ffmpeg line that turns it
    into video, because the next tool should not need guessing at.
    """
    plan = json.loads(plan_text)
    root = Path(str(photos)).expanduser().resolve()
    where = Path(str(directory)).expanduser().resolve()
    where.mkdir(parents=True, exist_ok=True)
    operations = _shift_operations(recipe)
    written = []
    for index, box in enumerate(plan["boxes"], start=1):
        image = _preview(root / box["name"])
        crop = image.crop((
            box["left"], box["top"],
            box["left"] + plan["width"], box["top"] + plan["height"]))
        if operations:
            # The engine's own invertible pair: display to scene-linear
            # and back, same primaries. Its sibling decode-to-rec2020
            # is NOT _encoded's inverse -- pairing them brightened every
            # frame by the gamut matrix, measured before it shipped.
            linear = _decoded(np.asarray(crop, dtype=np.float32) / 255.0)
            linear = _apply_global(linear, operations)
            shown = np.clip(_encoded(linear), 0.0, 1.0)
            crop = Image.fromarray((shown * 255).astype(np.uint8))
        name = f"{index:04d}.jpg"
        crop.save(where / name, quality=93)
        written.append({"frame": name, "source": box["name"],
                        "taken": box["taken"]})
    report = {
        "format": "darkimiya-timelapse-v1",
        "directory": str(where),
        "frames": written,
        "width": plan["width"], "height": plan["height"],
        "excluded": plan.get("excluded", []),
        "colour_shift": str(recipe) or "none",
        "assemble": (
            f"ffmpeg -framerate 12 -i {where}/%04d.jpg -c:v libx264 "
            f"-pix_fmt yuv420p {where}/timelapse.mp4"),
        "created_at": datetime.now(UTC).isoformat(),
    }
    return json.dumps(report, indent=2, sort_keys=True)


def sequence_complete(report_text: str, plan_text: str) -> bool:
    """Every planned frame is on disk at the plan's exact size."""
    report = json.loads(report_text)
    plan = json.loads(plan_text)
    frames = report.get("frames") or []
    if len(frames) != len(plan.get("boxes") or []):
        return False
    directory = Path(report.get("directory") or ".")
    for item in frames[:: max(1, len(frames) // 12)]:
        path = directory / item["frame"]
        if not path.is_file():
            return False
        with Image.open(path) as opened:
            if opened.size != (plan["width"], plan["height"]):
                return False
    return True
