import subprocess
import tempfile
import unittest
from pathlib import Path

from raw_developer import (
    BASELINE_FORMAT,
    RawDevelopmentError,
    parse_identification,
    render_baseline,
)


IDENTITY = """Camera: Fujifilm X-S10 ID: 0x26f12
ISO speed: 800
Number of raw images: 1
Full size:   6384 x 4182
Raw inset, width x height: 6240 x 4155 left: 6 top: 18
Filter pattern: GGGGBRGGGGRBGGGG
BlackLevelRepeatDim: 6 x 6
Lens: SIGMA 16mm F1.4 DC DN
"""


class RawDeveloperTests(unittest.TestCase):
    def test_parses_allowlisted_xtrans_metadata(self):
        value = parse_identification(IDENTITY)
        self.assertEqual(value["camera"], "Fujifilm X-S10")
        self.assertEqual(value["active_size"], [6240, 4155, 6, 18])
        self.assertEqual(value["black_repeat"], [6, 6])
        self.assertNotIn("serial", value)

    def test_rejects_incomplete_identification(self):
        with self.assertRaisesRegex(RawDevelopmentError, "omitted"):
            parse_identification("Camera: Fujifilm X-S10\n")

    def test_baseline_is_atomic_and_never_targets_source_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "source"
            source_dir.mkdir()
            source = source_dir / "A.RAF"
            source.write_bytes(b"immutable-raw")
            with self.assertRaisesRegex(
                    RawDevelopmentError, "beside the original"):
                render_baseline(source, source_dir)

            def runner(command, **kwargs):
                if Path(command[0]).name == "raw-identify":
                    return subprocess.CompletedProcess(command, 0, IDENTITY, "")
                output = Path(command[command.index("-Z") + 1]) \
                    if "-Z" in command else Path(command[-1])
                output.write_bytes(b"rendered")
                return subprocess.CompletedProcess(command, 0, "", "")

            result = render_baseline(
                source, root / "output", runner=runner,
                dcraw_tool="dcraw_emu", identify_tool="raw-identify",
                image_tool="magick")
            self.assertEqual(result["format"], BASELINE_FORMAT)
            self.assertEqual(
                result["source"]["sha256"],
                "f9c56f507acf7661001d43323d4f0570bedf4f7c7efb9c078d2b66401bf5a4c3",
            )
            self.assertTrue(Path(
                result["outputs"]["linear_tiff"]["path"]).is_file())
            self.assertEqual(list(source_dir.iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
