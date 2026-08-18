"""Kernels for the dust-removal program: find it, judge it, keep it.

The thinking lives in opencull_gui.dust; these are the words the
Kimiya program speaks. Detection is deterministic -- dust is the thing
that cannot move, so agreement across frames is the adjudicator and no
model is asked for anything. A run over a whole shoot costs preview
reads and arithmetic.

Standalone use, without the program:
  python dust_kernel.py scan /path/to/photos [pattern]
"""

from __future__ import annotations

import json
import sys
from typing import Any

from opencull_gui import dust


def survey_dust(photos: str, pattern: str = "",
                edge: float = 768) -> str:
    """The folder's dust map, voted by its own frames.

    A JSON string, because the program's last act writes it to disk
    verbatim -- file.overwrite writes exactly the text it is handed,
    so the text must already be the file.
    """
    return json.dumps(dust.survey(photos, pattern, edge=int(edge)),
                      indent=2)


def _parsed(report: Any) -> dict[str, Any] | None:
    if isinstance(report, dict):
        return report
    if isinstance(report, str):
        try:
            value = json.loads(report)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None


def map_valid(told: Any) -> bool:
    """A map worth keeping: enough frames voted, every spot sane.

    An empty spot list is valid -- a clean sensor is a finding, and
    keeping it recorded stops the question being asked again. What is
    not valid is a map that could not vote: too few readable frames.
    """
    report = _parsed(told)
    if report is None:
        return False
    if report.get("format") != dust.DUST_FORMAT:
        return False
    if int(report.get("frames_read", 0)) < 3:
        return False
    spots = report.get("spots")
    if not isinstance(spots, list):
        return False
    for spot in spots:
        if not isinstance(spot, dict):
            return False
        try:
            x, y, r = (float(spot["x"]), float(spot["y"]),
                       float(spot["r"]))
        except (KeyError, TypeError, ValueError):
            return False
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < r <= 0.05):
            return False
    return True


def map_note(told: Any) -> str:
    report = _parsed(told) or {}
    spots = report.get("spots", [])
    if not spots:
        return (f"No dust found across {report.get('frames_read', 0)} "
                "frames -- the sensor reads clean, and that is now on "
                "record.")
    worst = spots[0]
    return (f"{len(spots)} dust spot(s) agreed on by "
            f"{report.get('frames_read', 0)} frames; the deepest sits at "
            f"{worst['x']:.2f}, {worst['y']:.2f} "
            f"(depth {worst['depth']:.2f}, seen {worst['seen']:.0%}). "
            "Every render of this folder now heals them first.")


def map_home(photos: str) -> str:
    """Where the folder's dust map lives, ready for the act to write."""
    path = dust.map_path(photos)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "scan":
        told = survey_dust(argv[1], argv[2] if len(argv) > 2 else "")
        if map_valid(told):
            dust.save_map(json.loads(told))
            print(map_note(told))
            print(f"kept: {map_home(argv[1])}")
            return 0
        print(told)
        print("not kept: the map could not vote")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
