"""The contact sheet: what a run is about to read, before it reads it."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui.photos import PhotoStore  # noqa: E402
from shortlist_kernel import chosen_photographs  # noqa: E402
from tests.test_qt_develop import NAMES, build_shoot  # noqa: E402


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class ContactSheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.report_path, self.photos_path = build_shoot(self.root)
        self.store = PhotoStore(self.photos_path, self.root / "cache")
        self.addCleanup(self._temporary.cleanup)

    def loader(self):
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.store, None)
        self.addCleanup(loader.shutdown)
        return loader

    def sheet(self, names, **kwargs):
        from opencull_qt.sheet import ContactSheet

        widget = ContactSheet(names, self.loader(), **kwargs)
        self.addCleanup(widget.deleteLater)
        return widget

    def text(self, widget) -> str:
        return "\n".join(label.text() for label in widget.findChildren(QLabel))

    def test_the_sheet_deals_extra_width_as_extra_columns(self):
        from opencull_qt.sheet import TILE, TILE_MAX, ContactSheet

        # A wide window: more columns at the base size, tiles near it.
        columns, tile = ContactSheet.sheet_geometry(2000, 120, 10)
        self.assertEqual(columns, 10)
        self.assertLess(tile, TILE_MAX)
        self.assertGreaterEqual(tile, TILE)
        # A narrow one: fewer columns, never a zero.
        columns, tile = ContactSheet.sheet_geometry(400, 120, 10)
        self.assertEqual(columns, 2)
        # Few frames on a huge window: columns stop at the frame count
        # and the tiles stop at the ceiling.
        columns, tile = ContactSheet.sheet_geometry(3000, 3, 10)
        self.assertEqual(columns, 3)
        self.assertEqual(tile, TILE_MAX)

    def test_every_frame_of_the_selection_gets_a_tile(self):
        sheet = self.sheet(list(NAMES))
        self.assertEqual(sorted(sheet.tiles), sorted(NAMES))

    def test_each_tile_is_named(self):
        shown = self.text(self.sheet(list(NAMES)))
        for name in NAMES:
            self.assertIn(name, shown)

    def test_a_shoot_too_large_to_draw_says_what_it_left_out(self):
        many = [f"F{index:04d}.JPG" for index in range(500)]
        sheet = self.sheet(many, limit=120)
        self.assertEqual(len(sheet.tiles), 120)
        shown = self.text(sheet)
        self.assertIn("Showing 120 of 500", shown)
        self.assertIn("All 500 are included", shown)

    def test_a_sheet_within_the_limit_says_nothing_about_limits(self):
        self.assertNotIn("Showing the first", self.text(self.sheet(list(NAMES))))

    def test_the_whole_selection_is_remembered_even_when_not_all_are_drawn(self):
        many = [f"F{index:04d}.JPG" for index in range(500)]
        self.assertEqual(len(self.sheet(many, limit=10).selection), 500)


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class SelectableSheetTests(ContactSheetTests):
    """The sheet as the prefilter: leaving out is a visible decision."""

    def selectable(self, names=None, **kwargs):
        return self.sheet(
            list(names if names is not None else NAMES),
            selectable=True, **kwargs)

    def test_everything_starts_ticked(self):
        sheet = self.selectable()
        self.assertEqual(sheet.chosen(), list(NAMES))

    def test_a_click_leaves_a_frame_out_and_says_so(self):
        sheet = self.selectable()
        changes = []
        sheet.changed.connect(lambda: changes.append(True))
        sheet.toggle(NAMES[1])
        self.assertEqual(sheet.chosen(), [NAMES[0], NAMES[2]])
        self.assertFalse(sheet.tiles[NAMES[1]].included)
        self.assertEqual(len(changes), 1)

    def test_a_second_click_brings_it_back(self):
        sheet = self.selectable()
        sheet.toggle(NAMES[1])
        sheet.toggle(NAMES[1])
        self.assertEqual(sheet.chosen(), list(NAMES))
        self.assertTrue(sheet.tiles[NAMES[1]].included)

    def test_frames_past_the_first_page_are_reachable_by_showing_more(self):
        """The cap used to be a cliff: "only the frames shown can be
        left out" was the sheet telling a photographer with 300 frames
        that the zoomed passages at frame 200 did not exist."""
        many = [f"F{index:04d}.JPG" for index in range(500)]
        sheet = self.selectable(many, limit=10)
        sheet.toggle(many[3])
        self.assertEqual(len(sheet.chosen()), 499)
        # Beyond the first page: no tile yet, so no toggle yet.
        sheet.toggle(many[15])
        self.assertEqual(len(sheet.chosen()), 499)
        sheet.show_more()
        self.assertEqual(len(sheet.names), 20)
        sheet.toggle(many[15])
        self.assertEqual(len(sheet.chosen()), 498)

    def test_the_notice_says_all_are_included_and_offers_more(self):
        many = [f"F{index:04d}.JPG" for index in range(500)]
        sheet = self.selectable(many, limit=10)
        self.assertIn("All 500 are included", sheet.notice.text())
        self.assertIn("show more", sheet.notice.text())
        self.assertEqual(sheet.more_button.text(), "Show 10 more")
        self.assertTrue(sheet.more_button.isVisibleTo(sheet))

    def test_the_last_page_retires_the_button(self):
        many = [f"F{index:04d}.JPG" for index in range(25)]
        sheet = self.selectable(many, limit=10)
        sheet.show_more()
        self.assertEqual(sheet.more_button.text(), "Show 5 more")
        sheet.show_more()
        self.assertEqual(len(sheet.names), 25)
        self.assertFalse(sheet.more_button.isVisibleTo(sheet))
        self.assertIn("All 25 frames shown", sheet.notice.text())

    def test_a_frame_left_out_before_its_page_arrives_stays_left_out(self):
        many = [f"F{index:04d}.JPG" for index in range(30)]
        sheet = self.selectable(many, limit=10)
        sheet.set_all(False)          # everything left out, tiles or not
        sheet.show_more()
        self.assertFalse(sheet.tiles[many[12]].included)

    def test_a_display_sheet_ignores_clicks_entirely(self):
        sheet = self.sheet(list(NAMES))
        sheet.toggle(NAMES[0])
        self.assertEqual(sheet.chosen(), list(NAMES))

    def test_space_toggles_the_focused_tile(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        sheet = self.selectable()
        tile = sheet.tiles[NAMES[0]]
        tile.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Space,
            Qt.KeyboardModifier.NoModifier))
        self.assertNotIn(NAMES[0], sheet.chosen())

    def test_set_all_unticks_and_reticks_every_drawn_tile(self):
        sheet = self.selectable()
        sheet.set_all(False)
        self.assertEqual(sheet.chosen(), [])
        sheet.set_all(True)
        self.assertEqual(sheet.chosen(), list(NAMES))


class SelectionRuleTests(unittest.TestCase):
    """The page must show the list the run will actually read."""

    REPORT = {
        "clusters": [
            {"cluster_id": "g1", "photos": ["A.JPG", "B.JPG", "C.JPG"]},
            {"cluster_id": "g2", "photos": ["D.JPG", "E.JPG"]},
        ],
        "keep": [
            {"cluster_id": "g1", "photos": ["B.JPG"]},
            {"cluster_id": "g2", "photos": ["E.JPG"]},
        ],
    }

    def test_a_cull_narrows_the_selection_to_its_keepers(self):
        self.assertEqual(
            chosen_photographs(self.REPORT, None), ["B.JPG", "E.JPG"])

    def test_a_reviewed_cull_uses_the_photographers_keepers(self):
        review = {"revision": 2, "clusters": {
            "g1": {"reviewed": True, "keepers": ["A.JPG", "C.JPG"]}}}
        self.assertEqual(
            chosen_photographs(self.REPORT, review),
            ["A.JPG", "C.JPG", "E.JPG"])

    def test_an_everything_included_selection_keeps_everything(self):
        report = {
            "clusters": [{"cluster_id": "g1", "photos": ["A.JPG", "B.JPG"]}],
            "keep": [{"cluster_id": "g1", "photos": ["A.JPG", "B.JPG"]}],
        }
        self.assertEqual(chosen_photographs(report, None), ["A.JPG", "B.JPG"])

    def test_a_keeper_that_is_not_in_its_cluster_is_refused(self):
        report = {
            "clusters": [{"cluster_id": "g1", "photos": ["A.JPG"]}],
            "keep": [{"cluster_id": "g1", "photos": ["/etc/passwd"]}],
        }
        self.assertEqual(chosen_photographs(report, None), [])


if __name__ == "__main__":
    unittest.main()
