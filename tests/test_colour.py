"""Behavioural tests for colour: what files say, and what the screen gets.

Two claims are worth holding down. A file this application writes must
say it is sRGB, because a file that says nothing gets guessed at. And
what the interface draws must be converted for the screen's profile,
because drawing sRGB numbers unconverted on a wide-gamut display shows a
more saturated photograph than the one on disk.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from colour_profile import srgb_profile, tag_in_place  # noqa: E402
from development_engine import render_recipe  # noqa: E402
from opencull_gui.photos import PhotoStore  # noqa: E402
from opencull_gui.proofsheet import _embedded  # noqa: E402


def tagged(path: Path) -> bytes:
    with Image.open(path) as image:
        return image.info.get("icc_profile", b"")


class WrittenFilesSaySRGBTests(unittest.TestCase):
    """Every JPEG the application writes carries the profile."""

    def test_the_profile_is_a_real_one(self):
        self.assertGreater(len(srgb_profile()), 100)
        self.assertEqual(srgb_profile()[36:40], b"acsp")

    def test_a_render_is_tagged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.tiff"
            tifffile.imwrite(baseline, np.full((24, 32, 3), 9000, np.uint16))
            result = render_recipe(baseline, {
                "format": "opencull-development-recipe-v1",
                "source_photo": "A.RAF", "source_kind": "raw",
                "style": "standard",
                "operations": [{"op": "tone.exposure", "value": 1,
                                "mode": "delta"}],
            }, root / "out")
            self.assertEqual(
                tagged(Path(result["output"]["path"])), srgb_profile())

    def test_a_generated_preview_is_tagged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            Image.new("RGB", (900, 600), (110, 90, 70)).save(photos / "A.JPG")
            store = PhotoStore(photos, root / "cache")
            self.assertEqual(
                tagged(Path(store.preview("A.JPG", "thumb"))), srgb_profile())

    def test_a_proof_sheet_frame_is_tagged(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "A.JPG"
            Image.new("RGB", (120, 90), (60, 100, 140)).save(path)
            encoded = _embedded(path)
            self.assertIn("data:image/jpeg;base64,", encoded)
            import base64
            import io
            body = base64.b64decode(encoded.split(",", 1)[1])
            with Image.open(io.BytesIO(body)) as image:
                self.assertEqual(image.info.get("icc_profile", b""),
                                 srgb_profile())


class TaggingFinishedWorkTests(unittest.TestCase):
    """A photograph already exported gets its profile without recompression."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def a_photograph(self, name="A.jpg", **saved) -> Path:
        path = self.root / name
        pixels = np.random.default_rng(7).integers(
            0, 255, (48, 64, 3)).astype(np.uint8)
        Image.fromarray(pixels).save(path, quality=92, **saved)
        return path

    def test_tagging_changes_no_pixel(self):
        path = self.a_photograph()
        before = np.asarray(Image.open(path).convert("RGB"))
        self.assertTrue(tag_in_place(path))
        with Image.open(path) as image:
            self.assertEqual(image.info.get("icc_profile"), srgb_profile())
            self.assertTrue(np.array_equal(
                before, np.asarray(image.convert("RGB"))))

    def test_an_already_tagged_file_is_left_alone(self):
        path = self.a_photograph(icc_profile=srgb_profile())
        untouched = path.read_bytes()
        self.assertFalse(tag_in_place(path))
        self.assertEqual(path.read_bytes(), untouched)

    def test_exif_survives_and_stays_in_front(self):
        from PIL import Image as PILImage
        exif = PILImage.Exif()
        exif[271] = "FUJIFILM"
        path = self.a_photograph(exif=exif)
        self.assertTrue(tag_in_place(path))
        with Image.open(path) as image:
            self.assertEqual(image.getexif().get(271), "FUJIFILM")
            self.assertEqual(image.info.get("icc_profile"), srgb_profile())

    def test_something_that_is_not_a_jpeg_is_refused(self):
        path = self.root / "notes.txt"
        path.write_text("not a photograph")
        with self.assertRaises(ValueError):
            tag_in_place(path)


