"""RAW frames reach models as materialized previews, never as raw bytes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import scan  # noqa: E402
from opencull_kernel import candidate_path  # noqa: E402
from scan import visible_photograph  # noqa: E402


def _fake_open_preview(path: Path) -> Image.Image:
    if path.suffix.lower() in scan.RAW_EXTENSIONS:
        return Image.new("RGB", (120, 80), (90, 120, 150))
    with Image.open(path) as image:
        return image.convert("RGB")


class RawPreviewScanTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        (self.root / "DSCF0001.RAF").write_bytes(b"not-an-image" * 64)
        Image.new("RGB", (80, 60), (40, 80, 110)).save(
            self.root / "B0001.JPG")
        self.addCleanup(self._temporary.cleanup)

    def scan(self) -> dict:
        with mock.patch.object(
            scan, "open_preview", side_effect=_fake_open_preview
        ):
            return json.loads(scan.scan_directory(str(self.root)))

    def candidates(self, manifest: dict) -> dict[str, dict]:
        return {
            candidate["name"]: candidate
            for group in manifest["groups"]
            for candidate in group["candidates"]
        }

    def test_a_raw_frame_gets_a_preview_and_a_jpeg_does_not(self):
        candidates = self.candidates(self.scan())
        raw = candidates["DSCF0001.RAF"]
        self.assertIn("preview", raw)
        self.assertTrue((self.root / raw["preview"]).is_file())
        self.assertTrue(raw["preview"].startswith(".darkimiya/Previews/"))
        self.assertNotIn("preview", candidates["B0001.JPG"])

    def test_the_preview_cache_is_content_keyed_and_reused(self):
        first = self.candidates(self.scan())["DSCF0001.RAF"]["preview"]
        written = self.root / first
        stamp = written.stat().st_mtime_ns
        second = self.candidates(self.scan())["DSCF0001.RAF"]["preview"]
        self.assertEqual(first, second)
        self.assertEqual(written.stat().st_mtime_ns, stamp)

    def test_the_cull_resolver_hands_models_the_preview(self):
        candidates = self.candidates(self.scan())
        raw = candidates["DSCF0001.RAF"]
        resolved = candidate_path(str(self.root), raw)
        self.assertEqual(resolved, str(self.root / raw["preview"]))
        plain = candidate_path(str(self.root), candidates["B0001.JPG"])
        self.assertEqual(plain, str(self.root / "B0001.JPG"))

    def test_later_phases_resolve_raw_frames_by_the_cache_convention(self):
        self.scan()
        resolved = visible_photograph(self.root, "DSCF0001.RAF")
        self.assertTrue(str(resolved).endswith(".preview.jpg"))
        self.assertTrue(resolved.is_file())
        untouched = visible_photograph(self.root, "B0001.JPG")
        self.assertEqual(untouched, self.root / "B0001.JPG")

    def test_a_raw_frame_without_a_cache_falls_back_to_the_original(self):
        resolved = visible_photograph(self.root, "DSCF0001.RAF")
        self.assertEqual(resolved, self.root / "DSCF0001.RAF")


if __name__ == "__main__":
    unittest.main()
