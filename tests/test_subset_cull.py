"""A cull that reads only the frames the photographer left ticked."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from scan import scan_directory  # noqa: E402

NAMES = ("A0001.JPG", "A0002.JPG", "A0003.JPG")


class SubsetScanTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        for index, name in enumerate(NAMES):
            Image.new("RGB", (80, 60), (30 + 40 * index, 80, 110)).save(
                self.root / name)
        self.addCleanup(self._temporary.cleanup)

    def names(self, manifest: str) -> set[str]:
        value = json.loads(manifest)
        return {
            candidate["name"]
            for group in value["groups"]
            for candidate in group["candidates"]
        }

    def test_a_subset_scan_reads_only_the_wanted_frames(self):
        manifest = scan_directory(
            str(self.root), only_photos=json.dumps(["A0001.JPG", "A0003.JPG"]))
        self.assertEqual(self.names(manifest), {"A0001.JPG", "A0003.JPG"})

    def test_an_unknown_name_fails_loudly_rather_than_shrinking(self):
        with self.assertRaises(ValueError) as caught:
            scan_directory(
                str(self.root), only_photos=json.dumps(["GHOST.JPG"]))
        self.assertIn("GHOST.JPG", str(caught.exception))

    def test_garbage_instead_of_a_list_is_refused(self):
        for bad in ("not json", '"a string"', '{"a": 1}', "[1, 2]"):
            with self.assertRaises(ValueError):
                scan_directory(str(self.root), only_photos=bad)

    def test_the_subset_is_recorded_in_the_manifest_settings(self):
        manifest = json.loads(scan_directory(
            str(self.root), only_photos=json.dumps(["A0002.JPG"])))
        self.assertEqual(manifest["settings"]["only_photos"], ["A0002.JPG"])

    def test_a_full_scan_is_byte_identical_to_what_it_always_was(self):
        # Existing checkpoints are keyed on the manifest hash; the new
        # parameter must not invalidate them.
        self.assertEqual(
            scan_directory(str(self.root)),
            scan_directory(str(self.root), only_photos="[]"))
        self.assertNotIn(
            "only_photos",
            json.loads(scan_directory(str(self.root)))["settings"])

    def test_a_subset_manifest_is_its_own_checkpoint_identity(self):
        self.assertNotEqual(
            scan_directory(str(self.root)),
            scan_directory(str(self.root), only_photos=json.dumps(list(NAMES[:2]))))


class SubsetJobTests(unittest.TestCase):
    """jobs.add carries the prefilter to the command line."""

    def setUp(self):
        from opencull_gui.jobs import JobManager

        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name).resolve()
        self.photos = root / "shoot"
        self.photos.mkdir()
        for name in NAMES:
            Image.new("RGB", (60, 40), (50, 90, 120)).save(self.photos / name)
        self.jobs = JobManager(
            root / "jobs.json", Path(__file__).resolve().parents[1],
            autostart=False)
        self.addCleanup(self._temporary.cleanup)

    def queued(self) -> dict:
        return self.jobs._state["jobs"][0]

    def test_the_prefilter_lands_on_the_job_and_in_the_command(self):
        self.jobs.add(
            str(self.photos), only_photos=["A0001.JPG", "A0003.JPG"])
        job = self.queued()
        self.assertEqual(job["only_photos"], ["A0001.JPG", "A0003.JPG"])
        command = self.jobs._command(job)
        self.assertIn(
            'only_photos=["A0001.JPG", "A0003.JPG"]', command)

    def test_no_prefilter_means_the_command_it_always_was(self):
        self.jobs.add(str(self.photos))
        job = self.queued()
        self.assertEqual(job["only_photos"], [])
        self.assertIn("only_photos=[]", self.jobs._command(job))


if __name__ == "__main__":
    unittest.main()
