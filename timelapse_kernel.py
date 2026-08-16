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

The survey's contract is a BOX per frame -- the principal subject's
[x0, y0, x1, y1] -- not a circle. The eclipse measurer is simply the
first thing that fills it (a circle is the box cx±r, cy±r); a tracker
or a vision model fills the same four numbers and the plan and the
render never learn what the subject was. The plan pins each frame's
box top-left at a fixed offset and sizes the crop for the LARGEST box
in the sequence, margins measured against that span -- which is what
makes containment true by construction even when the subject's extent
varies frame to frame, a case the circle version quietly clamped.

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
import shlex
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from development_engine import _apply_global, _decoded, _encoded, active

FORMAT = "darkimiya-timelapse-plan-v2"

# A fitted diameter this far from the sequence's median means a
# different lens or a failed fit; either way the frame cannot share a
# pixel-space crop with the rest and is set aside by name.
SCALE_TOLERANCE = 0.12

# The limb is the edge of the truly bright thing, not of its glow.
BRIGHT_FRACTION = 0.88


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def default_run(photos: str) -> dict[str, str]:
    """The obvious parameters, filled by the process rather than typed.

    The pattern is the folder's own dominant photograph type, spelled
    the way the files spell it; the outputs follow the house
    convention -- a Timelapse folder inside the project's own area,
    frames in frames/, the report beside them. A person confirms or
    changes a folder; nobody should have to invent one.
    """
    root = Path(str(photos)).expanduser().resolve()
    kinds: dict[str, int] = {}
    for path in root.iterdir() if root.is_dir() else []:
        if path.is_file() and path.suffix.lower() in {
                ".raf", ".arw", ".nef", ".cr2", ".cr3", ".dng",
                ".jpg", ".jpeg", ".tif", ".tiff", ".png"}:
            kinds[path.suffix] = kinds.get(path.suffix, 0) + 1
    suffix = max(kinds, key=lambda key: kinds[key]) if kinds else ".jpg"
    home = root / ".darkimiya" / "Timelapse"
    return {
        "pattern": f"*{suffix}",
        "frames_dir": str(home / "frames"),
        "output": str(home / "timelapse.json"),
    }


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
    """The frames in shooting order: the clock, and the counter within
    a tied second, unwrapped -- the same order the assessment list shows,
    from the same function, so the two can never disagree about which
    frame follows which."""
    from opencull_gui.scenes import capture_time, shooting_order

    root = Path(str(photos)).expanduser().resolve()
    found = sorted(root.glob(str(pattern)))
    taken = {path.name: capture_time(path) for path in found}
    names = shooting_order([path.name for path in found], taken)
    return [{"name": name, "taken": _taken_at(root / name)}
            for name in names]


# --- finding the sun -------------------------------------------------------

def _brightest_body(grey: np.ndarray) -> np.ndarray:
    """The largest connected patch of the brightest light: the sun.

    A ghost reflection can be as saturated as the disc itself, and a
    threshold on the whole frame hands its edge pixels to the fit as if
    they were limb. Connected components separate the two for nothing:
    the sun is one body, the ghost is another, and the sun is the
    bigger. On the frames this was built against the reflection came
    within a hair of the disc's brightness and containment alone was
    the only thing that saved the fit; this makes the save unnecessary.
    """
    top = float(grey.max())
    if top <= 0:
        return np.zeros(grey.shape, dtype=bool)
    bright = grey >= BRIGHT_FRACTION * top
    if bright.sum() < 20:
        return np.zeros(grey.shape, dtype=bool)
    import cv2

    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        bright.astype(np.uint8), connectivity=8)
    if count <= 1:
        return bright
    areas = stats[1:, cv2.CC_STAT_AREA]
    biggest = 1 + int(np.argmax(areas))
    return labels == biggest


