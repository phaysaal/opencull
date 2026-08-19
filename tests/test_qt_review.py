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
    from PySide6.QtWidgets import QApplication, QLabel
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
        root = Path(self._temporary.name).resolve()
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
        # The page lands on the group overview; these tests exercise the
        # cluster view, so enter the first group the way a click would.
        if page.cluster_ids:
            page.show_cluster(page.cluster_ids[0])
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def overview(self):
        from opencull_qt.previews import PreviewLoader
        from opencull_qt.review import ReviewPage

        loader = PreviewLoader(self.photos)
        page = ReviewPage(self.report, self.reviews, loader)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def test_few_frames_grow_into_a_wide_window(self):
        page = self.page()
        page.resize(1700, 900)
        page.show()
        QApplication.processEvents()
        page._relayout()
        frame = page.frames["A.JPG"]
        self.assertGreater(frame.image.width(), 400)

    def test_frames_never_grow_past_their_preview_source(self):
        from opencull_qt.review import THUMB_MAX

        page = self.page()
        page.resize(3400, 1200)
        page.show()
        QApplication.processEvents()
        page._relayout()
        self.assertLessEqual(page.frames["A.JPG"].image.width(), THUMB_MAX)

    def test_the_review_lands_on_the_group_overview(self):
        page = self.overview()
        self.assertEqual(page.views.currentIndex(), 0)
        self.assertEqual(
            sorted(page._cards), ["group-0001", "group-0002"])
        card = page._cards["group-0001"]
        self.assertEqual(card.photos, ["A.JPG", "B.JPG"])
        self.assertIn("1/2 kept", card.findChildren(QLabel)[-1].text())

    def test_choosing_a_card_opens_its_group(self):
        page = self.overview()
        page._cards["group-0002"].chosen.emit("group-0002")
        self.assertEqual(page.views.currentIndex(), 1)
        self.assertEqual(page.heading.text(), "group-0002")

    def test_the_back_button_returns_with_fresh_counts(self):
        page = self.page()
        page.keep_none()
        page.back_button.click()
        self.assertEqual(page.views.currentIndex(), 0)
        card = page._cards["group-0001"]
        self.assertIn("0/2 kept", card.findChildren(QLabel)[-1].text())

    def test_keyboard_decisions_wait_for_a_group(self):
        page = self.overview()
        self.press(page, Qt.Key.Key_N)
        self.assertNotIn(
            "group-0001", self.reviews.public_state()["clusters"])

    def test_the_overview_deals_full_width_without_a_manual_resize(self):
        """The columns follow the viewport's own layout, not a stale read."""
        import json as json_module

        from PySide6.QtWidgets import QStackedWidget

        from opencull_qt.previews import PreviewLoader
        from opencull_qt.review import ReviewPage

        shoot = Path(tempfile.mkdtemp())
        photos = shoot / "photos"
        photos.mkdir()
        names = [f"P{n:02d}.JPG" for n in range(8)]
        for name in names:
            Image.new("RGB", (80, 60), (60, 90, 120)).save(photos / name)
        report_path = shoot / "results.json"
        report_path.write_text(json_module.dumps({
            "format": "opencull-report-v2", "manifest_sha256": "x",
            "clusters": [
                {"cluster_id": f"group-{n:04d}", "photos": [names[n]]}
                for n in range(8)],
            "keep": [
                {"cluster_id": f"group-{n:04d}", "photos": [names[n]],
                 "rationale": "solo", "confidence": 1.0, "warning": "",
                 "photographic_assessment": {}, "fallback": False}
                for n in range(8)],
            "warnings": [], "scan_errors": [], "adaptive_clustering": {},
            "notice": "x"}))
        report = load_report(report_path)
        store = PhotoStore(photos, shoot / "cache")
        reviews = ReviewStore(
            default_review_path(report_path), report, store.root)
        loader = PreviewLoader(store)
        self.addCleanup(loader.shutdown)
        stack = QStackedWidget()
        page = ReviewPage(report, reviews, loader)
        stack.addWidget(page)
        self.addCleanup(stack.deleteLater)
        stack.resize(1500, 700)
        stack.show()
        QApplication.processEvents()
        QApplication.processEvents()
        columns = page._overview_state[0]
        self.assertGreaterEqual(columns, 5)

    def test_the_geometry_maths_cover_narrow_wide_and_many(self):
        from opencull_qt.review import THUMB, THUMB_MAX, ReviewPage

        # Exactly two base tiles wide: both fit, at the base size.
        self.assertEqual(
            ReviewPage.frame_geometry(446, 2, 14), (2, THUMB))
        # Too narrow for two: one column, and the singleton grows.
        self.assertEqual(
            ReviewPage.frame_geometry(430, 2, 14), (1, 414))
        # Wide: two frames grow, capped at the preview's own resolution.
        self.assertEqual(
            ReviewPage.frame_geometry(1700, 2, 14), (2, THUMB_MAX))
        # Many frames wrap into the columns that honestly fit.
        columns, thumb = ReviewPage.frame_geometry(1000, 9, 14)
        self.assertEqual(columns, 4)
        self.assertGreaterEqual(thumb, THUMB)

    def test_previews_are_rendered_for_the_screens_real_pixels(self):
        from PySide6.QtGui import QPixmap

        from opencull_qt.previews import scaled

        source = QPixmap(1200, 900)
        dense = scaled(source, 400, 400, 2.0)
        self.assertEqual(dense.devicePixelRatio(), 2.0)
        self.assertEqual(dense.width(), 800)
        plain = scaled(source, 400, 400)
        self.assertEqual(plain.devicePixelRatio(), 1.0)
        self.assertLessEqual(plain.width(), 400)

    def test_a_screen_change_redraws_every_frame(self):
        page = self.page()
        drawn = []
        for frame in page.frames.values():
            frame.rerender = lambda f=frame: drawn.append(f.name)
        page._screen_changed(None)
        self.assertEqual(sorted(drawn), sorted(page.frames.keys()))

    def press(self, page, key):
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        page.keyPressEvent(event)

    # --- rendering ------------------------------------------------------

    def test_every_cluster_is_listed(self):
        from PySide6.QtCore import Qt

        page = self.page()
        cluster_rows = [
            page.clusters.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(page.clusters.count())
            if page.clusters.item(row).data(Qt.ItemDataRole.UserRole)]
        self.assertEqual(len(cluster_rows), 2)
        # Two scenes apart in the shooting order, so each gets its header.
        self.assertEqual(page.clusters.count() - len(cluster_rows), 2)

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

    def test_a_kept_none_survives_approval_and_returning(self):
        """Reviewed-to-none must never be resurrected as the AI proposal."""
        page = self.page()
        page.keep_none()
        self.press(page, Qt.Key.Key_Return)
        self.assertEqual(page.current, "group-0002")
        page.show_cluster("group-0001")
        self.assertEqual(page.current_keepers(), [])
        # Approving the displayed state again must keep the none.
        self.press(page, Qt.Key.Key_Return)
        state = self.reviews.public_state()["clusters"]["group-0001"]
        self.assertEqual(state["keepers"], [])
        self.assertTrue(state["reviewed"])

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

    def test_number_keys_focus_and_zoom_without_deciding(self):
        page = self.page()
        self.press(page, Qt.Key.Key_2)
        self.assertEqual(page.views.currentIndex(), 2)
        self.assertEqual(page.zoom.name, "B.JPG")
        self.assertEqual(page.focus_name(), "B.JPG")
        # Looking is not deciding.
        self.assertNotIn("group-0001", self.reviews.public_state()["clusters"])

    def test_escape_leaves_the_zoom_and_keeps_the_focus(self):
        page = self.page()
        self.press(page, Qt.Key.Key_2)
        self.press(page, Qt.Key.Key_Escape)
        self.assertEqual(page.views.currentIndex(), 1)
        self.assertEqual(page.focus_name(), "B.JPG")

    def test_left_and_right_move_the_focus_within_the_group(self):
        page = self.page()
        self.assertEqual(page.focus_name(), "A.JPG")
        self.press(page, Qt.Key.Key_Right)
        self.assertEqual(page.focus_name(), "B.JPG")
        self.assertEqual(page.current, "group-0001")
        self.press(page, Qt.Key.Key_Right)
        self.assertEqual(page.focus_name(), "B.JPG")
        self.press(page, Qt.Key.Key_Left)
        self.assertEqual(page.focus_name(), "A.JPG")

    def test_space_toggles_the_focused_frame(self):
        page = self.page()
        self.press(page, Qt.Key.Key_Right)
        self.press(page, Qt.Key.Key_Space)
        self.assertEqual(
            sorted(self.reviews.public_state()
                   ["clusters"]["group-0001"]["keepers"]),
            ["A.JPG", "B.JPG"])

    def test_enter_in_the_zoom_approves_and_stays_zoomed(self):
        page = self.page()
        self.press(page, Qt.Key.Key_2)
        self.press(page, Qt.Key.Key_Return)
        state = self.reviews.public_state()["clusters"]["group-0001"]
        self.assertTrue(state["reviewed"])
        self.assertEqual(page.current, "group-0002")
        self.assertEqual(page.views.currentIndex(), 2)
        self.assertEqual(page.zoom.name, "C.JPG")

    def test_group_arrows_keep_the_lightbox_open(self):
        page = self.page()
        self.press(page, Qt.Key.Key_2)
        self.press(page, Qt.Key.Key_Down)
        self.assertEqual(page.current, "group-0002")
        self.assertEqual(page.views.currentIndex(), 2)
        self.assertEqual(page.zoom.name, "C.JPG")
        self.press(page, Qt.Key.Key_Up)
        self.assertEqual(page.current, "group-0001")
        self.assertEqual(page.zoom.name, "A.JPG")

    def test_enter_on_the_frames_approves_the_group_and_moves_on(self):
        page = self.page()
        self.press(page, Qt.Key.Key_Return)
        state = self.reviews.public_state()["clusters"]["group-0001"]
        self.assertTrue(state["reviewed"])
        self.assertEqual(state["keepers"], ["A.JPG"])
        self.assertEqual(page.current, "group-0002")

    def test_the_digit_that_opened_the_zoom_closes_it(self):
        page = self.page()
        self.press(page, Qt.Key.Key_2)
        self.assertEqual(page.views.currentIndex(), 2)
        self.press(page, Qt.Key.Key_2)
        self.assertEqual(page.views.currentIndex(), 1)
        self.assertEqual(page.focus_name(), "B.JPG")
        # A different digit while zoomed just moves the zoom.
        self.press(page, Qt.Key.Key_1)
        self.assertEqual(page.views.currentIndex(), 2)
        self.press(page, Qt.Key.Key_2)
        self.assertEqual(page.views.currentIndex(), 2)
        self.assertEqual(page.zoom.name, "B.JPG")

    def test_the_zoom_caption_says_whose_decision_it_is(self):
        page = self.page()
        self.press(page, Qt.Key.Key_1)
        self.assertIn("AI proposal", page.zoom.caption.text())
        self.assertIn("kept ✓", page.zoom.caption.text())
        self.assertIn("AI recommended", page.zoom.caption.text())
        self.press(page, Qt.Key.Key_Space)
        self.assertIn("not kept", page.zoom.caption.text())
        self.assertIn("your decision", page.zoom.caption.text())

    def test_the_group_list_hides_on_the_overview_and_shows_pictures(self):
        page = self.overview()
        self.assertFalse(page.clusters.isVisibleTo(page))
        page.show_cluster("group-0001")
        self.assertTrue(page.clusters.isVisibleTo(page))
        rows = [page.clusters.item(i) for i in range(page.clusters.count())]
        entries = [item for item in rows
                   if item.data(Qt.ItemDataRole.UserRole)]
        self.assertEqual(len(entries), 2)
        for item in entries:
            self.assertEqual(item.text(), "")
            self.assertFalse(item.icon().isNull())

    def test_the_list_never_steals_the_keyboard(self):
        page = self.page()
        self.assertEqual(
            page.clusters.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_a_reviewed_group_is_marked_in_the_index(self):
        page = self.page()
        page.accept_ai()
        rows = [page.clusters.item(i) for i in range(page.clusters.count())]
        flags = {
            str(item.data(Qt.ItemDataRole.UserRole)):
                bool(item.data(Qt.ItemDataRole.UserRole + 1))
            for item in rows if item.data(Qt.ItemDataRole.UserRole)}
        self.assertTrue(flags["group-0001"])
        self.assertFalse(flags["group-0002"])

    def test_z_zooms_whatever_the_focus_is_on(self):
        """Digits stop at nine; the focus does not."""
        page = self.page()
        self.press(page, Qt.Key.Key_Right)
        self.press(page, Qt.Key.Key_Z)
        self.assertEqual(page.views.currentIndex(), 2)
        self.assertEqual(page.zoom.name, "B.JPG")
        self.press(page, Qt.Key.Key_Z)
        self.assertEqual(page.views.currentIndex(), 1)
        self.assertEqual(page.focus_name(), "B.JPG")

    def test_a_double_click_inspects_that_frame(self):
        page = self.page()
        page.frames["B.JPG"].inspected.emit("B.JPG")
        self.assertEqual(page.views.currentIndex(), 2)
        self.assertEqual(page.zoom.name, "B.JPG")
        self.assertEqual(page.focus_name(), "B.JPG")

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

    def test_up_and_down_move_between_clusters(self):
        page = self.page()
        self.press(page, Qt.Key.Key_Down)
        self.assertEqual(page.current, "group-0002")
        self.press(page, Qt.Key.Key_Up)
        self.assertEqual(page.current, "group-0001")

    def test_cluster_arrows_stop_at_the_ends(self):
        page = self.page()
        self.press(page, Qt.Key.Key_Up)
        self.assertEqual(page.current, "group-0001")
        self.press(page, Qt.Key.Key_Down)
        self.press(page, Qt.Key.Key_Down)
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
        root = Path(self._temporary.name).resolve()
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

if __name__ == "__main__":
    unittest.main()
