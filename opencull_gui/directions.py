"""Edit directions: what the suggestion pass produced, and where the next go.

Directions are versioned against the shortlist review that asked for them.
A file is named for the review revision it answers and carries the hash of
the shortlist it read, so a set of directions belonging to an older
selection is recognised as such rather than quietly reused.

Regeneration is targeted: asking again for one rejected frame writes a
report containing only that frame. Reading therefore overlays the newest
answer for each photograph onto older complete reports, so redoing one
frame never hides the rest of the batch.

This lived inside the HTTP server. Nothing about it is HTTP; it reads
files and compares hashes, and the native window needs the same answers.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

DIRECTIONS_FORMAT = "opencull-edit-directions-v1"

# A personal treatment only exists when the style profile produced all of it.
PERSONAL_FIELDS = (
    "personal_title", "personal_intent", "personal_instructions",
    "personal_recipe",
)


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
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(
            path.expanduser().resolve().read_bytes()).hexdigest()
    except OSError:
        return ""


class DirectionsIndex:
    """The edit directions belonging to one shortlist and its review."""

    def __init__(self, shortlist, reviews, recipes: Path,
                 style_profile: Callable[[], str] | None = None):
        self.shortlist = shortlist
        self.reviews = reviews
        self.recipes = Path(recipes)
        self.style_profile = style_profile or (lambda: "")

    # --- naming ---------------------------------------------------------

    def next_path(self, revision: int) -> Path:
        """Where a new set of directions for this review revision would go.

        The name carries the revision so that directions and the selection
        they answer stay legible as a pair. A second run against the same
        revision takes a -v2 suffix rather than replacing the first.
        """
        base = self.recipes / (
            f"{self.shortlist.path.stem}.edit-directions-r{revision}.json")
        candidate = base
        version = 2
        while candidate.exists():
            candidate = base.with_name(
                f"{base.stem}-v{version}{base.suffix}")
            version += 1
        return candidate

    def reports(self, shortlist_sha: str) -> list[tuple[Path, dict]]:
        """Every directions file that belongs to this shortlist, newest first."""
        prefix = f"{self.shortlist.path.stem}.edit-directions-r"
        roots = [self.recipes]
        if self.shortlist.path.parent != self.recipes:
            roots.append(self.shortlist.path.parent)
        paths = list(dict.fromkeys(
            path
            for root in roots
            for path in root.glob(f"{prefix}*.json")
            if not path.name.endswith(".checkpoint.json")
        ))
        paths.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        found: list[tuple[Path, dict]] = []
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(value, dict)
                and value.get("format") == DIRECTIONS_FORMAT
                and value.get("shortlist_sha256") == shortlist_sha
            ):
                found.append((path, value))
        return found

    # --- reading --------------------------------------------------------

    def payload(self) -> dict:
        review_state = self.reviews.public_state()
        revision = review_state["revision"]
        next_path = self.next_path(revision)
        valid = self.reports(_sha256_of(self.shortlist.path))

        exact = [
            (path, value) for path, value in valid
            if value.get("review_revision") == revision
        ]
        selected = {
            photo for photo, entry in review_state.get("entries", {}).items()
            if isinstance(entry, dict) and entry.get("interesting") is True
        }
        active_style = str(self.style_profile() or "")
        active_style_sha = _sha256_of(Path(active_style)) if active_style else ""
        ordered = exact + [item for item in valid if item not in exact]
        if not ordered:
            return {
                "available": False,
                "default_path": str(next_path),
                "processed_photos": [],
                "processed_count": 0,
                "regeneration_required": False,
                "selection_revision": revision,
                "selected_photos": sorted(selected),
            }

        _path, value = ordered[0]
        # A targeted regeneration report contains one photograph. Overlay the
        # newest exact-revision entry for each photo onto older complete
        # reports so regenerating one rejection never hides the rest.
        entries_by_photo: dict[str, dict] = {}
        personal_style_photos: set[str] = set()
        for _, report in ordered:
            report_style_sha = str(report.get("style_profile_sha256", ""))
            for entry in report.get("entries", []):
                photo = entry.get("photo") if isinstance(entry, dict) else None
                if (
                    isinstance(photo, str)
                    and photo not in entries_by_photo
                    and (not selected or photo in selected)
                ):
                    entries_by_photo[photo] = entry
                    has_personal = all(
                        isinstance(entry.get(field), str)
                        and bool(entry[field].strip())
                        for field in PERSONAL_FIELDS
                    )
                    # Reports predating style provenance remain usable when
                    # they visibly contain a complete personal treatment.
                    style_matches = (
                        not active_style_sha
                        or report_style_sha == active_style_sha
                        or not report_style_sha
                    )
                    if has_personal and style_matches:
                        personal_style_photos.add(photo)
        available_entries = [
            entries_by_photo[photo] for photo in sorted(entries_by_photo)
        ]
        available_photos = {entry.get("photo") for entry in available_entries}
        complete_photos = available_photos
        if active_style:
            complete_photos = available_photos & personal_style_photos
        processed_photos = sorted(
            photo for photo in (selected & complete_photos)
            if isinstance(photo, str)
        )
        directions = {
            **value,
            "review_revision": revision,
            "entries": available_entries,
            "assembled_from": [str(candidate) for candidate, _ in ordered],
        }
        encoded = json.dumps(
            directions, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")
        effective_path = self.recipes / (
            f"{self.shortlist.path.stem}.effective-edit-directions-"
            f"{hashlib.sha256(encoded).hexdigest()[:16]}.json")
        if not effective_path.is_file():
            _atomic_json(effective_path, directions)
        missing = sorted(selected - complete_photos)
        return {
            "available": True,
            "path": str(effective_path),
            "directions": directions,
            "default_path": str(next_path),
            "partial": not exact or bool(missing),
            "missing_photos": missing,
            "processed_photos": processed_photos,
            "processed_count": len(processed_photos),
            "regeneration_required": bool(processed_photos),
            "selection_revision": revision,
            "selected_photos": sorted(selected),
        }