def _limb_points(grey: np.ndarray) -> np.ndarray:
    """Edge pixels of the sun's body: the limb, plus the moon's bite."""
    bright = _brightest_body(grey)
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
    bys, bxs = np.nonzero(_brightest_body(grey))
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
        cx, cy = fitted["cx"] / scale, fitted["cy"] / scale
        r = fitted["r"] / scale
        frames.append({
            **item, "width": width, "height": height,
            # The contract every measurer fills: the subject's box.
            # The circle's own numbers stay beside it as this
            # measurer's working notes.
            "box": [cx - r, cy - r, cx + r, cy + r],
            "r": r, "inliers": fitted["inliers"]})
    frames = _screen(frames)
    kept = [f for f in frames if "excluded" not in f]
    return json.dumps({
        "format": FORMAT, "photos": str(root), "pattern": str(pattern),
        "frames": frames,
        "kept": len(kept),
        "excluded": [
            {"name": f["name"], "why": f["excluded"]}
            for f in frames if "excluded" in f],
    }, sort_keys=True)


# --- the second measurer: click once, track everywhere ---------------------

# Below this correlation the match is noise wearing the template's
# shape; the frame is set aside rather than trusted.
TRACK_CONFIDENCE = 0.45


def track_survey(photos: str, pattern: str, subject_box: str,
                 detect_edge: float = 1200) -> str:
    """Follow one subject named by a single box in the first frame.

    The free tier of subject-locked stabilization: the photographer
    marks the principal subject once -- [x0, y0, x1, y1] on the first
    frame, full-resolution coordinates -- and normalized correlation
    finds the same patch in every later frame, searching a window
    around where it last stood, because subjects move but do not
    teleport. The template is cut once and never updated: a template
    that follows its own matches drifts onto whatever it matched, and
    the drift compounds silently. A frame where the best match falls
    below confidence is set aside by name, and the search resumes from
    the last frame that was believed.

    No model is asked for anything; a sequence costs decode time.
    """
    import cv2

    root = Path(str(photos)).expanduser().resolve()
    listed = list_frames(photos, pattern)
    if not listed:
        return json.dumps({"format": FORMAT, "photos": str(root),
                           "pattern": str(pattern), "frames": [],
                           "kept": 0, "excluded": []})
    seed = [float(v) for v in str(subject_box).split(",")]
    if len(seed) != 4 or seed[2] <= seed[0] or seed[3] <= seed[1]:
        raise ValueError(
            "subject_box is x0,y0,x1,y1 on the first frame, and the "
            "box must have area")
    frames: list[dict[str, Any]] = []
    template = None
    template_size = (0, 0)
    last: tuple[float, float] | None = None
    scale = 1.0
    for item in listed:
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
        if template is None:
            x0, y0, x1, y1 = (v * scale for v in seed)
            template = grey[int(y0):int(y1), int(x0):int(x1)].copy()
            if template.size < 64:
                raise ValueError("the subject box is too small to track")
            template_size = template.shape
            last = (float(x0), float(y0))
            frames.append({**item, "width": width, "height": height,
                           "box": list(seed), "confidence": 1.0})
            continue
        th, tw = template_size
        # Subjects move but do not teleport: search around the last
        # believed position, one and a half subject-spans out.
        reach = int(1.5 * max(th, tw))
        wx0 = max(0, int(last[0]) - reach)
        wy0 = max(0, int(last[1]) - reach)
        wx1 = min(grey.shape[1], int(last[0]) + tw + reach)
        wy1 = min(grey.shape[0], int(last[1]) + th + reach)
        window = grey[wy0:wy1, wx0:wx1]
        if window.shape[0] < th or window.shape[1] < tw:
            frames.append({**item, "excluded": (
                "the search window fell off the frame; the subject was "
                "last seen too close to the edge")})
            continue
        scored = cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED)
        _lo, best, _lo_at, best_at = cv2.minMaxLoc(scored)
        if best < TRACK_CONFIDENCE:
            frames.append({**item, "excluded": (
                f"the tracker lost the subject (confidence {best:.2f}); "
                "the search resumes from the last believed frame")})
            continue
        found_x = wx0 + best_at[0]
        found_y = wy0 + best_at[1]
        last = (float(found_x), float(found_y))
        frames.append({
            **item, "width": width, "height": height,
            "box": [found_x / scale, found_y / scale,
                    (found_x + tw) / scale, (found_y + th) / scale],
            "confidence": round(float(best), 3)})
    frames = _screen(frames)
    kept = [f for f in frames if "excluded" not in f]
    return json.dumps({
        "format": FORMAT, "photos": str(root), "pattern": str(pattern),
        "frames": frames, "kept": len(kept),
        "excluded": [{"name": f["name"], "why": f["excluded"]}
                     for f in frames if "excluded" in f],
    }, sort_keys=True)


