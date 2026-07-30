"""Atomic human review state for an immutable professional shortlist."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .shortlist import ShortlistIndex, TIERS


SHORTLIST_REVIEW_FORMAT = "opencull-professional-review-v1"
MAX_HISTORY = 4000


class ShortlistReviewError(ValueError):
    """Professional-shortlist review state is invalid or stale."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_shortlist_review_path(shortlist_path: Path) -> Path:
    return shortlist_path.with_suffix(".review.json")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class ShortlistReviewStore:
    def __init__(self, path: Path, shortlist: ShortlistIndex):
        self.path = path.expanduser().resolve()
        self.shortlist = shortlist
        self._lock = threading.RLock()
        self.stale_reason = ""
        self._state = self._empty()
        self._load()

    def _empty(self) -> dict[str, Any]:
        timestamp = _now()
        return {
            "format": SHORTLIST_REVIEW_FORMAT,
            "shortlist_path": str(self.shortlist.path),
            "source_report_sha256": self.shortlist.data[
                "source_report_sha256"],
            "candidate_signature": self.shortlist.data.get(
                "candidate_signature", ""),
            "revision": 0,
            "created_at": timestamp,
            "updated_at": timestamp,
            "entries": {},
            "history": [],
        }

    def _validate_entry(self, photo: str, value: Any) -> dict[str, Any]:
        if photo not in self.shortlist.entry_by_photo:
            raise ShortlistReviewError(f"unknown shortlist photograph: {photo}")
        if not isinstance(value, dict):
            raise ShortlistReviewError("shortlist review entry must be an object")
        tier = str(value.get("tier", "")).strip().lower()
        note = value.get("note", "")
        edit_raw = value.get("edit_raw", False)
        reviewed = value.get("reviewed", False)
        if tier not in TIERS:
            raise ShortlistReviewError(f"unsupported quality tier: {tier!r}")
        if not isinstance(note, str) or len(note) > 5000:
            raise ShortlistReviewError("shortlist note is invalid")
        if not isinstance(edit_raw, bool) or not isinstance(reviewed, bool):
            raise ShortlistReviewError("review flags must be true or false")
        return {
            "tier": tier,
            "edit_raw": edit_raw,
            "reviewed": reviewed,
            "note": note,
            "updated_at": str(value.get("updated_at", _now())),
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.stale_reason = f"Professional review cannot be read: {exc}"
            return
        if (
            not isinstance(data, dict)
            or data.get("format") != SHORTLIST_REVIEW_FORMAT
            or data.get("source_report_sha256")
            != self.shortlist.data["source_report_sha256"]
            or data.get("candidate_signature")
            != self.shortlist.data.get("candidate_signature", "")
        ):
            self.stale_reason = (
                "Professional review belongs to a different shortlist version.")
            return
        try:
            entries = data.get("entries", {})
            if not isinstance(entries, dict):
                raise ShortlistReviewError("review entries must be an object")
            clean_entries = {
                photo: self._validate_entry(photo, value)
                for photo, value in entries.items()
            }
            history = data.get("history", [])
            if not isinstance(history, list):
                raise ShortlistReviewError("review history must be a list")
            self._state = {
                **self._empty(),
                **data,
                "revision": int(data.get("revision", 0)),
                "entries": clean_entries,
                "history": history[-MAX_HISTORY:],
            }
        except (ShortlistReviewError, TypeError, ValueError) as exc:
            self.stale_reason = str(exc)

    def _require_revision(self, revision: Any) -> None:
        if self.stale_reason:
            raise ShortlistReviewError(self.stale_reason)
        try:
            expected = int(revision)
        except (TypeError, ValueError) as exc:
            raise ShortlistReviewError("review revision is invalid") from exc
        if expected != self._state["revision"]:
            raise ShortlistReviewError(
                "Professional review changed in another tab; reload.")

    def _write(self) -> None:
        self._state["updated_at"] = _now()
        _atomic_json(self.path, self._state)

    def public_state(self) -> dict[str, Any]:
        with self._lock:
            reviewed = sum(
                entry.get("reviewed") is True
                for entry in self._state["entries"].values())
            edit_raw = sum(
                entry.get("reviewed") is True and entry.get("edit_raw") is True
                for entry in self._state["entries"].values())
            return {
                **deepcopy(self._state),
                "path": str(self.path),
                "stale": bool(self.stale_reason),
                "stale_reason": self.stale_reason,
                "summary": {
                    "reviewed": reviewed,
                    "total": len(self.shortlist.entries),
                    "edit_raw": edit_raw,
                },
            }

    def update(
        self,
        photo: str,
        tier: Any,
        edit_raw: Any,
        note: Any,
        reviewed: Any,
        revision: Any,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_revision(revision)
            before = deepcopy(self._state["entries"].get(photo))
            ai = self.shortlist.entry_by_photo.get(photo)
            if ai is None:
                raise ShortlistReviewError(
                    f"unknown shortlist photograph: {photo}")
            value = self._validate_entry(photo, {
                "tier": tier,
                "edit_raw": edit_raw,
                "note": note,
                "reviewed": reviewed,
                "updated_at": _now(),
            })
            self._state["entries"][photo] = value
            self._state["history"].append({
                "photo": photo,
                "before": before,
                "after": deepcopy(value),
                "at": _now(),
            })
            self._state["history"] = self._state["history"][-MAX_HISTORY:]
            self._state["revision"] += 1
            self._write()
            return self.public_state()

    def undo(self, revision: Any) -> dict[str, Any]:
        with self._lock:
            self._require_revision(revision)
            if not self._state["history"]:
                raise ShortlistReviewError("nothing to undo")
            event = self._state["history"].pop()
            if event.get("before") is None:
                self._state["entries"].pop(event["photo"], None)
            else:
                self._state["entries"][event["photo"]] = self._validate_entry(
                    event["photo"], event["before"])
            self._state["revision"] += 1
            self._write()
            return self.public_state()

    def export(self) -> dict[str, Any]:
        with self._lock:
            entries = []
            for ai in self.shortlist.entries:
                human = self._state["entries"].get(ai["photo"])
                reviewed = bool(human and human.get("reviewed"))
                entries.append({
                    **deepcopy(ai),
                    "effective_tier": (
                        human["tier"] if reviewed else ai["tier"]),
                    "edit_raw": (
                        human["edit_raw"] if reviewed
                        else ai["tier"] in {"exceptional", "strong"}),
                    "human_reviewed": reviewed,
                    "human_note": human["note"] if human else "",
                })
            return {
                "format": "opencull-reviewed-professional-shortlist-v1",
                "source_report_sha256": self.shortlist.data[
                    "source_report_sha256"],
                "source_shortlist": str(self.shortlist.path),
                "review_revision": self._state["revision"],
                "entries": entries,
                "edit_raw_files": sorted({
                    raw
                    for entry in entries if entry["edit_raw"]
                    for raw in entry.get("raw_files", [])
                }),
            }
