"""Where each photograph's fine tuning stands, remembered.

A shoot is tuned across many frames, and a hand moves between them:
two sliders here, a mask there, back to the first frame to compare.
Losing the settings at every switch makes the page a corridor of doors
that slam. This ledger keeps one profile per photograph -- the
treatment being tuned, the changes as they stand, the selected layer
-- and whether those changes have been exported yet, so the file list
can say honestly which frames carry work that has not left the room.

One JSON file beside the project's recipes: small, written whole,
readable by a person wondering what the page will restore.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

TUNING_FORMAT = "darkimiya-finetune-state-v1"


class TuningLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._states: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(value, dict) and value.get(
                "format") == TUNING_FORMAT and isinstance(
                value.get("photos"), dict):
            self._states = {
                str(name): dict(state)
                for name, state in value["photos"].items()
                if isinstance(state, dict)}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.writing")
        temporary.write_text(json.dumps(
            {"format": TUNING_FORMAT, "photos": self._states},
            indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def get(self, photo: str) -> dict[str, Any] | None:
        """The profile a photograph left behind, or None."""
        state = self._states.get(str(photo))
        if not state or not state.get("changes"):
            return None
        return dict(state)

    def save(self, photo: str, treatment: str,
             changes: dict[str, Any], layer: int) -> None:
        """What this photograph's tuning is right now.

        Unchanged tuning writes nothing: a zoom or a re-render is not
        an edit, and the ledger should not stamp it as one.
        """
        state = self._states.setdefault(str(photo), {})
        settled = json.loads(json.dumps(changes))
        if (state.get("treatment") == str(treatment)
                and state.get("changes") == settled
                and state.get("layer") == int(layer)):
            return
        state.update({
            "treatment": str(treatment),
            "changes": settled,
            "layer": int(layer),
            "edited_at": time.time(),
        })
        self._write()

    def settle(self, photo: str) -> None:
        """The photograph's changes are gone -- undone or reset.

        The export record stays: knowing a frame was exported once is
        still true after its pending edits are taken back.
        """
        state = self._states.get(str(photo))
        if state and state.get("changes"):
            state.pop("changes", None)
            state.pop("treatment", None)
            state.pop("layer", None)
            state.pop("edited_at", None)
            self._write()

    def mark_exported(self, photo: str) -> None:
        state = self._states.setdefault(str(photo), {})
        state["exported_at"] = time.time()
        self._write()

    def unexported(self, photo: str) -> bool:
        """Whether this frame carries edits that have not been exported."""
        state = self._states.get(str(photo))
        if not state or not state.get("changes"):
            return False
        exported = float(state.get("exported_at") or 0.0)
        return float(state.get("edited_at") or 0.0) > exported


__all__ = ["TUNING_FORMAT", "TuningLedger"]
