"""How far a control can be pushed before the photograph pays for it.

Every slider on the advanced fine-tune page is painted in three bands:
the safe range, where a professional would move without comment; the
artistic range, where the move is a statement and had better be meant;
and the rest, where the photograph is being damaged for nothing. The
photographer can still go anywhere the compiler's bounds allow -- the
bands are advice, not walls -- but the advice is visible at the exact
place the decision is made, which is the slider.

Two sources, one shape. A model that has looked at the photograph can
write a zones file beside its recipes saying, per control, where those
bands sit *for this frame* -- a night frame's safe exposure range is
not a beach frame's. Until such a file exists, the professional
defaults below apply: conservative, unit-aware, and deliberately
narrower than the compiler's bounds, because the compiler bounds what
is executable, not what is wise.

The file, when a model writes one:

    .darkimiya/Recipes/<stem>.control-zones.json
    {
      "format": "darkimiya-control-zones-v1",
      "photo": "DSC00703.ARW",
      "zones": {
        "tone.exposure": {"safe": [-0.8, 0.5], "artistic": [-2.5, 1.2]},
        ...
      }
    }

Safe must sit inside artistic; both are clamped to the compiler's
range. A control the file does not name keeps the default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recipe_compiler import RANGES

FORMAT = "darkimiya-control-zones-v1"

# The professional defaults: what a careful editor would call safe and
# what a deliberate one might still sign. Values in each unit's own
# terms. Where a control's damage is asymmetric -- exposure ruins
# highlights faster than shadows, dehaze invents halos long before
# negative dehaze fogs anything -- the bands are asymmetric too.
DEFAULTS: dict[str, dict[str, tuple[float, float]]] = {
    "tone.exposure": {"safe": (-1.0, 0.7), "artistic": (-2.5, 1.5)},
    "tone.brightness": {"safe": (-15.0, 15.0), "artistic": (-40.0, 40.0)},
    "tone.contrast": {"safe": (-15.0, 25.0), "artistic": (-40.0, 60.0)},
    "tone.highlight": {"safe": (-40.0, 15.0), "artistic": (-80.0, 40.0)},
    "tone.shadow": {"safe": (-15.0, 40.0), "artistic": (-40.0, 80.0)},
    "tone.white": {"safe": (-20.0, 15.0), "artistic": (-50.0, 40.0)},
    "tone.black": {"safe": (-15.0, 20.0), "artistic": (-40.0, 50.0)},
    "levels.black_input": {"safe": (0.0, 24.0), "artistic": (0.0, 64.0)},
    "levels.white_input": {"safe": (216.0, 255.0), "artistic": (160.0, 255.0)},
    "levels.midpoint": {"safe": (0.8, 1.3), "artistic": (0.5, 2.0)},
    "color.temperature": {"safe": (-600.0, 600.0), "artistic": (-2000.0, 2000.0)},
    "color.tint": {"safe": (-12.0, 12.0), "artistic": (-40.0, 40.0)},
    "color.saturation": {"safe": (-15.0, 15.0), "artistic": (-100.0, 45.0)},
    "detail.clarity": {"safe": (-10.0, 15.0), "artistic": (-30.0, 40.0)},
    "detail.structure": {"safe": (-10.0, 15.0), "artistic": (-25.0, 40.0)},
    "detail.dehaze": {"safe": (-5.0, 15.0), "artistic": (-20.0, 45.0)},
    "finish.vignette": {"safe": (-25.0, 10.0), "artistic": (-60.0, 30.0)},
    "color.neutralize": {"safe": (0.0, 100.0), "artistic": (0.0, 100.0)},
}


def _band(pair: Any, low: float, high: float) -> tuple[float, float] | None:
    if (not isinstance(pair, (list, tuple)) or len(pair) != 2
            or not all(isinstance(v, (int, float)) for v in pair)):
        return None
    a, b = sorted((float(pair[0]), float(pair[1])))
    return (max(low, a), min(high, b))


def zones_for(op: str, stated: dict[str, Any] | None = None) -> dict[str, Any]:
    """One control's bands, clamped inside the compiler's range.

    ``stated`` is the per-photo file's entry for this op, or None. Safe
    is forced inside artistic, because a safe move that the artistic
    band disowns is a contradiction no slider can paint.
    """
    if op not in RANGES:
        return {}
    low, high = RANGES[op][0], RANGES[op][1]
    told = stated if isinstance(stated, dict) else {}
    fallback = DEFAULTS.get(op, {})
    artistic = (_band(told.get("artistic"), low, high)
                or _band(fallback.get("artistic"), low, high)
                or (low, high))
    safe = (_band(told.get("safe"), low, high)
            or _band(fallback.get("safe"), low, high)
            or artistic)
    safe = (max(safe[0], artistic[0]), min(safe[1], artistic[1]))
    if safe[0] > safe[1]:
        safe = artistic
    return {"safe": safe, "artistic": artistic}


def zones_path(photos_root: Path, photo: str) -> Path:
    """Where a model's per-photo zones live, beside the recipes."""
    return (Path(photos_root) / ".darkimiya" / "Recipes"
            / f"{Path(photo).stem}.control-zones.json")


def load(photos_root: Path, photo: str) -> dict[str, dict[str, Any]]:
    """Every control's bands for one photograph: stated where a model
    has looked at this frame, defaults where it has not."""
    stated: dict[str, Any] = {}
    path = zones_path(photos_root, photo)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(value, dict) and value.get("format") == FORMAT
                and isinstance(value.get("zones"), dict)):
            stated = value["zones"]
    except (OSError, json.JSONDecodeError, ValueError):
        stated = {}
    return {op: zones_for(op, stated.get(op)) for op in RANGES}
