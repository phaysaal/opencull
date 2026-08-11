"""The personal style profile: which one Darkimiya uses, and what it says.

A style profile is read from photographs the photographer already edited to
their own satisfaction, and it describes how they edit rather than what they
shoot. It belongs to the person, not to any one folder, so which profile is
in use is settled once here rather than per project.

What it is for is narrow and worth stating: the suggestion pass reads it and
produces a fourth treatment, "personal", alongside standard, signature and
creative. Nothing else consults it, and a project that used one records
which one in its own manifest, so a render's provenance stays legible after
the selection here changes.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STYLE_FORMAT = "opencull-personal-style-profile-v1"
SELECTION_FORMAT = "darkimiya-style-selection-v1"

# The profile's own vocabulary, in the order a person reads it.
STYLE_FIELDS = (
    "visual_signature",
    "tonal_preferences",
    "contrast_preferences",
    "color_preferences",
    "white_balance_preferences",
    "saturation_preferences",
    "highlight_shadow_preferences",
    "texture_detail_preferences",
    "composition_preferences",
    "subject_and_skin_preferences",
    "scene_adaptation",
    "preferred_adjustments",
    "avoid_or_guardrails",
    "professional_refinement",
)

FIELD_LABELS = {
    "visual_signature": "Visual signature",
    "tonal_preferences": "Tone",
    "contrast_preferences": "Contrast",
    "color_preferences": "Colour",
    "white_balance_preferences": "White balance",
    "saturation_preferences": "Saturation",
    "highlight_shadow_preferences": "Highlights and shadows",
    "texture_detail_preferences": "Texture and detail",
    "composition_preferences": "Composition",
    "subject_and_skin_preferences": "Subject and skin",
    "scene_adaptation": "How it adapts by scene",
    "preferred_adjustments": "Adjustments you reach for",
    "avoid_or_guardrails": "What you avoid",
    "professional_refinement": "Professional refinement",
}


class StyleProfileError(ValueError):
    """A style profile is unreadable or is not one."""


def _atomic_json(path: Path, value: dict) -> None:
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
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def read_profile(path: Path) -> dict[str, Any]:
    """Load one profile, refusing anything that is not one."""
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StyleProfileError(f"cannot read style profile: {exc}") from exc
    if not isinstance(value, dict) or value.get("format") != STYLE_FORMAT:
        raise StyleProfileError("that file is not a Darkimiya style profile")
    return value


def profile_summary(value: dict[str, Any]) -> dict[str, Any]:
    """The parts of a profile worth putting on screen."""
    profile = value.get("profile")
    profile = profile if isinstance(profile, dict) else value
    fields = [
        (FIELD_LABELS.get(field, field.replace("_", " ").capitalize()),
         str(profile.get(field, "")).strip())
        for field in STYLE_FIELDS
        if str(profile.get(field, "")).strip()
    ]
    confidence = profile.get("confidence")
    return {
        "name": str(profile.get("profile_name") or "Personal style"),
        "confidence": (
            float(confidence) if isinstance(confidence, (int, float)) else None),
        "fields": fields,
        "examples": len(value.get("examples", []) or []),
        "revision": value.get("revision"),
        "updated_at": str(value.get("updated_at") or value.get("created_at") or ""),
    }


class StyleProfileStore:
    """Which personal style profile is in use, and which ones exist."""

    def __init__(self, path: Path, results: Path):
        self.path = Path(path).expanduser().resolve()
        self.results = Path(results).expanduser().resolve()

    # --- selection ------------------------------------------------------

    def selected(self) -> str:
        if not self.path.is_file():
            return ""
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(value, dict):
            return ""
        chosen = str(value.get("selected") or "")
        # A profile that has been moved or deleted is not in use, whatever
        # the selection file still says.
        return chosen if chosen and Path(chosen).is_file() else ""

    def _stored(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def select(self, path: str | Path) -> dict[str, Any]:
        resolved = Path(str(path)).expanduser().resolve()
        read_profile(resolved)
        _atomic_json(self.path, {
            "format": SELECTION_FORMAT, "selected": str(resolved),
            "names": self._stored().get("names") or {}})
        return self.public()

    def forget(self) -> dict[str, Any]:
        """Stop using a profile without deleting it."""
        _atomic_json(self.path, {
            "format": SELECTION_FORMAT, "selected": "",
            "names": self._stored().get("names") or {}})
        return self.public()

    # --- naming ---------------------------------------------------------

    def set_name(self, path: str | Path, name: str) -> dict[str, Any]:
        """Call a profile what the photographer calls it.

        The name lives beside the selection rather than inside the profile,
        because the profile file is a kernel's output and is not edited.
        Several profiles otherwise all display as "Personal style", which
        makes choosing between them a guess.
        """
        resolved = str(Path(str(path)).expanduser().resolve())
        read_profile(Path(resolved))
        stored = self._stored()
        names = dict(stored.get("names") or {})
        cleaned = str(name).strip()[:80]
        if cleaned:
            names[resolved] = cleaned
        else:
            names.pop(resolved, None)
        _atomic_json(self.path, {
            "format": SELECTION_FORMAT,
            "selected": str(stored.get("selected") or ""),
            "names": names,
        })
        return self.public()

    def name_for(self, path: str | Path) -> str:
        names = self._stored().get("names") or {}
        return str(names.get(str(Path(str(path)).expanduser().resolve()), ""))

    # --- discovery ------------------------------------------------------

    @staticmethod
    def examples_of(value: dict[str, Any]) -> list[str]:
        """The photographs a profile was read from."""
        return [
            str(item.get("path"))
            for item in value.get("examples") or []
            if isinstance(item, dict) and item.get("path")
        ]

    @staticmethod
    def group_name(examples: list[str]) -> str:
        """What to call a group of photographs before anyone names it.

        The folder they came from, because that is the name the
        photographer already gave that body of work. Where they were
        gathered from several folders, say so rather than picking one.
        """
        folders = {Path(item).parent for item in examples if item}
        if not folders:
            return ""
        if len(folders) == 1:
            return next(iter(folders)).name
        return f"{len(folders)} folders"

    def available(self) -> list[dict[str, Any]]:
        """Every style profile Darkimiya has produced, newest first."""
        found: list[dict[str, Any]] = []
        if not self.results.is_dir():
            return found
        for path in sorted(self.results.glob("*.json")):
            try:
                value = read_profile(path)
            except StyleProfileError:
                continue
            summary = profile_summary(value)
            given = self.name_for(path)
            examples = self.examples_of(value)
            group = self.group_name(examples)
            found.append({
                **summary,
                # The photographer's own name wins. Failing that the
                # one the model wrote, which says what the look is; the
                # folder says which work it came from, and rides along
                # as the subtitle rather than replacing the title.
                **({"name": given} if given else {}),
                "given_name": given,
                "model_name": summary.get("name", ""),
                # `examples` is the count the summary already reports;
                # the paths are their own field rather than shadowing it.
                "example_paths": examples,
                "group": group,
                "path": str(path),
                "modified": path.stat().st_mtime_ns})
        found.sort(key=lambda item: item["modified"], reverse=True)
        return found

    def retire(self, path: str | Path) -> Path:
        """Take a profile off the shelf without destroying it.

        A render made under a profile can only be explained while that
        profile still exists, so removing one moves it aside rather than
        deleting it. It stops being offered, its name is forgotten, and
        it stops being the profile in use if it was.
        """
        resolved = Path(str(path)).expanduser().resolve()
        read_profile(resolved)
        retired = self.results / "Retired profiles"
        retired.mkdir(parents=True, exist_ok=True)
        destination = retired / resolved.name
        number = 2
        while destination.exists():
            destination = retired / f"{resolved.stem}-{number}.json"
            number += 1
        resolved.rename(destination)
        stored = self._stored()
        names = dict(stored.get("names") or {})
        names.pop(str(resolved), None)
        _atomic_json(self.path, {
            "format": SELECTION_FORMAT,
            "selected": ("" if stored.get("selected") == str(resolved)
                         else stored.get("selected") or ""),
            "names": names})
        return destination

    def public(self) -> dict[str, Any]:
        chosen = self.selected()
        summary: dict[str, Any] | None = None
        if chosen:
            try:
                summary = profile_summary(read_profile(Path(chosen)))
                given = self.name_for(chosen)
                if given:
                    summary["name"] = given
            except StyleProfileError:
                summary = None
        return {
            "format": SELECTION_FORMAT,
            "selected": chosen,
            "profile": summary,
            "available": self.available(),
        }

    def suggested_output(self, stamp: str) -> Path:
        """Where a new profile should be written.

        Named for when it was made, because a profile is a revision of an
        opinion rather than a fact: keeping the old one lets a render made
        under it still be explained.
        """
        return self.results / f"personal-style-{stamp}.json"
