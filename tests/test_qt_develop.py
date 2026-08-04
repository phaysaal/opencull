"""The native develop page: comparison, treatments, and rendering on request."""

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

NAMES = ["A.JPG", "B.JPG", "C.JPG"]


def build_shoot(root: Path) -> tuple[Path, Path]:
    photos = root / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(NAMES):
        Image.new("RGB", (160, 120), (60 + index * 20, 100, 90)).save(
            photos / name)
    report = root / "shoot-results.json"
    report.write_text(json.dumps({
        "format": "opencull-report-v2", "manifest_sha256": "x",
        "clusters": [{"cluster_id": "group-0001", "photos": NAMES}],
        "keep": [{"cluster_id": "group-0001", "photos": [NAMES[0]],
                  "rationale": "sharpest", "confidence": 0.8, "warning": "",
                  "fallback": False, "photographic_assessment": []}],
        "warnings": [], "adaptive_clustering": {"enabled": False},
        "notice": "read only",
    }), encoding="utf-8")
    return report, photos


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class DevelopPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.report_path, self.photos_path = build_shoot(root)
        self.report = load_report(self.report_path)
        self.photos = PhotoStore(self.photos_path, root / "cache")
        self.addCleanup(self._temporary.cleanup)

    def page(self, engines=None):
        from opencull_qt.develop import DevelopPage, workspace_for
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.photos)
        page = DevelopPage(
            self.report, workspace_for(self.report, self.photos.root), loader,
            engines=engines if engines is not None else {"default"})
        page.resize(900, 600)
        page.show()
        self.addCleanup(page.shutdown)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def wait_for(self, condition, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_every_photograph_in_the_selection_is_listed(self):
        page = self.page()
        self.assertEqual(
            [page.photos.item(row).data(Qt.ItemDataRole.UserRole)
             for row in range(page.photos.count())],
            NAMES)

    def test_a_culled_folder_offers_the_baseline_without_any_suggestion(self):
        # No shortlist, no edit directions, no provider call: the ordinary
        # state of a folder that has just been culled.
        page = self.page()
        self.assertEqual(
            [item["id"] for item in page.available], ["calibrated"])
        self.assertTrue(page.develop_button.isEnabled())

    def test_nothing_is_rendered_until_it_is_asked_for(self):
        page = self.page()
        self.assertEqual(page.rendered, {})
        self.assertIn("Not developed", page.treated.image.text())
        # Moving through the frames must not start rendering either. A
        # render costs real time, so it happens when a person asks.
        page.step(1)
        page.step(1)
        self.assertEqual(page.rendered, {})

    def test_developing_a_frame_produces_a_picture_and_keeps_the_original(self):
        page = self.page()
        before = (self.photos_path / "A.JPG").read_bytes()
        page.develop_current()
        self.assertTrue(
            self.wait_for(lambda: ("A.JPG", "calibrated") in page.rendered),
            "the render never arrived")
        self.assertFalse(page.treated._pixmap.isNull())
        self.assertEqual((self.photos_path / "A.JPG").read_bytes(), before)

    def test_the_second_look_at_a_render_does_not_render_again(self):
        page = self.page()
        page.develop_current()
        self.assertTrue(
            self.wait_for(lambda: ("A.JPG", "calibrated") in page.rendered))
        generation = page.renderer._generation
        page.develop_current()
        self.assertEqual(page.renderer._generation, generation)

    def test_moving_frames_abandons_a_render_that_is_no_longer_wanted(self):
        page = self.page()
        page.develop_current()
        generation = page.renderer._generation
        page.step(1)
        self.assertNotEqual(page.renderer._generation, generation)

    def test_the_page_says_which_rendering_it_is_about_to_make(self):
        # No RAW is matched here, so the note must say so rather than imply a
        # demosaic that is not happening.
        page = self.page()
        self.assertIn("No RAW", page.engine_note.text())

    def test_a_raw_without_darktable_is_named_as_the_camera_rendering(self):
        page = self.page()
        page.workspace.raw_files = lambda photo: ["/somewhere/A.ARW"]
        page._chose_treatment(0)
        self.assertIn("darktable is not installed", page.engine_note.text())
        # And the render must not claim to be a demosaic it cannot perform.
        self.assertEqual(page.engine_for("A.JPG"), "default")

    def test_a_raw_with_darktable_is_demosaiced(self):
        page = self.page(engines={"default", "darktable"})
        page.workspace.raw_files = lambda photo: ["/somewhere/A.ARW"]
        page._chose_treatment(0)
        self.assertIn("demosaiced by darktable", page.engine_note.text())
        self.assertEqual(page.engine_for("A.JPG"), "darktable")

    def test_arrow_keys_walk_the_selection(self):
        page = self.page()
        page.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(page.current, "B.JPG")
        page.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(page.current, "A.JPG")

    def test_escape_leaves_the_page(self):
        page = self.page()
        closed = []
        page.closed.connect(lambda: closed.append(True))
        page.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
        self.assertEqual(closed, [True])

    def test_a_failed_render_says_why_and_leaves_the_button_usable(self):
        page = self.page()
        page._render_failed("A.JPG", "the reference photograph is unavailable")
        self.assertTrue(page.develop_button.isEnabled())
        self.assertIn("unavailable", page.status.text())
        self.assertEqual(page.status.property("tone"), "alarm")


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class EngineDetectionTests(unittest.TestCase):
    def test_default_is_always_there_and_darktable_is_not_assumed(self):
        from opencull_qt.develop import available_engines

        engines = available_engines()
        self.assertIn("default", engines)
        self.assertLessEqual(engines, {"default", "darktable"})


if __name__ == "__main__":
    unittest.main()
