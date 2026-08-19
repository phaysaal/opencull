"""Approving the whole cull, and moving its rejects to the trash."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from PIL import Image  # noqa: E402

from opencull_gui.photos import PhotoStore  # noqa: E402
from opencull_gui.report import load_report  # noqa: E402
from opencull_gui.reviews import (  # noqa: E402
    ReviewStore,
    approve_remaining,
    default_review_path,
)

CLUSTERS = {
    "g1": ["A1.JPG", "A2.JPG"],
    "g2": ["B1.JPG"],
    "g3": ["C1.JPG", "C2.JPG"],
}
KEEP = {"g1": ["A1.JPG"], "g2": ["B1.JPG"], "g3": ["C1.JPG"]}


def build_context(root: Path):
    photos = root / "photos"
    photos.mkdir()
    for names in CLUSTERS.values():
        for name in names:
            Image.new("RGB", (60, 40), (40, 70, 90)).save(photos / name)
    report_path = root / "shoot-results.json"
    report_path.write_text(json.dumps({
        "format": "opencull-report-v2", "manifest_sha256": "x",
        "clusters": [{"cluster_id": key, "photos": names}
                     for key, names in CLUSTERS.items()],
        "keep": [{"cluster_id": key, "photos": KEEP[key],
                  "rationale": "sharpest", "confidence": 0.8, "warning": "",
                  "fallback": False, "photographic_assessment": []}
                 for key in CLUSTERS],
        "warnings": [], "adaptive_clustering": {"enabled": True},
        "notice": "read only",
    }), encoding="utf-8")
    report = load_report(report_path)
    reviews = ReviewStore(
        default_review_path(report_path), report, photos)
    return report, photos, reviews


class ApproveRemainingTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.report, self.photos, self.reviews = build_context(self.root)
        self.addCleanup(self._temporary.cleanup)

    def test_every_unreviewed_group_takes_the_proposal(self):
        written = approve_remaining(self.reviews)
        self.assertEqual(written, 3)
        state = self.reviews.public_state()
        for key in CLUSTERS:
            self.assertTrue(state["clusters"][key]["reviewed"])
            self.assertEqual(state["clusters"][key]["keepers"], KEEP[key])

    def test_a_decision_already_made_is_never_touched(self):
        self.reviews.update_cluster("g1", ["A2.JPG"], "mine", True, 0)
        written = approve_remaining(self.reviews)
        self.assertEqual(written, 2)
        entry = self.reviews.public_state()["clusters"]["g1"]
        self.assertEqual(entry["keepers"], ["A2.JPG"])
        self.assertEqual(entry["note"], "mine")

    def test_the_history_names_the_act(self):
        approve_remaining(self.reviews)
        actions = {event["action"]
                   for event in self.reviews.public_state()["history"]}
        self.assertEqual(actions, {"approve"})


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class ReviewPageWholeCullTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.report, self.photos_root, self.reviews = build_context(self.root)
        self.addCleanup(self._temporary.cleanup)

    def page(self):
        from opencull_qt.previews import PreviewLoader
        from opencull_qt.review import ReviewPage

        store = PhotoStore(self.photos_root, self.root / "cache")
        loader = PreviewLoader(store)
        page = ReviewPage(self.report, self.reviews, loader)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def test_approving_the_lot_asks_then_reviews_everything(self):
        page = self.page()
        with mock.patch.object(page, "confirm_approve", return_value=True):
            page.approve_the_lot()
        status = self.reviews.public_state()["status"]
        self.assertEqual(status["reviewed_clusters"], 3)
        self.assertIn("Approved the proposal for 3 groups",
                      page.status.text())

    def test_declining_approves_nothing(self):
        page = self.page()
        with mock.patch.object(page, "confirm_approve", return_value=False):
            page.approve_the_lot()
        self.assertEqual(
            self.reviews.public_state()["status"]["reviewed_clusters"], 0)

    def test_a_fully_reviewed_cull_is_not_asked_again(self):
        page = self.page()
        approve_remaining(self.reviews)
        with mock.patch.object(page, "confirm_approve") as confirm:
            page.approve_the_lot()
        confirm.assert_not_called()
        self.assertIn("already reviewed", page.status.text())

    def test_the_rejects_are_only_rejects_once_every_group_is_decided(self):
        page = self.page()
        with mock.patch.object(page, "confirm_trash") as confirm:
            page.trash_rejects()
        confirm.assert_not_called()
        self.assertIn("not reviewed yet", page.status.text())

    def test_declining_the_trash_moves_nothing(self):
        page = self.page()
        approve_remaining(self.reviews)
        with mock.patch.object(page, "confirm_trash", return_value=False):
            page.trash_rejects()
        for names in CLUSTERS.values():
            for name in names:
                self.assertTrue((self.photos_root / name).exists())

    def test_the_rejects_move_to_the_trash_and_the_keepers_stay(self):
        page = self.page()
        approve_remaining(self.reviews)
        finished: list[dict] = []
        page._trash_done.connect(finished.append)
        with mock.patch.object(page, "confirm_trash", return_value=True), \
                mock.patch.dict(os.environ, {
                    "XDG_DATA_HOME": str(self.root / "xdg")}):
            page.trash_rejects()
            deadline = time.monotonic() + 15
            while not finished and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.05)
        self.assertTrue(finished, "the trash operation never finished")
        journal = finished[0]
        # The hook fires before the status flag flips; the items are the
        # durable truth at that moment.
        self.assertEqual(
            journal["completed_files"], len(journal["items"]))
        # The unselected frames are gone from the folder; keepers stay.
        self.assertFalse((self.photos_root / "A2.JPG").exists())
        self.assertFalse((self.photos_root / "C2.JPG").exists())
        for kept in ("A1.JPG", "B1.JPG", "C1.JPG"):
            self.assertTrue((self.photos_root / kept).exists())
        # And every moved file still exists, at its journaled destination.
        for item in journal["items"]:
            self.assertTrue(Path(item["destination"]).exists())
        self.assertIn("moved to the trash", page.status.text())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class SceneReviewTests(ReviewPageWholeCullTests):
    """The review's groups, gathered into scenes."""

    def test_scenes_follow_the_shooting_order(self):
        page = self.page()
        scenes = page.scenes()
        # A1/A2, B1, C1/C2 are adjacent in sequence: one scene.
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["clusters"], ["g1", "g2", "g3"])

    def test_a_single_scene_grows_no_headers(self):
        from PySide6.QtCore import Qt

        page = self.page()
        rows = [
            page.clusters.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(page.clusters.count())]
        self.assertNotIn(None, rows)

    def test_accepting_a_scene_reviews_only_its_groups(self):
        page = self.page()
        page.accept_scene(["g1", "g2"])
        state = self.reviews.public_state()
        self.assertTrue(state["clusters"]["g1"]["reviewed"])
        self.assertTrue(state["clusters"]["g2"]["reviewed"])
        self.assertNotIn("g3", state["clusters"])
        self.assertIn("Accepted the proposal for 2 groups",
                      page.status.text())

    def test_accepting_a_scene_never_overrules_a_decision(self):
        page = self.page()
        self.reviews.update_cluster("g1", ["A2.JPG"], "mine", True, 0)
        page.accept_scene(["g1", "g2"])
        self.assertEqual(
            self.reviews.public_state()["clusters"]["g1"]["keepers"],
            ["A2.JPG"])


    def test_the_s_key_accepts_the_current_frames_scene(self):
        page = self.page()
        page.accept_current_scene()
        state = self.reviews.public_state()
        # One scene holds all three groups here, so S reviews them all.
        self.assertEqual(state["status"]["reviewed_clusters"], 3)
