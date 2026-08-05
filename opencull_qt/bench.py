"""Everything one shoot needs, built once and shared by its phases.

The phases of a shoot read the same things: the cull's report, the folder
of photographs, the assessment and the marks against it, the edit
directions, the develop workspace. Building those per page meant a
photographer moving from the assessment to development paid for the report
to be parsed twice and got two review stores over one file on disk -- two
objects that could disagree about what had been marked.

The bench builds each one at most once, on the first phase that asks for
it, and hands the same object to every phase after that. What a folder does
not have yet is not an error: a folder that has never been assessed simply
has no shortlist, and the phase that needed one is the phase that says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opencull_gui.directions import DirectionsIndex
from opencull_gui.photos import PhotoStore
from opencull_gui.project import (
    ensure_project_layout,
    load_or_create_folder_project,
    load_project,
)
from opencull_gui.report import load_report
from opencull_gui.reviews import ReviewStore, default_review_path
from opencull_gui.shortlist import load_shortlist
from opencull_gui.shortlist_reviews import (
    ShortlistReviewStore,
    default_shortlist_review_path,
)

from .develop import workspace_for


class BenchError(RuntimeError):
    """A phase was asked for that this folder has no input for."""


class Bench:
    """One shoot's inputs, built on demand.

    ``report_path`` is settled at construction rather than looked up per
    phase, because development falls back to a deterministic
    everything-included selection when there has been no cull, and every
    phase must agree about which selection is in play.
    """

    def __init__(self, project: dict, cache: Path, report_path: Path):
        self.project = project
        self.cache = Path(cache)
        self.report_path = Path(report_path)
        self.photos_root = Path(str(project.get("photos", ""))).expanduser()
        self._cached: dict[str, Any] = {}

    def _once(self, key: str, build):
        if key not in self._cached:
            self._cached[key] = build()
        return self._cached[key]

    # --- the cull ---------------------------------------------------------

    @property
    def report(self):
        return self._once("report", lambda: load_report(self.report_path))

    @property
    def photos(self) -> PhotoStore:
        return self._once(
            "photos",
            lambda: PhotoStore(self.photos_root, self.cache / "previews"))

    @property
    def reviews(self) -> ReviewStore:
        return self._once("reviews", lambda: ReviewStore(
            default_review_path(self.report_path), self.report,
            self.photos.root))

    # --- the project on disk ----------------------------------------------

    @property
    def layout(self) -> dict:
        return self._once(
            "layout", lambda: ensure_project_layout(self.photos.root))

    @property
    def project_path(self) -> Path:
        def build() -> Path:
            path, _manifest = load_or_create_folder_project(
                self.photos.root, self.report.path.stem)
            return path
        return self._once("project_path", build)

    # --- the assessment ---------------------------------------------------

    @property
    def shortlist_path(self) -> Path:
        recorded = str(self.project.get("shortlist") or "")
        if recorded:
            return Path(recorded).expanduser()
        return (self.layout["Reports"]
                / f"{self.report.path.stem}.professional-shortlist.json")

    @property
    def shortlist(self):
        def build():
            path = self.shortlist_path
            if not path.is_file():
                raise BenchError(
                    "This folder has not been assessed yet, so there is "
                    "nothing to mark.")
            return load_shortlist(path, self.report, self.photos.root)
        return self._once("shortlist", build)

    @property
    def shortlist_reviews(self) -> ShortlistReviewStore:
        return self._once("shortlist_reviews", lambda: ShortlistReviewStore(
            default_shortlist_review_path(self.shortlist_path),
            self.shortlist))

    @property
    def directions(self) -> DirectionsIndex:
        return self._once("directions", lambda: DirectionsIndex(
            self.shortlist, self.shortlist_reviews, self.layout["Recipes"],
            style_profile=lambda: str(
                load_project(self.project_path).get(
                    "active_style_profile") or "")))

    # --- development ------------------------------------------------------

    @property
    def workspace(self):
        return self._once(
            "workspace", lambda: workspace_for(self.report, self.photos.root))

    # --- what the phase bar needs to know ---------------------------------

    def culled(self) -> bool:
        """Whether a cull was actually run over this folder.

        Development registers a deterministic everything-included selection
        as the culling report when there has been no cull, so the presence
        of a report is not evidence that models ever looked at the frames.
        """
        if not self.project.get("report_available"):
            return False
        try:
            clustering = self.report.data.get("adaptive_clustering") or {}
        except Exception:                            # noqa: BLE001 - absent
            return False
        return str(clustering.get("mode") or "") != "manual-selection"

    def marked(self) -> int | None:
        """How many assessed frames are marked worth developing.

        ``None`` when there is no assessment or it cannot be read: nobody
        has looked, which is not the same as having looked and marked
        nothing, and only one of those should block asking for suggestions.
        """
        if not self.shortlist_path.is_file():
            return None
        try:
            state = self.shortlist_reviews.public_state()
        except Exception:                            # noqa: BLE001 - absent
            return None
        return sum(
            1 for entry in state.get("entries", {}).values()
            if isinstance(entry, dict) and entry.get("interesting") is True)