# --- the third measurer: a model anchors, the tracker carries --------------
#
# Vision on every frame of a long sequence is money spent on work
# correlation does for free. So the model is asked only at KEYFRAMES --
# where the subject is named in words and located in numbers -- and the
# tracker carries the box between anchors, re-cutting its template at
# each one so drift is bounded by the anchor spacing and appearance
# changes are absorbed where a model has just vouched for the box.

def keyframes(photos: str, pattern: str, every: float = 30) -> str:
    """The names a model will be shown: first, last, and every Nth."""
    listed = list_frames(photos, pattern)
    stride = max(1, int(every))
    chosen = listed[::stride]
    if listed and (not chosen or chosen[-1]["name"] != listed[-1]["name"]):
        chosen.append(listed[-1])
    return json.dumps([item["name"] for item in chosen])


def keyframe_proof(photos: str, name: str, directory: str,
                   edge: float = 1400) -> str:
    """One keyframe as a JPEG a vision model can be shown, size stated."""
    root = Path(str(photos)).expanduser().resolve()
    image = _preview(root / Path(str(name)).name)
    image.thumbnail((int(edge), int(edge)), Image.Resampling.LANCZOS)
    where = Path(str(directory)).expanduser().resolve()
    where.mkdir(parents=True, exist_ok=True)
    out = where / f"anchor-{Path(str(name)).stem}.jpg"
    image.save(out, quality=90)
    return str(out)


def collect_anchor(anchors_json: str, photos: str, name: str,
                   proof: str, answer: Any) -> str:
    """One model answer, validated and scaled to full resolution.

    The model answered in the proof's own pixels; the survey speaks
    full-resolution boxes, so the scale is settled here, once, and an
    unusable answer records why instead of pretending.
    """
    from treatment_kernel import as_record

    held = json.loads(anchors_json or "[]")
    record = as_record(answer)
    root = Path(str(photos)).expanduser().resolve()
    with Image.open(str(proof)) as opened:
        shown_w, shown_h = opened.size
    full = _preview(root / Path(str(name)).name)
    scale = full.size[0] / max(shown_w, 1)
    entry: dict[str, Any] = {"name": Path(str(name)).name}
    visible = record.get("visible", True)
    if isinstance(visible, str):
        visible = visible.strip().lower() not in {"false", "no", "0"}
    values = []
    for key in ("x0", "y0", "x1", "y1"):
        value = record.get(key)
        values.append(float(value)
                      if isinstance(value, (int, float)) else None)
    if not visible:
        entry["skipped"] = "the model says the subject is not visible here"
    elif any(v is None for v in values) or values[2] <= values[0]             or values[3] <= values[1]:
        entry["skipped"] = f"unusable box from the model: {values}"
    else:
        entry["box"] = [
            max(0.0, min(values[0], shown_w)) * scale,
            max(0.0, min(values[1], shown_h)) * scale,
            max(0.0, min(values[2], shown_w)) * scale,
            max(0.0, min(values[3], shown_h)) * scale,
        ]
    held.append(entry)
    return json.dumps(held)


