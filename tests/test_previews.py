"""Behavioural tests for preview decoding and cache accounting.

These cover the preview path by exercising it, rather than by asserting on the
text of the modules that implement it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scan  # noqa: E402
from opencull_gui.photos import PhotoStore, PreviewManager  # noqa: E402


def write_photo(path: Path, size: tuple[int, int] = (3000, 2000)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (120, 90, 60)).save(path, format="JPEG", quality=90)
    return path


class DecodeOnceTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.photos = self.root / "photos"
        write_photo(self.photos / "A.JPG")
        self.store = PhotoStore(self.photos, self.root / "cache")

    def tearDown(self):
        self._temporary.cleanup()

    def test_one_decode_produces_every_preview_size(self):
        with mock.patch(
            "opencull_gui.photos.open_preview", wraps=scan.open_preview
        ) as decode:
            self.store.generate_preview("A.JPG", "thumb")
            self.assertEqual(decode.call_count, 1)
            for size in self.store.SIZES:
                self.assertIsNotNone(
                    self.store.cached_preview("A.JPG", size),
                    f"{size} preview was not derived from the same decode",
                )

    def test_second_size_is_served_from_cache_without_decoding(self):
        self.store.generate_preview("A.JPG", "thumb")
        with mock.patch("opencull_gui.photos.open_preview") as decode:
            self.store.preview("A.JPG", "detail")
            decode.assert_not_called()

    def test_each_size_is_bounded_by_its_declared_edge(self):
        self.store.generate_preview("A.JPG", "thumb")
        for size, edge in self.store.SIZES.items():
            path = self.store.cached_preview("A.JPG", size)
            self.assertIsNotNone(path)
            with Image.open(path) as image:
                self.assertLessEqual(max(image.size), edge)

    def test_a_changed_source_is_not_served_from_the_old_cache(self):
        first = self.store.generate_preview("A.JPG", "thumb")
        write_photo(self.photos / "A.JPG", size=(1200, 800))
        second = self.store.generate_preview("A.JPG", "thumb")
        self.assertNotEqual(first, second)


class CacheStatsTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.photos = self.root / "photos"
        write_photo(self.photos / "A.JPG")
        self.store = PhotoStore(self.photos, self.root / "cache")

    def tearDown(self):
        self._temporary.cleanup()

    def test_repeated_polling_does_not_rescan_the_cache_directory(self):
        self.store.generate_preview("A.JPG", "thumb")
        self.store.cache_stats()
        with mock.patch.object(
            self.store, "_scan_cache_stats", wraps=self.store._scan_cache_stats
        ) as rescan:
            for _ in range(50):
                self.store.cache_stats()
            rescan.assert_not_called()

    def test_totals_account_for_generated_previews(self):
        empty = self.store.cache_stats()
        self.assertEqual(empty["files"], 0)
        self.store.generate_preview("A.JPG", "thumb")
        after = self.store.cache_stats()
        self.assertEqual(after["files"], len(self.store.SIZES))
        self.assertGreater(after["bytes"], 0)

    def test_clearing_the_cache_resets_the_totals(self):
        self.store.generate_preview("A.JPG", "thumb")
        self.store.clear_cache()
        self.assertEqual(
            self.store.cache_stats(), {"files": 0, "bytes": 0})


class RawDecoderStatusTests(unittest.TestCase):
    def test_rawpy_is_reported_as_the_healthy_decoder(self):
        with mock.patch.object(scan, "rawpy", object()):
            status = scan.raw_decoder_status()
        self.assertEqual(status["decoder"], "rawpy")
        self.assertFalse(status["degraded"])

    def test_sips_fallback_is_reported_as_degraded(self):
        with mock.patch.object(scan, "rawpy", None), mock.patch.object(
            scan.shutil, "which", return_value="/usr/bin/sips"
        ):
            status = scan.raw_decoder_status()
        self.assertEqual(status["decoder"], "sips")
        self.assertTrue(status["degraded"])
        self.assertIn("rawpy", status["detail"])

    def test_absent_decoder_is_reported_as_unavailable(self):
        with mock.patch.object(scan, "rawpy", None), mock.patch.object(
            scan.shutil, "which", return_value=None
        ):
            status = scan.raw_decoder_status()
        self.assertFalse(status["available"])
        self.assertTrue(status["degraded"])

    def test_falling_back_to_sips_warns_rather_than_degrading_silently(self):
        with mock.patch.object(scan, "rawpy", None), mock.patch.object(
            scan, "_sips_fallback_announced", False
        ), mock.patch.object(
            scan, "sips_preview", return_value=Image.new("RGB", (4, 4))
        ):
            with self.assertWarns(RuntimeWarning):
                scan.raw_preview(Path("photo.raf"))

    def test_preview_progress_reports_the_decoder(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            write_photo(root / "photos" / "A.JPG")
            store = PhotoStore(root / "photos", root / "cache")
            manager = PreviewManager(store, workers=1, max_pending=16)
            try:
                self.assertIn("decoder", manager.progress())
            finally:
                manager.shutdown()


class WorkerDefaultTests(unittest.TestCase):
    def test_default_scales_with_the_machine_and_stays_bounded(self):
        for cpus, expected in ((1, 2), (2, 2), (4, 2), (8, 6), (32, 6)):
            with mock.patch("opencull_gui.photos.os.cpu_count", return_value=cpus):
                self.assertEqual(PreviewManager.default_workers(), expected)

    def test_an_explicit_worker_count_is_still_honoured(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            write_photo(root / "photos" / "A.JPG")
            store = PhotoStore(root / "photos", root / "cache")
            manager = PreviewManager(store, workers=1, max_pending=16)
            try:
                self.assertEqual(manager.workers, 1)
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
