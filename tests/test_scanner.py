import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import scan


class EmbeddedScannerTests(unittest.TestCase):
    def test_recursive_scan_excludes_darkimiya_managed_photographs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (32, 24), "blue").save(root / "source.jpg")
            managed = root / "Darkimiya" / "Developments"
            managed.mkdir(parents=True)
            Image.new("RGB", (32, 24), "red").save(managed / "render.jpg")

            manifest = json.loads(scan.scan_directory(str(root), recursive=True))

            self.assertEqual(manifest["photo_count"], 1)
            self.assertEqual(
                manifest["groups"][0]["candidates"][0]["name"], "source.jpg")

    def test_scan_directory_returns_manifest_without_modifying_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "sample.jpg"
            Image.new("RGB", (64, 48), (40, 80, 120)).save(photo)
            before = (photo.read_bytes(), photo.stat().st_mtime_ns)

            manifest = json.loads(scan.scan_directory(str(root)))

            self.assertEqual(manifest["photo_count"], 1)
            self.assertEqual(len(manifest["groups"]), 1)
            self.assertEqual(
                manifest["groups"][0]["candidates"][0]["name"],
                "sample.jpg",
            )
            self.assertEqual(
                (photo.read_bytes(), photo.stat().st_mtime_ns),
                before,
            )


if __name__ == "__main__":
    unittest.main()
