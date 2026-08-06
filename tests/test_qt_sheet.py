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
        self.assertIn("first 120 of 500", shown)
        self.assertIn("All of them are read", shown)

    def test_a_sheet_within_the_limit_says_nothing_about_limits(self):
        self.assertNotIn("Showing the first", self.text(self.sheet(list(NAMES))))

    def test_the_whole_selection_is_remembered_even_when_not_all_are_drawn(self):
        many = [f"F{index:04d}.JPG" for index in range(500)]
        self.assertEqual(len(self.sheet(many, limit=10).selection), 500)


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
