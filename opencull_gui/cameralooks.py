"""Per-camera default looks: the colour a camera starts with.

The last move of the Capture One study. Their deepest habit is not a
tool but a default: every camera model arrives wearing a look its
colour team built for it, underneath everything else, before anyone
touches a slider. This store is that habit for this house.

A camera look is a small set of operations -- typically a matrix and a
color.warp lattice, fitted from a chart or authored as a preference --
assigned to a camera model by name. When a photograph from that camera
is prepared for rendering, the look's operations are laid under the
recipe's own, whatever the recipe is: baseline, preset, treatment or a
hand-tuned version. The look belongs to the camera, so it is marked as
the camera's, and what is kept as a preset or carried to another frame
leaves it behind -- exactly as a crop is left behind. Assign a look
deliberately: it colours everything that camera shoots.

Looks live outside every project, like presets, because a camera is
the photographer's and not the shoot's.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .appdirs import support_dir

LOOK_FORMAT = "darkimiya-camera-look-v1"
LOOKS_ENVIRONMENT = "DARKIMIYA_CAMERA_LOOKS"


class CameraLookError(ValueError):
    """A camera look cannot be kept or read."""


def looks_dir() -> Path:
    """Where the camera looks live, unless a test or a shared drive says."""
    named = os.environ.get(LOOKS_ENVIRONMENT, "").strip()
    if named:
        return Path(named).expanduser()
    return support_dir() / "CameraLooks"


def slug(model: str) -> str:
    """One camera model, as a filename: 'FUJIFILM X-T5' -> 'fujifilm-x-t5'."""
    settled = re.sub(r"[^a-z0-9]+", "-", str(model).casefold()).strip("-")
    return settled or "unknown-camera"


def assign(model: str, operations: list[dict[str, Any]],
           note: str = "", root: Path | None = None) -> dict[str, Any]:
    """Give one camera its look. Assigning again replaces it whole."""
    name = " ".join(str(model).split())
    if not name:
        raise CameraLookError("a camera look needs the camera's name")
    marked = []
    for item in operations:
        if not isinstance(item, dict):
            continue
        held = json.loads(json.dumps(item))
        held["camera_look"] = True
        marked.append(held)
    if not marked:
        raise CameraLookError("a camera look needs at least one operation")
    value = {
        "format": LOOK_FORMAT,
        "camera": name,
        "note": str(note).strip(),
        "assigned_at": datetime.now(UTC).isoformat(),
        "operations": marked,
    }
    folder = Path(root) if root is not None else looks_dir()
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{slug(name)}.json"
    temporary = destination.with_suffix(".json.writing")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return value


def look_for(model: str | None,
             root: Path | None = None) -> dict[str, Any] | None:
    """The look one camera wears, or None. Reads fresh: looks are small."""
    if not model or not str(model).strip():
        return None
    folder = Path(root) if root is not None else looks_dir()
    path = folder / f"{slug(model)}.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("format") != LOOK_FORMAT:
        return None
    if not isinstance(value.get("operations"), list):
        return None
    return value


def forget(model: str, root: Path | None = None) -> bool:
    """Take a camera's look away. True if there was one."""
    folder = Path(root) if root is not None else looks_dir()
    path = folder / f"{slug(model)}.json"
    try:
        path.unlink()
        return True
    except OSError:
        return False


def cameras(root: Path | None = None) -> list[dict[str, Any]]:
    """Every camera that wears a look, with when it was dressed."""
    folder = Path(root) if root is not None else looks_dir()
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("format") == LOOK_FORMAT:
            found.append({"camera": str(value.get("camera", path.stem)),
                          "note": str(value.get("note", "")),
                          "assigned_at": str(value.get("assigned_at", ""))})
    return found


def wearing(operations: list[dict[str, Any]] | None) -> bool:
    """Whether a recipe already carries a camera's look."""
    return any(isinstance(item, dict) and item.get("camera_look")
               for item in operations or [])


def underneath(recipe: dict[str, Any],
               look: dict[str, Any] | None) -> dict[str, Any]:
    """The recipe with the camera's look laid under its own operations.

    Under, not over: the look is what the camera's colour IS before any
    treatment speaks, the way a profile sits under a develop. A recipe
    already wearing a look -- one round-tripped through fine tuning and
    kept -- is returned untouched, so a look is never worn twice.
    """
    if not look or wearing(recipe.get("operations")):
        return recipe
    dressed = json.loads(json.dumps(recipe))
    dressed["operations"] = (
        json.loads(json.dumps(look["operations"]))
        + list(dressed.get("operations") or []))
    return dressed


__all__ = [
    "LOOKS_ENVIRONMENT", "LOOK_FORMAT", "CameraLookError", "assign",
    "cameras", "forget", "look_for", "looks_dir", "slug", "underneath",
    "wearing",
]
