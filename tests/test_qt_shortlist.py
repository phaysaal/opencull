"""The native assessment page: read the evidence, mark what is worth developing."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui.photos import PhotoStore  # noqa: E402
from opencull_gui.report import load_report  # noqa: E402
from opencull_gui.shortlist import ASSESSMENT_FIELDS, load_shortlist  # noqa: E402
from opencull_gui.shortlist_reviews import (  # noqa: E402
    ShortlistReviewStore,
    default_shortlist_review_path,
)

NAMES = ["A.JPG", "B.JPG", "C.JPG"]
TIERS = ["exceptional", "promising", "reject"]


def build(root: Path) -> tuple[Path, Path, Path]:
    photos = root / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(NAMES):
        Image.new("RGB", (160, 120), (60 + index * 30, 100, 90)).save(
            photos / name)
    report_path = root / "shoot-results.json"
    report_path.write_text(json.dumps({
        "format": "opencull-report-v2", "manifest_sha256": "x",
        "clusters": [{"cluster_id": "group-0001", "photos": NAMES}],
        "keep": [{"cluster_id": "group-0001", "photos": [NAMES[0]],
                  "rationale": "sharpest", "confidence": 0.8, "warning": "",
                  "fallback": False, "photographic_assessment": []}],
        "warnings": [], "adaptive_clustering": {"enabled": False},
        "notice": "read only",
    }), encoding="utf-8")

    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    shortlist_path = root / "shoot.professional-shortlist.json"
    shortlist_path.write_text(json.dumps({
        "format": "opencull-professional-shortlist-v1",
        "source_report_sha256": digest,
        "candidate_policy": "effective",
        "candidate_signature": "sig",
        "entries": [
            {
                "rank": index + 1, "photo": name, "cluster_id": "group-0001",
                "tier": TIERS[index], "score": 90 - index * 25,
                "confidence": 0.9 - index * 0.1,
                "rationale": f"why {name} sits where it does",
                "raw_files": [],
                "assessment": {
                    field: f"{field} reading for {name}"
                    for field in ASSESSMENT_FIELDS},
                "warnings": [],
            }
            for index, name in enumerate(NAMES)
        ],
    }), encoding="utf-8")
    return report_path, photos, shortlist_path


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class ShortlistPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.report_path, self.photos_path, self.shortlist_path = build(root)
        self.report = load_report(self.report_path)
        self.photos = PhotoStore(self.photos_path, root / "cache")
        self.shortlist = load_shortlist(
            self.shortlist_path, self.report, self.photos.root)
        self.reviews = ShortlistReviewStore(
            default_shortlist_review_path(self.shortlist_path), self.shortlist)
        self.addCleanup(self._temporary.cleanup)

    def page(self):
        from opencull_qt.previews import PreviewLoader
        from opencull_qt.shortlist import ShortlistPage

        loader = PreviewLoader(self.photos)
        page = ShortlistPage(self.shortlist, self.reviews, loader)
        page.resize(1000, 700)
        page.show()
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def press(self, page, key):
        page.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))

    def test_frames_are_listed_in_the_order_the_models_ranked_them(self):
        page = self.page()
        self.assertEqual(
            [page.list.item(row).data(Qt.ItemDataRole.UserRole)
             for row in range(page.list.count())],
            NAMES)

    def test_the_whole_assessment_is_shown_not_just_the_score(self):
        page = self.page()
        shown = "\n".join(
            page.axes.itemAt(index).widget().findChildren(type(page.heading))[1].text()
            for index in range(page.axes.count())
            if page.axes.itemAt(index).widget() is not None)
        for field in ASSESSMENT_FIELDS:
            self.assertIn(f"{field} reading for A.JPG", shown)

    def test_the_verdict_carries_tier_score_and_confidence(self):
        page = self.page()
        self.assertIn("EXCEPTIONAL", page.verdict.text())
        self.assertIn("90/100", page.verdict.text())
        self.assertIn("90%", page.verdict.text())

    def test_nothing_is_marked_until_a_person_marks_it(self):
        # The assessment is a reading, not a decision. An AI tier of
        # "exceptional" must not select the frame on its own.
        page = self.page()
        self.assertFalse(page.interesting.isChecked())
        self.assertEqual(
            self.reviews.public_state()["summary"]["interesting"], 0)

    def test_marking_a_frame_records_it_and_survives_a_reload(self):
        page = self.page()
        page.set_interesting(True)
        state = self.reviews.public_state()
        self.assertTrue(state["entries"]["A.JPG"]["interesting"])
        self.assertEqual(state["summary"]["interesting"], 1)
        fresh = ShortlistReviewStore(
            default_shortlist_review_path(self.shortlist_path), self.shortlist)
        self.assertTrue(
            fresh.public_state()["entries"]["A.JPG"]["interesting"])

    def test_the_i_key_toggles_the_mark_both_ways(self):
        page = self.page()
        self.press(page, Qt.Key.Key_I)
        self.assertTrue(page.interesting.isChecked())
        self.press(page, Qt.Key.Key_I)
        self.assertFalse(page.interesting.isChecked())
        self.assertFalse(
            self.reviews.public_state()["entries"]["A.JPG"]["interesting"])

    def test_marking_keeps_the_tier_the_assessment_gave(self):
        # The tier is the models' word and this page does not argue with it;
        # what a person decides here is whether to develop the frame.
        page = self.page()
        page.set_interesting(True)
        self.assertEqual(
            self.reviews.public_state()["entries"]["A.JPG"]["tier"],
            "exceptional")

    def test_a_note_is_saved_with_the_frame(self):
        page = self.page()
        page.note.setPlainText("the hands are the point here")
        page.save()
        self.assertEqual(
            self.reviews.public_state()["entries"]["A.JPG"]["note"],
            "the hands are the point here")

    def test_moving_frames_shows_that_frame_s_own_marks(self):
        page = self.page()
        page.set_interesting(True)
        page.step(1)
        self.assertEqual(page.current, "B.JPG")
        self.assertFalse(page.interesting.isChecked())
        page.step(-1)
        self.assertTrue(page.interesting.isChecked())

    def test_the_count_of_what_will_be_sent_is_always_visible(self):
        page = self.page()
        self.assertIn("0 of 3", page.progress.text())
        self.assertIn("nothing to read", page.selected.text())
        page.set_interesting(True)
        self.assertIn("1 of 3", page.progress.text())
        self.assertIn("1 frame marked", page.selected.text())

    def test_the_list_marks_which_frames_are_chosen(self):
        page = self.page()
        page.set_interesting(True)
        self.assertIn("✓", page.list.item(0).text())
        self.assertNotIn("✓", page.list.item(1).text())

    def test_the_assessment_file_is_never_written_to(self):
        before = self.shortlist_path.read_bytes()
        page = self.page()
        page.set_interesting(True)
        page.note.setPlainText("a note")
        page.save()
        self.assertEqual(self.shortlist_path.read_bytes(), before)

    def test_escape_leaves_the_page(self):
        page = self.page()
        closed = []
        page.closed.connect(lambda: closed.append(True))
        self.press(page, Qt.Key.Key_Escape)
        self.assertEqual(closed, [True])

    def test_the_page_says_what_the_marks_are_for(self):
        page = self.page()
        page.set_interesting(True)
        self.assertIn("suggestion pass reads exactly these",
                      page.selected.text())


if __name__ == "__main__":
    unittest.main()
