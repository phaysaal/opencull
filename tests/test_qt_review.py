"""The native review page: clusters, keepers, keyboard, and persistence."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
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
from opencull_gui.reviews import ReviewStore, default_review_path  # noqa: E402

NAMES = ["A.JPG", "B.JPG", "C.JPG", "D.JPG"]


def build_shoot(root: Path) -> tuple[Path, Path]:
    photos = root / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        Image.new("RGB", (120, 90), (70, 100, 90)).save(photos / name)
    report = root / "results.json"
    report.write_text(json.dumps({
        "format": "opencull-report-v2", "manifest_sha256": "x",
        "clusters": [
            {"cluster_id": "group-0001", "photos": NAMES[:2]},
            {"cluster_id": "group-0002", "photos": NAMES[2:]},
        ],
        "keep": [
            {"cluster_id": "group-0001", "photos": ["A.JPG"],
             "rationale": "A is sharper.", "confidence": 0.91, "warning": "",
             "photographic_assessment": {}, "fallback": False},
            {"cluster_id": "group-0002", "photos": ["C.JPG"],
             "rationale": "C has better timing.", "confidence": 0.8,
             "warning": "", "photographic_assessment": {}, "fallback": False},
        ],
        "warnings": [], "scan_errors": [], "adaptive_clustering": {},
        "notice": "Recommendations only.",
    }), encoding="utf-8")
    return report, photos


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class ReviewPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.report_path, self.photos_path = build_shoot(root)
        self.report = load_report(self.report_path)
        self.photos = PhotoStore(self.photos_path, root / "cache")
        self.reviews = ReviewStore(
            default_review_path(self.report_path), self.report, self.photos.root)
        self.addCleanup(self._temporary.cleanup)

    def page(self):
        from opencull_qt.previews import PreviewLoader
        from opencull_qt.review import ReviewPage

        loader = PreviewLoader(self.photos)
        page = ReviewPage(self.report, self.reviews, loader)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def press(self, page, key):
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        page.keyPressEvent(event)

    # --- rendering ------------------------------------------------------

    def test_every_cluster_is_listed(self):
        page = self.page()
        self.assertEqual(page.clusters.count(), 2)

    def test_the_first_cluster_opens_with_its_frames(self):
        page = self.page()
        self.assertEqual(page.current, "group-0001")
        self.assertEqual(sorted(page.frames), ["A.JPG", "B.JPG"])

    def test_the_curator_rationale_and_confidence_are_shown(self):
        page = self.page()
        self.assertIn("A is sharper", page.rationale.text())
        self.assertIn("91", page.rationale.text())

    def test_the_recommendation_starts_selected(self):
        page = self.page()
        self.assertTrue(page.frames["A.JPG"].kept)
        self.assertFalse(page.frames["B.JPG"].kept)

    def test_moving_to_another_cluster_replaces_the_frames(self):
        page = self.page()
        page.show_cluster("group-0002")
        self.assertEqual(sorted(page.frames), ["C.JPG", "D.JPG"])

    # --- decisions ------------------------------------------------------

    def test_toggling_a_frame_persists_it(self):
        page = self.page()
        page.toggle("B.JPG")
        stored = self.reviews.public_state()["clusters"]["group-0001"]
        self.assertEqual(sorted(stored["keepers"]), ["A.JPG", "B.JPG"])
        self.assertTrue(stored["reviewed"])

    def test_toggling_twice_returns_to_where_it_started(self):
        page = self.page()
        page.toggle("A.JPG")
        page.toggle("A.JPG")
        stored = self.reviews.public_state()["clusters"]["group-0001"]
        self.assertEqual(stored["keepers"], ["A.JPG"])

    def test_keepers_keep_the_order_the_cluster_lists_them_in(self):
        page = self.page()
        page.toggle("B.JPG")
        self.assertEqual(
            self.reviews.public_state()["clusters"]["group-0001"]["keepers"],
            ["A.JPG", "B.JPG"])

    def test_keep_none_rejects_the_whole_cluster(self):
        page = self.page()
        page.keep_none()
        stored = self.reviews.public_state()["clusters"]["group-0001"]
        self.assertEqual(stored["keepers"], [])
        self.assertTrue(stored["reviewed"])

    def test_accept_ai_restores_the_curator_selection(self):
        page = self.page()
        page.keep_none()
        page.accept_ai()
        self.assertEqual(
            self.reviews.public_state()["clusters"]["group-0001"]["keepers"],
            ["A.JPG"])

    def test_unreviewed_puts_the_cluster_back(self):
        page = self.page()
        page.toggle("B.JPG")
        page.mark_unreviewed()
        stored = self.reviews.public_state()["clusters"]["group-0001"]
        self.assertFalse(stored["reviewed"])

    def test_the_report_itself_is_never_written_to(self):
        before = self.report_path.read_bytes()
        page = self.page()
        page.toggle("B.JPG")
        page.keep_none()
        self.assertEqual(self.report_path.read_bytes(), before)

    def test_progress_counts_reviewed_clusters(self):
        page = self.page()
        page.toggle("B.JPG")
        self.assertIn("1 of 2", page.progress.text())

    # --- keyboard -------------------------------------------------------

    def test_number_keys_toggle_by_position(self):
        page = self.page()
        self.press(page, Qt.Key.Key_2)
        self.assertEqual(
            sorted(self.reviews.public_state()["clusters"]["group-0001"]["keepers"]),
            ["A.JPG", "B.JPG"])

    def test_a_number_beyond_the_cluster_does_nothing(self):
        # Nothing is recorded at all: an out-of-range key is not a decision.
        page = self.page()
        self.press(page, Qt.Key.Key_9)
        self.assertEqual(page.current_keepers(), ["A.JPG"])
        self.assertNotIn("group-0001", self.reviews.public_state()["clusters"])

    def test_n_keeps_none(self):
        page = self.page()
        self.press(page, Qt.Key.Key_N)
        self.assertEqual(
            self.reviews.public_state()["clusters"]["group-0001"]["keepers"], [])

    def test_a_accepts_the_recommendation(self):
        page = self.page()
        self.press(page, Qt.Key.Key_N)
        self.press(page, Qt.Key.Key_A)
        self.assertEqual(
            self.reviews.public_state()["clusters"]["group-0001"]["keepers"],
            ["A.JPG"])

    def test_u_returns_a_cluster_to_unreviewed(self):
        page = self.page()
        self.press(page, Qt.Key.Key_N)
        self.press(page, Qt.Key.Key_U)
        self.assertFalse(
            self.reviews.public_state()["clusters"]["group-0001"]["reviewed"])

    def test_arrows_move_between_clusters(self):
        page = self.page()
        self.press(page, Qt.Key.Key_Right)
        self.assertEqual(page.current, "group-0002")
        self.press(page, Qt.Key.Key_Left)
        self.assertEqual(page.current, "group-0001")

    def test_arrows_stop_at_the_ends(self):
        page = self.page()
        self.press(page, Qt.Key.Key_Left)
        self.assertEqual(page.current, "group-0001")
        self.press(page, Qt.Key.Key_Right)
        self.press(page, Qt.Key.Key_Right)
        self.assertEqual(page.current, "group-0002")

    def test_escape_asks_to_leave(self):
        page = self.page()
        seen = []
        page.closed.connect(lambda: seen.append(True))
        self.press(page, Qt.Key.Key_Escape)
        self.assertEqual(seen, [True])


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class PreviewLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        _report, photos = build_shoot(root)
        self.store = PhotoStore(photos, root / "cache")
        self.addCleanup(self._temporary.cleanup)

    @staticmethod
    def wait_for(seen: list, seconds: float = 5.0) -> None:
        """Give the pool real time; spinning processEvents alone does not."""
        deadline = time.monotonic() + seconds
        while not seen and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)

    def test_a_preview_is_decoded_and_then_remembered(self):
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.store)
        self.addCleanup(loader.shutdown)
        seen: list[str] = []
        loader.ready.connect(lambda name, size, pixmap: seen.append(name))

        self.assertIsNone(loader.request("A.JPG"))
        self.wait_for(seen)
        self.assertEqual(seen, ["A.JPG"])
        # Second ask is served from memory, with no further work.
        self.assertIsNotNone(loader.cached("A.JPG", "thumb"))
        self.assertIsNotNone(loader.request("A.JPG"))

    def test_abandoning_work_does_not_stop_later_requests(self):
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.store)
        self.addCleanup(loader.shutdown)
        loader.request("A.JPG")
        loader.abandon()
        seen: list[str] = []
        loader.ready.connect(lambda name, size, pixmap: seen.append(name))
        loader.request("B.JPG")
        self.wait_for(seen)
        self.assertEqual(seen, ["B.JPG"])

    def test_the_cache_does_not_grow_without_bound(self):
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.store, cache_size=2)
        self.addCleanup(loader.shutdown)
        for name in NAMES:
            seen: list[str] = []
            loader.ready.connect(lambda n, s, p, box=seen: box.append(n))
            loader.request(name)
            self.wait_for(seen)
        self.assertLessEqual(len(loader._cache), 2)

@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class IntentTests(unittest.TestCase):
    """Review and develop land on the same page; only the framing differs."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.report_path, self.photos_path = build_shoot(root)
        self.report = load_report(self.report_path)
        self.photos = PhotoStore(self.photos_path, root / "cache")
        self.reviews = ReviewStore(
            default_review_path(self.report_path), self.report, self.photos.root)
        self.addCleanup(self._temporary.cleanup)

    def page(self, intent):
        from opencull_qt.previews import PreviewLoader
        from opencull_qt.review import ReviewPage

        loader = PreviewLoader(self.photos)
        page = ReviewPage(self.report, self.reviews, loader, intent=intent)
        page.show()
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def test_reviewing_shows_no_caveat(self):
        self.assertFalse(self.page("review").pending.isVisible())

    def test_developing_says_the_controls_are_not_here_yet(self):
        page = self.page("develop")
        self.assertTrue(page.pending.isVisible())
        self.assertIn("not in this window yet", page.pending.text())

    def test_both_intents_show_the_same_frames(self):
        self.assertEqual(
            sorted(self.page("review").frames),
            sorted(self.page("develop").frames))

    def test_decisions_are_saved_from_either_intent(self):
        page = self.page("develop")
        page.toggle("B.JPG")
        self.assertIn(
            "B.JPG",
            self.reviews.public_state()["clusters"]["group-0001"]["keepers"])

if __name__ == "__main__":
    unittest.main()