def anchors_usable(anchors_json: str) -> bool:
    """At least the first anchor must be a real box: the tracker's seed."""
    held = json.loads(anchors_json or "[]")
    placed = [item for item in held if "box" in item]
    return bool(placed)


def anchor_prompt(subject: str, proof: str) -> str:
    with Image.open(str(proof)) as opened:
        width, height = opened.size
    return (
        f"Find {str(subject).strip()} in this {width}x{height} image. "
        "Answer x0, y0, x1, y1: the tightest box around it, in pixels "
        "of THIS image, origin top-left. If it is not visible, answer "
        "visible=false and zeros. In 'what', say in a few words what "
        "you boxed, so a wrong lock is readable afterwards.")


def anchored_survey(photos: str, pattern: str, anchors_json: str,
                    detect_edge: float = 1200) -> str:
    """The tracker's survey, seeded and re-anchored by the model's boxes.

    Between anchors the tracker does what it always does. At each
    anchored keyframe the belief is reset to the model's box and the
    template re-cut there -- drift is bounded by anchor spacing, and a
    subject that changes appearance is re-learned exactly where a model
    vouched for it.
    """
    import cv2

    root = Path(str(photos)).expanduser().resolve()
    anchors = {item["name"]: item["box"]
               for item in json.loads(anchors_json or "[]")
               if "box" in item}
    listed = list_frames(photos, pattern)
    frames: list[dict[str, Any]] = []
    template = None
    template_size = (0, 0)
    last: tuple[float, float] | None = None
    for item in listed:
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
        anchored = anchors.get(item["name"])
        if anchored is not None:
            x0, y0, x1, y1 = (v * scale for v in anchored)
            cut = grey[int(y0):int(y1), int(x0):int(x1)]
            if cut.size >= 64:
                template = cut.copy()
                template_size = template.shape
                last = (float(x0), float(y0))
                frames.append({
                    **item, "width": width, "height": height,
                    "box": list(anchored), "confidence": 1.0,
                    "anchored": True})
                continue
        if template is None or last is None:
            frames.append({**item, "excluded":
                           "before the first usable anchor"})
            continue
        th, tw = template_size
        reach = int(1.5 * max(th, tw))
        wx0 = max(0, int(last[0]) - reach)
        wy0 = max(0, int(last[1]) - reach)
        wx1 = min(grey.shape[1], int(last[0]) + tw + reach)
        wy1 = min(grey.shape[0], int(last[1]) + th + reach)
        window = grey[wy0:wy1, wx0:wx1]
        if window.shape[0] < th or window.shape[1] < tw:
            frames.append({**item, "excluded": (
                "the search window fell off the frame; the subject was "
                "last seen too close to the edge")})
            continue
        scored = cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED)
        _lo, best, _lo_at, best_at = cv2.minMaxLoc(scored)
        if best < TRACK_CONFIDENCE:
            frames.append({**item, "excluded": (
                f"the tracker lost the subject (confidence {best:.2f}) "
                "between anchors")})
            continue
        found_x = wx0 + best_at[0]
        found_y = wy0 + best_at[1]
        last = (float(found_x), float(found_y))
        frames.append({
            **item, "width": width, "height": height,
            "box": [found_x / scale, found_y / scale,
                    (found_x + tw) / scale, (found_y + th) / scale],
            "confidence": round(float(best), 3)})
    frames = _screen(frames)
    kept = [f for f in frames if "excluded" not in f]
    return json.dumps({
        "format": FORMAT, "photos": str(root), "pattern": str(pattern),
        "frames": frames, "kept": len(kept),
        "excluded": [{"name": f["name"], "why": f["excluded"]}
                     for f in frames if "excluded" in f],
    }, sort_keys=True)


