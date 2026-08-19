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
        self.root = Path(self._temporary.name).resolve()
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
        self.root = Path(self._temporary.name).resolve()
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

class FolderClassificationTests(unittest.TestCase):
    """What a folder holds decides what can be done to it."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()

    def tearDown(self):
        self._temporary.cleanup()

    def make(self, *names):
        for name in names:
            path = self.root / name
            if path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                write_photo(path, size=(40, 30))
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\0")
        return scan.classify_folder(self.root)

    def test_only_raw_is_a_raw_folder(self):
        result = self.make("A.RAF", "B.CR3", "C.NEF")
        self.assertEqual(result["kind"], "raw")
        self.assertEqual(result["raw"], 3)
        self.assertEqual(result["bitmap"], 0)

    def test_only_rendered_files_is_a_bitmap_folder(self):
        result = self.make("A.JPG", "B.jpeg", "C.png")
        self.assertEqual(result["kind"], "bitmap")
        self.assertEqual(result["bitmap"], 3)

    def test_both_kinds_is_mixed(self):
        result = self.make("A.RAF", "A.JPG")
        self.assertEqual(result["kind"], "mixed")
        self.assertEqual((result["raw"], result["bitmap"]), (1, 1))

    def test_a_folder_with_no_photographs_is_empty(self):
        (self.root / "notes.txt").write_text("x", encoding="utf-8")
        result = scan.classify_folder(self.root)
        self.assertEqual(result["kind"], "empty")
        self.assertEqual(result["total"], 0)

    def test_case_does_not_decide_the_kind(self):
        self.assertEqual(self.make("a.raf", "B.RaF")["kind"], "raw")

    def test_nested_folders_are_counted(self):
        write_photo(self.root / "day2" / "A.JPG", size=(40, 30))
        self.assertEqual(scan.classify_folder(self.root)["kind"], "bitmap")

class FolderSampleTests(unittest.TestCase):
    """The library shows a folder by its frames, so it must name a few."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()

    def tearDown(self):
        self._temporary.cleanup()

    def make(self, count: int, suffix: str = ".JPG") -> dict:
        for index in range(count):
            path = self.root / f"DSCF{index:04d}{suffix}"
            if suffix.lower() in {".jpg", ".jpeg", ".png"}:
                write_photo(path, size=(40, 30))
            else:
                path.write_bytes(b"\0")
        return scan.classify_folder(self.root)

    def test_a_large_folder_is_sampled_not_truncated(self):
        result = self.make(30)
        self.assertEqual(len(result["samples"]), 3)
        # Spread across the folder, not the first three frames of the shoot.
        self.assertNotEqual(
            result["samples"],
            ["DSCF0000.JPG", "DSCF0001.JPG", "DSCF0002.JPG"])

    def test_a_small_folder_offers_what_it_has(self):
        self.assertEqual(len(self.make(2)["samples"]), 2)

    def test_an_empty_folder_offers_nothing(self):
        self.assertEqual(scan.classify_folder(self.root)["samples"], [])

    def test_rendered_files_are_preferred_over_raw(self):
        write_photo(self.root / "B.JPG", size=(40, 30))
        (self.root / "A.RAF").write_bytes(b"\0")
        # A JPEG decodes without a RAW library and is what the camera chose.
        self.assertEqual(scan.classify_folder(self.root)["samples"], ["B.JPG"])

    def test_a_raw_only_folder_still_offers_samples(self):
        result = self.make(4, suffix=".RAF")
        self.assertEqual(len(result["samples"]), 3)

    def test_samples_are_relative_to_the_folder(self):
        write_photo(self.root / "day2" / "A.JPG", size=(40, 30))
        self.assertEqual(
            scan.classify_folder(self.root)["samples"], ["day2/A.JPG"])

if __name__ == "__main__":
    unittest.main()