class ScreenConversionTests(unittest.TestCase):
    """What is drawn is converted for the screen, or deliberately is not."""

    def setUp(self):
        # A QApplication, not the lighter QGuiApplication these tests would
        # be happy with: the instance is process-wide, and a widget test
        # later in the run cannot be constructed under a gui-only one.
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        from opencull_qt import colour
        self.colour = colour
        self.forget()
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self.forget()
        self._temporary.cleanup()

    def forget(self):
        for cached in (self.colour.display_profile, self.colour._display_space,
                       self.colour._srgb):
            cached.cache_clear()

    def a_profile(self) -> Path:
        """A profile file on disk, written rather than found.

        Taking the running machine's profile would make this test say one
        thing on the photographer's calibrated screen and another on a
        laptop with no colord at all.
        """
        path = self.root / "screen.icc"
        path.write_bytes(srgb_profile())
        return path

    def image(self, colour=(200, 40, 40)):
        from PySide6.QtGui import QImage
        array = np.full((4, 6, 3), colour, np.uint8)
        return QImage(array.tobytes(), 6, 4, 18,
                      QImage.Format.Format_RGB888).copy()

    def test_no_profile_means_pixels_pass_through(self):
        with mock.patch.dict(
            "os.environ", {self.colour.PROFILE_ENVIRONMENT: "none"}
        ):
            self.forget()
            self.assertFalse(self.colour.managed())
            image = self.image()
            self.assertEqual(self.colour.for_screen(image).pixel(0, 0),
                             image.pixel(0, 0))

    def test_a_named_profile_is_used(self):
        with mock.patch.dict(
            "os.environ",
            {self.colour.PROFILE_ENVIRONMENT: str(self.a_profile())}
        ):
            self.forget()
            self.assertTrue(self.colour.managed())

    def test_converting_towards_a_wider_screen_pulls_saturation_in(self):
        """The behaviour the whole change exists for.

        A display whose red primary is wider than sRGB's must be driven
        with *less* than full red to show sRGB's red. Unconverted, the
        photograph is shown more saturated than it is.
        """
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColorSpace
        wide = QColorSpace(
            QPointF(0.3127, 0.3290), QPointF(0.708, 0.292),
            QPointF(0.170, 0.797), QPointF(0.131, 0.046),
            QColorSpace.TransferFunction.SRgb)
        self.assertTrue(wide.isValid())
        with mock.patch.object(
            self.colour, "_display_space", lambda: wide
        ):
            red = self.image((255, 0, 0))
            shown = self.colour.for_screen(red)
        self.assertLess((shown.pixel(0, 0) >> 16) & 0xFF, 255)
        self.assertGreater((shown.pixel(0, 0) >> 8) & 0xFF, 0)

    def test_an_untagged_file_is_read_as_srgb(self):
        path = self.root / "plain.jpg"
        Image.new("RGB", (8, 8), (200, 40, 40)).save(path)
        with mock.patch.dict(
            "os.environ",
            {self.colour.PROFILE_ENVIRONMENT: str(self.a_profile())}
        ):
            self.forget()
            pixmap = self.colour.load_for_screen(path)
        self.assertFalse(pixmap.isNull())
        self.assertEqual((pixmap.width(), pixmap.height()), (8, 8))

    def test_an_unreadable_file_gives_an_empty_pixmap(self):
        self.assertTrue(
            self.colour.load_for_screen(self.root / "absent.jpg").isNull())

    def test_colord_prefers_the_primary_screen(self):
        laptop = self.root / "laptop.icc"
        external = self.root / "external.icc"
        for path in (laptop, external):
            path.write_bytes(srgb_profile())
        listing = (
            "Object Path:   /devices/xrandr_laptop\n"
            "Type:          display\n"
            f"Profile 1:     icc-aaa\n               {laptop}\n"
            "Metadata:      XRANDR_name=eDP-1\n"
            "\n"
            "Object Path:   /devices/xrandr_external\n"
            "Type:          display\n"
            f"Profile 1:     icc-bbb\n               {external}\n"
            "Metadata:      OutputPriority=primary\n")

        class Result:
            returncode = 0
            stdout = listing

        with mock.patch("subprocess.run", return_value=Result()):
            self.assertEqual(self.colour._from_colord(), external)

    def test_no_colord_is_not_an_error(self):
        with mock.patch("subprocess.run", side_effect=OSError):
            self.assertIsNone(self.colour._from_colord())


if __name__ == "__main__":
    unittest.main()
