import json
import tempfile
import unittest
from pathlib import Path

from move_selected import (
    DEFAULT_SUBFOLDER,
    MoveSelectedError,
    build_plan,
    execute,
    selected_names,
)


class MoveSelectedTests(unittest.TestCase):
    def make_report(self, root: Path, photos: list[str]) -> Path:
        report = root / "result.json"
        report.write_text(
            json.dumps({"keep": [{"cluster_id": "g1", "photos": photos}]}),
            encoding="utf-8",
        )
        return report

    def test_extracts_unique_selected_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = self.make_report(root, ["A.JPG", "A.JPG", "B.RAF"])
            self.assertEqual(selected_names(report), ["A.JPG", "B.RAF"])

    def test_build_and_execute_move_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos = root / "photos"
            photos.mkdir()
            (photos / "A.JPG").write_bytes(b"photo")
            report = self.make_report(root, ["A.JPG"])

            plan = build_plan(report, photos, "keepers")
            self.assertTrue((photos / "A.JPG").exists())
            self.assertFalse((photos / "keepers").exists())

            execute(plan)
            self.assertFalse((photos / "A.JPG").exists())
            self.assertEqual((photos / "keepers" / "A.JPG").read_bytes(), b"photo")

    def test_default_cli_destination_name_is_opensull(self):
        self.assertEqual(DEFAULT_SUBFOLDER, "opensull")

    def test_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos = root / "photos"
            (photos / "selected").mkdir(parents=True)
            (photos / "A.JPG").write_bytes(b"original")
            (photos / "selected" / "A.JPG").write_bytes(b"existing")
            report = self.make_report(root, ["A.JPG"])

            with self.assertRaisesRegex(MoveSelectedError, "already exists"):
                build_plan(report, photos, "selected")

    def test_refuses_ambiguous_recursive_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos = root / "photos"
            (photos / "one").mkdir(parents=True)
            (photos / "two").mkdir()
            (photos / "one" / "A.JPG").write_bytes(b"one")
            (photos / "two" / "A.JPG").write_bytes(b"two")
            report = self.make_report(root, ["A.JPG"])

            with self.assertRaisesRegex(MoveSelectedError, "ambiguous"):
                build_plan(report, photos, "selected", recursive=True)

    def test_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = self.make_report(root, ["../A.JPG"])
            with self.assertRaisesRegex(MoveSelectedError, "unsafe"):
                selected_names(report)


if __name__ == "__main__":
    unittest.main()
