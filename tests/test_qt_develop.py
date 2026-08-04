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
            self.report,
            workspace_for(self.report, self.photos.root,
                          decoders=set() if engines is None else engines),
            loader)
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

    def match_a_raw(self, page):
        """Point the first frame at a real RAW file beside the shoot."""
        raw = self.photos_path.parent / "A.ARW"
        raw.write_bytes(b"raw")
        page.workspace.raw_files = lambda photo: [str(raw)]

    def test_a_raw_falls_back_to_libraw_when_darktable_is_absent(self):
        # OpenCull's own renderer, not the camera's rendering. It is the
        # deterministic fallback, and it is a real decode.
        page = self.page(engines={"libraw"})
        self.match_a_raw(page)
        page._chose_treatment(0)
        self.assertIn("LibRaw", page.engine_note.text())
        self.assertEqual(page.engine_for("A.JPG"), "default")

    def test_a_raw_with_neither_decoder_is_named_as_the_camera_rendering(self):
        page = self.page(engines=set())
        self.match_a_raw(page)
        page._chose_treatment(0)
        note = page.engine_note.text()
        self.assertIn("camera's own embedded rendering", note)
        self.assertIn("not a development", note)
        self.assertEqual(page.engine_for("A.JPG"), "default")

    def test_a_raw_with_darktable_is_demosaiced(self):
        page = self.page(engines={"darktable", "libraw"})
        self.match_a_raw(page)
        page._chose_treatment(0)
        self.assertIn("demosaiced by darktable", page.engine_note.text())
        self.assertEqual(page.engine_for("A.JPG"), "darktable")

    def test_exporting_renders_at_full_size_and_writes_the_file(self):
        from unittest import mock

        page = self.page()
        target = self.photos_path.parent / "delivery" / "A.jpg"
        with mock.patch(
            "opencull_qt.develop.QFileDialog.getSaveFileName",
            return_value=(str(target), ""),
        ):
            page.export_current()
        self.assertTrue(
            self.wait_for(lambda: page.exporter.pending == 0),
            "the export never finished")
        self.assertTrue(target.is_file())
        with Image.open(target) as written:
            # The proof on screen is bounded to PROOF_EDGE; a delivery is not.
            self.assertEqual(max(written.size), 160)
        self.assertIn("exported to", page.status.text())
        # And the photograph it came from is untouched.
        self.assertTrue((self.photos_path / "A.JPG").is_file())

    def test_choosing_a_name_that_exists_reports_the_name_actually_written(self):
        from unittest import mock

        page = self.page()
        target = self.photos_path.parent / "A.jpg"
        target.write_bytes(b"already here")
        with mock.patch(
            "opencull_qt.develop.QFileDialog.getSaveFileName",
            return_value=(str(target), ""),
        ):
            page.export_current()
        self.assertTrue(self.wait_for(lambda: page.exporter.pending == 0))
        self.assertEqual(target.read_bytes(), b"already here")
        self.assertIn("A-2.jpg", page.status.text())
        self.assertIn("does not write over a file", page.status.text())

    def test_cancelling_the_chooser_exports_nothing(self):
        from unittest import mock

        page = self.page()
        with mock.patch(
            "opencull_qt.develop.QFileDialog.getSaveFileName",
            return_value=("", ""),
        ):
            page.export_current()
        self.assertEqual(page.exporter.pending, 0)
        self.assertTrue(page.export_button.isEnabled())

    def test_the_offered_name_says_the_frame_and_the_treatment(self):
        from unittest import mock

        page = self.page()
        seen = {}

        def remember(_parent, _title, path, *args, **kwargs):
            seen["path"] = path
            return ("", "")

        with mock.patch(
            "opencull_qt.develop.QFileDialog.getSaveFileName", remember
        ):
            page.export_current()
        self.assertTrue(seen["path"].endswith("A-calibrated.jpg"))
        # And it lands in the project's own Exports directory by default.
        self.assertIn("Exports", seen["path"])

    def test_a_failed_export_says_why_and_leaves_the_button_usable(self):
        page = self.page()
        page._export_failed("A.JPG", "the destination is not writable")
        self.assertTrue(page.export_button.isEnabled())
        self.assertIn("not writable", page.status.text())
        self.assertEqual(page.status.property("tone"), "alarm")

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


class DecoderDetectionTests(unittest.TestCase):
    def test_nothing_is_assumed_to_be_installed(self):
        from opencull_gui.development import available_decoders

        self.assertLessEqual(available_decoders(), {"darktable", "libraw"})

    def test_libraw_needs_every_tool_it_shells_out_to(self):
        from unittest import mock

        from opencull_gui.development import raw_baseline_tools_present

        with mock.patch("shutil.which", lambda name: None):
            self.assertFalse(raw_baseline_tools_present())
        with mock.patch("shutil.which", lambda name: "/usr/bin/" + name):
            self.assertTrue(raw_baseline_tools_present())
        # ImageMagick 6 installs `convert`, not `magick`, and that is what
        # most Linux distributions still package.
        with mock.patch(
            "shutil.which",
            lambda name: None if name == "magick" else "/usr/bin/" + name,
        ):
            self.assertTrue(raw_baseline_tools_present())
        with mock.patch(
            "shutil.which",
            lambda name: None if name == "dcraw_emu" else "/usr/bin/" + name,
        ):
            self.assertFalse(raw_baseline_tools_present())


if __name__ == "__main__":
    unittest.main()
