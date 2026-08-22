"""Kernels for the look-learning program: pair, fit, judge, keep.

The thinking lives in colour_look.py; these are the words the Kimiya
program speaks. Deterministic end to end: the teacher is the camera's
own JPEG, and no model is asked for anything.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import colour_look as cl


def learn_look(photos: str, pattern: str = "*.RAF",
               name: str = "Learned look",
               most_frames: float = 12) -> str:
    """A look learned from every RAW+JPEG pair the folder offers.

    A JSON string shaped exactly like a saved preset, because the
    program's last act writes it into the preset store verbatim and
    the strip reads it like any other.
    """
    root = Path(str(photos)).expanduser().resolve()
    raws = sorted(
        path for path in root.glob(pattern or "*.RAF")
        if path.suffix.casefold() in cl.RAW_SUFFIXES)
    paired = []
    for raw in raws:
        sibling = next(
            (raw.with_suffix(ending) for ending in
             (".JPG", ".jpg", ".JPEG", ".jpeg")
             if raw.with_suffix(ending).is_file()), None)
        if sibling is not None:
            paired.append((raw, sibling))
    if len(paired) > int(most_frames):
        stride = len(paired) // int(most_frames) + 1
        paired = paired[::stride]
    if not paired:
        return json.dumps({
            "error": f"no RAW+JPEG pairs under {root} matching "
                     f"{pattern!r} -- shoot RAW+JPEG with the "
                     "simulation set, and both land side by side"})
    pairs = [(cl._thumb_display(raw), cl._thumb_display(jpeg))
             for raw, jpeg in paired]
    try:
        operations, report = cl.fit_mimic(pairs)
    except cl.LookError as refused:
        return json.dumps({"error": str(refused)})
    report["pairs"] = [raw.name for raw, _jpeg in paired]
    title = " ".join(str(name).split())[:80] or "Learned look"
    digest = hashlib.sha256(json.dumps(
        operations, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return json.dumps({
        "format": "darkimiya-preset-v1",
        "id": f"saved-{_slug(title)}-{digest[:8]}",
        "name": title,
        "intent": "Learned from the camera's own JPEGs: the neutral "
                  "decode on one side, the rendering the camera made "
                  "of the same light on the other.",
        "origin": "saved",
        "operations": operations,
        "report": report,
    }, indent=2)


def _slug(title: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "look"


def look_valid(told: Any) -> bool:
    """A look worth keeping: it fitted, and its operations are whole."""
    try:
        value = json.loads(told) if isinstance(told, str) else told
    except ValueError:
        return False
    if not isinstance(value, dict) or value.get("error"):
        return False
    operations = value.get("operations")
    if not isinstance(operations, list) or not operations:
        return False
    return all(isinstance(item, dict) and item.get("op")
               for item in operations)


def look_note(told: Any) -> str:
    try:
        value = json.loads(told) if isinstance(told, str) else told
    except ValueError:
        return "the fit returned something unreadable"
    if value.get("error"):
        return str(value["error"])
    report = value.get("report", {})
    return (f"Learned “{value.get('name')}” from "
            f"{len(report.get('pairs', []))} pair(s), "
            f"{report.get('samples', 0)} colour samples; residual "
            f"{report.get('residual_before')} -> "
            f"{report.get('residual_matrix')}. It is now in the "
            "preset strip of every photograph.")


def receipt_home(told: str, output: str = "") -> str:
    """Where the run's receipt lands: the asked-for place, or the store.

    The queue wants to say where a result landed; the look itself
    always lands in the preset store. Given an output, the same JSON is
    written there too, a receipt the queue badge can reveal. Given
    none, the receipt IS the store file, written once.
    """
    asked = str(output or "").strip()
    if asked:
        home = Path(asked)
        home.parent.mkdir(parents=True, exist_ok=True)
        return str(home)
    return look_home(told)


def look_home(told: str) -> str:
    """Where this look lives in the preset store, ready to be written."""
    from opencull_gui.presets import presets_dir

    value = json.loads(told)
    folder = presets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = str(value.get("id", "saved-look")).removeprefix("saved-")
    return str(folder / f"{stem}.json")
