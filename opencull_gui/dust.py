"""Sensor dust, found by the one thing dust cannot do: move.

A dust spot lives on the sensor, so it sits at exactly the same place
in every frame of a shoot while the world moves behind it. That makes
detection a statistics problem, not a vision problem: divide each
frame by its own local blur so lighting cancels, and a dust spot is a
small dip that is STILL THERE in the median across frames. A bird, a
branch, a person -- anything real -- moves or leaves, and the median
forgets it. No model is asked; consistency across frames is the
adjudicator, and it is free.

The finding is a dust map: a short list of spots in fractions of the
frame, saved beside the photographs under the house folder. Renders
wear it the way they wear a camera look -- laid under every recipe,
marked as the map's, never worn twice, and left behind by presets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

DUST_FORMAT = "darkimiya-dust-map-v1"
_MAP_FILE = "dust-map.json"

# What counts as dust: a dip this deep, present in this share of the
# frames, no bigger than this. Wider than that it is not dust, it is
# the photograph.
_DIP = 0.035           # median residual must sit this far below flat
_SEEN = 0.7            # fraction of frames the dip must appear in
_WIDEST = 0.02         # spot radius cap, fraction of the short edge
_MOST = 48             # spots a map may carry
_LEAST_FRAMES = 3      # fewer frames than this cannot vote


def _grey(image) -> np.ndarray:
    """One frame as float grey at working size."""
    return np.asarray(image.convert("L"), dtype=np.float32) / 255.0


def _box(plane: np.ndarray, radius: int) -> np.ndarray:
    from development_engine import _box_mean

    return _box_mean(plane, radius)


def residual(grey: np.ndarray, radius: int | None = None) -> np.ndarray:
    """The frame divided by its own neighbourhood: lighting cancels.

    Flat sky, dark forest, bright snow all come out near 1.0; what is
    left below 1.0 is smaller than the blur radius and darker than its
    surroundings -- which is what a dust shadow is.
    """
    height, width = grey.shape
    reach = radius if radius else max(6, min(height, width) // 40)
    smooth = _box(grey, reach)
    return grey / np.maximum(smooth, 1e-4)


def _blobs(mask: np.ndarray) -> list[tuple[float, float, float]]:
    """Connected dips as (cy, cx, radius) in pixels. Plain BFS: the
    mask is sparse -- dust is rare or the folder has bigger problems."""
    found = []
    remaining = {(int(y), int(x)) for y, x in np.argwhere(mask)}
    while remaining:
        seed = remaining.pop()
        crowd = [seed]
        queue = [seed]
        while queue:
            y, x = queue.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    near = (y + dy, x + dx)
                    if near in remaining:
                        remaining.discard(near)
                        crowd.append(near)
                        queue.append(near)
        ys = np.array([p[0] for p in crowd], np.float32)
        xs = np.array([p[1] for p in crowd], np.float32)
        radius = float(np.sqrt(len(crowd) / np.pi))
        found.append((float(ys.mean()), float(xs.mean()), radius))
    return found


def survey(photos: str | Path, pattern: str = "",
           edge: int = 768, most_frames: int = 40) -> dict[str, Any]:
    """The dust map of one folder, from its frames' own agreement."""
    from timelapse_kernel import _preview, default_run

    root = Path(str(photos)).expanduser().resolve()
    wanted = pattern or default_run(str(root))["pattern"]
    frames = sorted(root.glob(wanted))
    if len(frames) > most_frames:
        picked = np.linspace(0, len(frames) - 1, most_frames).astype(int)
        frames = [frames[i] for i in picked]
    residuals = []
    size = None
    for path in frames:
        try:
            image = _preview(path)
        except Exception:                    # noqa: BLE001 - unreadable frame
            continue
        image.thumbnail((edge, edge))
        if size is None:
            size = image.size
        elif image.size != size:
            image = image.resize(size)
        residuals.append(residual(_grey(image)))
    report: dict[str, Any] = {
        "format": DUST_FORMAT,
        "photos": str(root),
        "pattern": wanted,
        "frames_read": len(residuals),
        "spots": [],
    }
    if len(residuals) < _LEAST_FRAMES:
        report["note"] = (
            f"only {len(residuals)} readable frames -- dust detection "
            f"votes across frames and needs at least {_LEAST_FRAMES}")
        return report
    stack = np.stack(residuals)
    agreed = np.median(stack, axis=0)
    seen = (stack < 1.0 - _DIP * 0.6).mean(axis=0)
    dips = (agreed < 1.0 - _DIP) & (seen >= _SEEN)
    height, width = agreed.shape
    short = float(min(height, width))
    spots = []
    for cy, cx, radius in _blobs(dips):
        if radius > _WIDEST * short:
            continue                          # too wide to be dust
        depth = float(1.0 - agreed[int(cy), int(cx)])
        spots.append({
            "x": round(cx / width, 5),
            "y": round(cy / height, 5),
            # Healed a little wider than measured: the dip's shoulders
            # are part of the shadow.
            "r": round(min(max(radius * 1.8, 2.0) / short,
                           _WIDEST * 1.8), 5),
            "depth": round(depth, 4),
            "seen": round(float(seen[int(cy), int(cx)]), 3),
        })
    spots.sort(key=lambda item: -item["depth"])
    report["spots"] = spots[:_MOST]
    return report


def heal_operation(report: dict[str, Any]) -> dict[str, Any] | None:
    """The map as the one operation the renderer needs, or None."""
    spots = [item for item in report.get("spots", [])
             if isinstance(item, dict)]
    if not spots:
        return None
    return {
        "op": "heal.spots", "unit": "spots", "mode": "absolute",
        "value": {"spots": [{"x": item["x"], "y": item["y"],
                             "r": item["r"]} for item in spots]},
        "source_instruction": f"dust map: {len(spots)} spots healed",
        "enabled": True,
        "dust_map": True,
    }


# --- where the map lives --------------------------------------------------

def map_path(photos: str | Path) -> Path:
    return Path(str(photos)).expanduser().resolve() / ".darkimiya" / _MAP_FILE


def save_map(report: dict[str, Any]) -> Path:
    destination = map_path(report["photos"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.writing")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def load_map(photos: str | Path) -> dict[str, Any] | None:
    path = map_path(photos)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("format") != DUST_FORMAT:
        return None
    return value


def wearing(operations: list[dict[str, Any]] | None) -> bool:
    return any(isinstance(item, dict) and item.get("dust_map")
               for item in operations or [])


def underneath(recipe: dict[str, Any],
               report: dict[str, Any] | None) -> dict[str, Any]:
    """The recipe with the folder's dust healed before anything else.

    First, even under a camera look: the spots are defects of the
    frame, and every other operation deserves to work on the frame as
    it should have been. A recipe already wearing a map is untouched.
    """
    if not report or wearing(recipe.get("operations")):
        return recipe
    operation = heal_operation(report)
    if operation is None:
        return recipe
    dressed = json.loads(json.dumps(recipe))
    dressed["operations"] = [operation] + list(
        dressed.get("operations") or [])
    return dressed


__all__ = [
    "DUST_FORMAT", "heal_operation", "load_map", "map_path", "residual",
    "save_map", "survey", "underneath", "wearing",
]
