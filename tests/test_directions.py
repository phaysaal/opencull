"""Edit directions: versioning, overlay, and what counts as already answered."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui.directions import DirectionsIndex  # noqa: E402

PERSONAL = {
    "personal_title": "Personal", "personal_intent": "as you would",
    "personal_instructions": "warm it", "personal_recipe": "temperature +200",
}


class FakeShortlist:
    def __init__(self, path: Path):
        self.path = path


class FakeReviews:
    def __init__(self, revision: int, interesting: list[str]):
        self.revision = revision
        self.interesting = interesting

    def public_state(self) -> dict:
        return {
            "revision": self.revision,
            "entries": {
                photo: {"interesting": True} for photo in self.interesting},
        }


class DirectionsTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.recipes = self.root / "Recipes"
        self.recipes.mkdir(parents=True)
        self.shortlist_path = self.root / "shoot.professional-shortlist.json"
        self.shortlist_path.write_text('{"entries": []}', encoding="utf-8")
        self.sha = hashlib.sha256(
            self.shortlist_path.read_bytes()).hexdigest()
        self.addCleanup(self._temporary.cleanup)

    def index(self, revision=1, interesting=("A.JPG",), style=""):
        return DirectionsIndex(
            FakeShortlist(self.shortlist_path),
            FakeReviews(revision, list(interesting)),
            self.recipes, style_profile=lambda: style)

    def write_directions(self, name, revision, photos, sha=None, **extra):
        path = self.recipes / name
        path.write_text(json.dumps({
            "format": "opencull-edit-directions-v1",
            "shortlist_sha256": self.sha if sha is None else sha,
            "review_revision": revision,
            "entries": [
                {"photo": photo, "standard_title": f"Standard for {photo}",
                 "standard_recipe": "exposure +0.2", **extra}
                for photo in photos
            ],
        }), encoding="utf-8")
        return path

    def test_a_new_set_is_named_for_the_review_it_answers(self):
        self.assertEqual(
            self.index(revision=4).next_path(4).name,
            "shoot.professional-shortlist.edit-directions-r4.json")

    def test_a_second_run_at_the_same_revision_does_not_replace_the_first(self):
        index = self.index(revision=2)
        first = index.next_path(2)
        first.write_text("{}", encoding="utf-8")
        self.assertEqual(index.next_path(2).name, f"{first.stem}-v2.json")

    def test_directions_for_another_shortlist_are_not_this_folder_s(self):
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.json", 1,
            ["A.JPG"], sha="somebody-elses-shortlist")
        self.assertFalse(self.index().payload()["available"])

    def test_the_default_path_is_offered_even_when_there_is_nothing_yet(self):
        payload = self.index().payload()
        self.assertFalse(payload["available"])
        self.assertTrue(payload["default_path"].endswith(
            "edit-directions-r1.json"))
        self.assertEqual(payload["processed_photos"], [])

    def test_directions_are_read_back_for_the_frames_that_were_marked(self):
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.json", 1,
            ["A.JPG", "B.JPG"])
        payload = self.index(interesting=["A.JPG"]).payload()
        self.assertTrue(payload["available"])
        self.assertEqual(
            [entry["photo"] for entry in payload["directions"]["entries"]],
            ["A.JPG"])
        self.assertEqual(payload["processed_photos"], ["A.JPG"])

    def test_redoing_one_frame_does_not_hide_the_rest_of_the_batch(self):
        import os
        import time

        batch = self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.json", 1,
            ["A.JPG", "B.JPG", "C.JPG"])
        time.sleep(0.01)
        single = self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1-v2.json", 1,
            ["B.JPG"])
        # The targeted rerun is the newer file.
        os.utime(single, (time.time() + 1, time.time() + 1))
        os.utime(batch, (time.time(), time.time()))

        payload = self.index(
            interesting=["A.JPG", "B.JPG", "C.JPG"]).payload()
        entries = {
            entry["photo"]: entry
            for entry in payload["directions"]["entries"]}
        self.assertEqual(sorted(entries), ["A.JPG", "B.JPG", "C.JPG"])
        self.assertEqual(len(payload["directions"]["assembled_from"]), 2)

    def test_a_frame_marked_but_never_answered_is_reported_missing(self):
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.json", 1,
            ["A.JPG"])
        payload = self.index(interesting=["A.JPG", "B.JPG"]).payload()
        self.assertEqual(payload["missing_photos"], ["B.JPG"])
        self.assertTrue(payload["partial"])

    def test_directions_answering_an_older_selection_are_marked_partial(self):
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.json", 1,
            ["A.JPG"])
        # The photographer has since changed their marks, so the review moved on.
        payload = self.index(revision=2, interesting=["A.JPG"]).payload()
        self.assertTrue(payload["available"])
        self.assertTrue(payload["partial"])

    def test_a_style_profile_makes_a_personal_treatment_required(self):
        style = self.root / "style.json"
        style.write_text('{"profile": true}', encoding="utf-8")
        # Directions produced before the profile existed have no personal
        # treatment, so with a profile selected the frame is not done.
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.json", 1,
            ["A.JPG"], style_profile_sha256="")
        payload = self.index(style=str(style)).payload()
        self.assertEqual(payload["processed_photos"], [])
        self.assertEqual(payload["missing_photos"], ["A.JPG"])

    def test_a_personal_treatment_from_the_current_profile_counts_as_done(self):
        style = self.root / "style.json"
        style.write_text('{"profile": true}', encoding="utf-8")
        digest = hashlib.sha256(style.read_bytes()).hexdigest()
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.json", 1,
            ["A.JPG"], style_profile_sha256=digest, **PERSONAL)
        payload = self.index(style=str(style)).payload()
        self.assertEqual(payload["processed_photos"], ["A.JPG"])
        self.assertEqual(payload["missing_photos"], [])

    def test_the_assembled_result_is_written_once_and_reused(self):
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.json", 1,
            ["A.JPG"])
        index = self.index()
        first = Path(index.payload()["path"])
        self.assertTrue(first.is_file())
        stamp = first.stat().st_mtime_ns
        self.assertEqual(Path(index.payload()["path"]), first)
        self.assertEqual(first.stat().st_mtime_ns, stamp)

    def test_a_directions_file_that_cannot_be_parsed_is_skipped(self):
        (self.recipes /
         "shoot.professional-shortlist.edit-directions-r1.json").write_text(
            "{not json", encoding="utf-8")
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1-v2.json", 1,
            ["A.JPG"])
        payload = self.index().payload()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["processed_photos"], ["A.JPG"])

    def test_a_checkpoint_is_not_mistaken_for_a_result(self):
        self.write_directions(
            "shoot.professional-shortlist.edit-directions-r1.checkpoint.json",
            1, ["A.JPG"])
        self.assertFalse(self.index().payload()["available"])


if __name__ == "__main__":
    unittest.main()