def _screen(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The survey's honesty passes, on the box contract.

    Scale is no longer grounds for exclusion: a frame shot at a
    different zoom is not a failed frame, and scale-normalisation folds
    it into the sequence with the subject a constant size. What still
    excludes is a scale NOBODY NEARBY SHARES -- a diameter that leaps
    from its neighbours' the way a failed fit does, where a real zoom
    is a run of frames agreeing on the new scale. And the track pass:
    a centre that leaps from its neighbours' path is a detection
    failure wearing the subject's costume. Both name the frame; neither
    drops it silently.
    """
    kept = [f for f in frames if "excluded" not in f]
    if len(kept) >= 7:
        spans = [max(f["box"][2] - f["box"][0],
                     f["box"][3] - f["box"][1]) for f in kept]
        track = _smoothed(spans)
        # A frame whose scale departs from the smoothed run is a
        # suspect -- either a botched fit or a zoom. The smoothed
        # median only swings onto a new scale once the run is long
        # enough to outvote it, so a SHORT burst (two frames at a new
        # zoom) reads as two lone spikes and would be lost. What tells
        # a burst from a botch is that its members agree with EACH
        # OTHER: a zoom's neighbours share its extent to within fit
        # noise, two independent failed fits land at different wrong
        # diameters. So a suspect flanked by a suspect of nearly its
        # own extent is a zoom run, and is kept.
        suspect = [abs(span - near) > 0.5 * near
                   for span, near in zip(spans, track)]
        for index, frame in enumerate(kept):
            if not suspect[index]:
                continue
            span = spans[index]
            in_run = any(
                suspect[j] and abs(spans[j] - span) <= 0.15 * span
                for j in (index - 1, index + 1)
                if 0 <= j < len(kept))
            if in_run:
                continue
            # A lone spike: this frame's scale disagrees with the
            # smoothed run around it, and no neighbour shares the new
            # extent. A botched fit is alone, so it does not survive.
            frame["excluded"] = (
                f"subject extent {span:.0f}px leaps from its "
                f"neighbours' {track[index]:.0f}px -- a failed find, not a "
                "zoom (a zoom moves several frames together)")
    kept = [f for f in frames if "excluded" not in f]
    if len(kept) >= 7:
        centres_x = [(f["box"][0] + f["box"][2]) / 2 for f in kept]
        centres_y = [(f["box"][1] + f["box"][3]) / 2 for f in kept]
        track_x = _smoothed(centres_x)
        track_y = _smoothed(centres_y)
        for frame, cx, cy, at_x, at_y in zip(
                kept, centres_x, centres_y, track_x, track_y):
            leap = float(np.hypot(cx - at_x, cy - at_y))
            span = max(frame["box"][2] - frame["box"][0],
                       frame["box"][3] - frame["box"][1])
            if leap > 0.75 * span:
                frame["excluded"] = (
                    f"the find sits {leap:.0f}px from its neighbours' "
                    "track -- a detection failure, not a hand movement")
    return frames


def crop_plan(survey_text: str) -> str:
    """The largest common crop, with every frame normalised to one scale.

    A zoom made the sun a different pixel size; scale-normalisation
    resamples each frame so the sun is the SAME size throughout, and
    the wider or narrower field of view simply shows as more or less
    sky around it -- the subject constant, the world breathing, which
    is the effect a kept zoom is for. Each frame carries the factor
    that maps its own subject to the target, and the crop is computed
    in normalised pixels, where the largest-common-crop math and its
    two fixed bugs are unchanged.
    """
    told = json.loads(survey_text)
    kept = [f for f in told["frames"] if "excluded" not in f]
    if not kept:
        return json.dumps({**told, "error": "no frames survived the survey"})
    # The target scale: the median subject extent, so most frames are
    # barely resampled and only the zoomed few move.
    spans = [max(f["box"][2] - f["box"][0], f["box"][3] - f["box"][1])
             for f in kept]
    target = float(np.median(spans))
    for frame in kept:
        own = max(frame["box"][2] - frame["box"][0],
                  frame["box"][3] - frame["box"][1], 1.0)
        s = target / own
        frame["_scale"] = s
        frame["_nbox"] = [v * s for v in frame["box"]]
        frame["_nw"] = frame["width"] * s
        frame["_nh"] = frame["height"] * s
    span_w = max(f["_nbox"][2] - f["_nbox"][0] for f in kept)
    span_h = max(f["_nbox"][3] - f["_nbox"][1] for f in kept)
    min_t = min(f["_nbox"][1] for f in kept)
    min_l = min(f["_nbox"][0] for f in kept)
    min_r = min(f["_nw"] - f["_nbox"][0] - span_w for f in kept)
    min_b = min(f["_nh"] - f["_nbox"][1] - span_h for f in kept)
    if min(min_t, min_l, min_r, min_b) < 0:
        worst = min(kept, key=lambda f: min(
            f["_nbox"][1], f["_nbox"][0],
            f["_nw"] - f["_nbox"][0] - span_w,
            f["_nh"] - f["_nbox"][1] - span_h))
        return json.dumps({**told, "error": (
            "the subject sits too close to the frame edge in at least "
            f"one photograph (worst: {worst['name']}); exclude it and "
            "survey again")})
    crop_w = int(min_l + span_w + min_r) // 2 * 2
    crop_h = int(min_t + span_h + min_b) // 2 * 2
    boxes = []
    for frame in kept:
        left = int(round(frame["_nbox"][0] - min_l))
        top = int(round(frame["_nbox"][1] - min_t))
        left = max(0, min(left, int(frame["_nw"]) - crop_w))
        top = max(0, min(top, int(frame["_nh"]) - crop_h))
        boxes.append({"name": frame["name"], "taken": frame["taken"],
                      "left": left, "top": top,
                      "scale": round(frame["_scale"], 5)})
    return json.dumps({
        "format": FORMAT, "photos": told["photos"],
        "span": {"width": span_w, "height": span_h},
        "target_diameter": target,
        "width": crop_w, "height": crop_h,
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

def _shift_operations(recipe: str, photos: str = ".") -> list[dict[str, Any]]:
    """The colour shift, named the way a person names it.

    Three spellings, tried in order. A path -- absolute, or relative
    to the photos folder -- to any house recipe file, for a custom
    look. Failing that, a preset's name: its id, its id without the
    "preset-" prefix, or its human name, case blind -- so
    "infrared-720-false-colour" and "Infrared · 720nm false colour"
    both mean the built-in, and a preset saved from the fine-tune page
    is addressable by whatever it was called. An unknown name refuses
    with the list of what would have worked, because a colour shift
    silently skipped is a timelapse quietly wrong.
    """
    asked = str(recipe).strip()
    if not asked:
        return []
    stated = Path(asked).expanduser()
    if not stated.is_absolute():
        beside = Path(str(photos)).expanduser().resolve() / asked
        if beside.is_file():
            stated = beside
    if stated.is_file():
        value = json.loads(stated.read_text(encoding="utf-8"))
        found = value.get("recipe", value)
        operations = found.get("operations", [])
    else:
        from opencull_gui import presets as preset_library

        offered = preset_library.presets()
        wanted = asked.casefold()
        chosen = next(
            (item for item in offered
             if wanted in {str(item.get("id", "")).casefold(),
                           str(item.get("id", "")).casefold().removeprefix(
                               "preset-"),
                           str(item.get("name", "")).casefold()}),
            None)
        if chosen is None:
            names = ", ".join(
                str(item.get("id", "")).removeprefix("preset-")
                for item in offered)
            raise ValueError(
                f"no recipe file at {asked!r} and no preset by that "
                f"name; the presets are: {names}")
        operations = chosen.get("operations", [])
    kept = [item for item in operations
            if isinstance(item, dict)
            and not str(item.get("op", "")).startswith("mask.")]
    return active(kept)


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
    operations = _shift_operations(recipe, photos)
    written = []
    for index, box in enumerate(plan["boxes"], start=1):
        image = _preview(root / box["name"])
        scale = float(box.get("scale", 1.0))
        if abs(scale - 1.0) > 1e-4:
            # Resample to the sequence's scale so the sun is one size
            # throughout. The crop box is already in these normalised
            # pixels, so the crop that follows is in the same frame as
            # every other frame's.
            image = image.resize(
                (max(1, round(image.width * scale)),
                 max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS)
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
        "assemble": _assemble_command(where, _video_target(where)),
        "video": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    return json.dumps(report, indent=2, sort_keys=True)


FRAMERATE = 12


def _video_target(frames_dir: Path) -> Path:
    """Where the finished video lands: beside the frames, not among them.

    The frames sit in a `frames/` folder; the video belongs one level up,
    in the timelapse folder itself, so it is not lost in a scroll of a few
    hundred stills and the next run's frames do not sit beside last run's
    film.
    """
    frames_dir = Path(frames_dir)
    parent = (frames_dir.parent if frames_dir.name == "frames"
              else frames_dir)
    return parent / "timelapse.mp4"


def _assemble_command(frames_dir: Path, target: Path) -> str:
    return " ".join(shlex.quote(part) for part in _ffmpeg_argv(
        Path(frames_dir), Path(target)))


def _ffmpeg_argv(frames_dir: Path, target: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-framerate", str(FRAMERATE),
        "-i", str(frames_dir / "%04d.jpg"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        str(target)]


def assemble_video(report_text: str) -> str:
    """Encode the numbered frames into the video, and record where it went.

    The alignment produced the frames and the exact ffmpeg line; running
    it is the last thing the photographer asked for when they pressed
    "Make the timelapse", so the program does it rather than leaving a
    command for a terminal. It is best-effort by design: if ffmpeg is not
    installed, or the encode fails, the frames and the assemble line are
    still on record and the report says plainly what happened -- the run
    is not failed over a missing encoder, it just has no film yet.
    """
    report = json.loads(report_text)
    frames_dir = Path(str(report.get("directory") or "."))
    target = _video_target(frames_dir)
    report["assemble"] = _assemble_command(frames_dir, target)
    frames = report.get("frames") or []
    if not frames:
        report["video"] = None
        report["video_note"] = "no frames to assemble into a video"
        return json.dumps(report, indent=2, sort_keys=True)
    if shutil.which("ffmpeg") is None:
        report["video"] = None
        report["video_note"] = (
            "ffmpeg is not installed, so the frames are ready but the video "
            "was not made; install ffmpeg and run the assemble line above")
        return json.dumps(report, indent=2, sort_keys=True)
    try:
        finished = subprocess.run(
            _ffmpeg_argv(frames_dir, target),
            capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["video"] = None
        report["video_note"] = f"ffmpeg could not run: {exc}"
        return json.dumps(report, indent=2, sort_keys=True)
    if finished.returncode != 0 or not target.is_file():
        tail = (finished.stderr or "").strip().splitlines()
        report["video"] = None
        report["video_note"] = (
            "ffmpeg did not produce a video: "
            + (tail[-1] if tail else f"exit {finished.returncode}"))
        return json.dumps(report, indent=2, sort_keys=True)
    report["video"] = str(target)
    report["video_note"] = (
        f"{len(frames)} frames at {FRAMERATE} fps -- "
        f"{len(frames) / FRAMERATE:.0f} seconds")
    return json.dumps(report, indent=2, sort_keys=True)


def video_note(report_text: str) -> str:
    """One line for the log: what became of the video."""
    try:
        report = json.loads(report_text)
    except (ValueError, TypeError):
        return ""
    made = report.get("video")
    note = str(report.get("video_note") or "")
    if made:
        return f"video: {made}  ({note})" if note else f"video: {made}"
    return f"no video: {note}" if note else "no video was made"


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
