"""Scenes: one treatment shared across frames that honestly share it."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, UnidentifiedImageError  # noqa: E402

from opencull_gui import scenes  # noqa: E402
from opencull_gui.directions import DirectionsIndex  # noqa: E402
from opencull_gui.project import ensure_project_layout  # noqa: E402
from opencull_gui.report import load_report  # noqa: E402
from opencull_gui.shortlist import load_shortlist  # noqa: E402
from opencull_gui.shortlist_reviews import (  # noqa: E402
    ShortlistReviewStore,
    default_shortlist_review_path,
)
from tests.test_qt_develop import NAMES, assess_and_suggest, build_shoot  # noqa: E402


def entry(photo: str, cluster: str, rank: int = 1) -> dict:
    return {"photo": photo, "cluster_id": cluster, "rank": rank}


def timed_photo(path: Path, when: str) -> None:
    exif = Image.Exif()
    exif[36867] = when
    Image.new("RGB", (60, 40), (30, 60, 90)).save(path, exif=exif)


class GroupingTests(unittest.TestCase):
    def photos(self, groups) -> list[list[str]]:
        return [group["photos"] for group in groups]

    def test_adjacent_frames_share_a_scene(self):
        groups = scenes.scene_groups([
            entry("DSCF0001.JPG", "c1"), entry("DSCF0002.JPG", "c2"),
            entry("DSCF0003.JPG", "c3")])
        self.assertEqual(self.photos(groups), [
            ["DSCF0001.JPG", "DSCF0002.JPG", "DSCF0003.JPG"]])

    def test_a_long_silence_of_the_shutter_breaks_the_scene(self):
        groups = scenes.scene_groups([
            entry("DSCF0001.JPG", "c1"), entry("DSCF0100.JPG", "c2")])
        self.assertEqual(len(groups), 2)

    def test_frames_of_one_cluster_never_separate(self):
        # Alike enough to be duplicates is alike enough to share an edit,
        # whatever the counter says happened in between.
        groups = scenes.scene_groups([
            entry("DSCF0001.JPG", "c1"), entry("DSCF0100.JPG", "c1")])
        self.assertEqual(len(groups), 1)

    def test_the_clock_outranks_the_frame_counter(self):
        root = Path(tempfile.mkdtemp())
        timed_photo(root / "A0001.JPG", "2026:01:01 10:00:00")
        timed_photo(root / "A0002.JPG", "2026:01:01 13:00:00")
        groups = scenes.scene_groups(
            [entry("A0001.JPG", "c1"), entry("A0002.JPG", "c2")], root)
        # Adjacent on the counter, three hours apart on the clock.
        self.assertEqual(len(groups), 2)

    def test_a_close_clock_holds_a_scene_across_a_counter_gap(self):
        root = Path(tempfile.mkdtemp())
        timed_photo(root / "A0001.JPG", "2026:01:01 10:00:00")
        timed_photo(root / "A0500.JPG", "2026:01:01 10:00:30")
        groups = scenes.scene_groups(
            [entry("A0001.JPG", "c1"), entry("A0500.JPG", "c2")], root)
        self.assertEqual(len(groups), 1)

    def test_no_evidence_breaks_the_scene_rather_than_guessing(self):
        groups = scenes.scene_groups([
            entry("sunrise.JPG", "c1"), entry("harbour.JPG", "c2")])
        self.assertEqual(len(groups), 2)

    def test_scenes_come_back_in_shooting_order(self):
        groups = scenes.scene_groups([
            entry("DSCF0100.JPG", "c2"), entry("DSCF0001.JPG", "c1")])
        self.assertEqual(self.photos(groups),
                         [["DSCF0001.JPG"], ["DSCF0100.JPG"]])


class RawClockTests(unittest.TestCase):
    """A raw file is not an image PIL can open, but it carries a clock."""

    def raw(self, root: Path, name: str, when: str) -> Path:
        """A file shaped like a raw: a header, then an embedded JPEG."""
        jpeg = root / "preview.jpg"
        timed_photo(jpeg, when)
        path = root / name
        path.write_bytes(b"FUJIFILMCCD-RAW 0201FF12345678"
                         + b"\x00" * 512 + jpeg.read_bytes())
        jpeg.unlink()
        return path

    def test_the_clock_inside_a_raw_file_is_read(self):
        root = Path(tempfile.mkdtemp())
        path = self.raw(root, "DSCF0001.RAF", "2026:01:01 10:00:00")
        # PIL cannot open this at all, which is exactly why the time was
        # invisible before.
        with self.assertRaises(UnidentifiedImageError):
            Image.open(path)
        self.assertEqual(
            datetime.fromtimestamp(scenes.capture_time(path)).strftime(
                "%Y-%m-%d %H:%M"),
            "2026-01-01 10:00")

    def test_raw_frames_hours_apart_are_different_scenes(self):
        # The defect this fixes: with no readable clock these fell back to
        # the frame counter, and a whole day became one scene.
        root = Path(tempfile.mkdtemp())
        self.raw(root, "DSCF0001.RAF", "2026:01:01 10:00:00")
        self.raw(root, "DSCF0002.RAF", "2026:01:01 16:00:00")
        groups = scenes.scene_groups(
            [entry("DSCF0001.RAF", "c1"), entry("DSCF0002.RAF", "c2")], root)
        self.assertEqual(len(groups), 2)

    def test_a_file_with_no_clock_anywhere_is_simply_unknown(self):
        root = Path(tempfile.mkdtemp())
        path = root / "DSCF0003.RAF"
        path.write_bytes(b"not a photograph at all")
        self.assertIsNone(scenes.capture_time(path))


class SmallHeadTests(unittest.TestCase):
    """The marker sits near the start, so a small head finds it -- and a
    large read is only the fallback for a file that buries it deeper.
    This is the read that a gigabyte of pointless disk shrank to."""

    def raw(self, root: Path, name: str, when: str, pad: int = 512) -> Path:
        jpeg = root / "preview.jpg"
        timed_photo(jpeg, when)
        path = root / name
        path.write_bytes(b"FUJIFILMCCD-RAW 0201FF12345678"
                         + b"\x00" * pad + jpeg.read_bytes())
        jpeg.unlink()
        return path

    def test_the_small_head_alone_reads_a_normal_raw(self):
        # With the large read shrunk to the small one, a normal frame is
        # still read: proof the marker was found inside the small head,
        # because the fallback now offers nothing more.
        root = Path(tempfile.mkdtemp())
        path = self.raw(root, "NEAR.RAF", "2026:01:01 08:00:00")
        with mock.patch.object(scenes, "_LARGE_HEAD", scenes._SMALL_HEAD):
            found = scenes._embedded_capture_time(path)
        self.assertIsNotNone(found)
        self.assertEqual(
            datetime.fromtimestamp(found).strftime("%H:%M"), "08:00")

    def test_the_large_head_fallback_finds_exif_past_the_small_head(self):
        # A camera that buries the preview beyond the small head still
        # works: the small read misses, the large read finds it.
        root = Path(tempfile.mkdtemp())
        path = self.raw(root, "DEEP.RAF", "2026:01:01 09:00:00",
                        pad=scenes._SMALL_HEAD + 4096)
        # The small head alone cannot see it...
        with mock.patch.object(scenes, "_LARGE_HEAD", scenes._SMALL_HEAD):
            self.assertIsNone(scenes._embedded_capture_time(path))
        # ...but with the fallback restored, it does.
        found = scenes.capture_time(path)
        self.assertIsNotNone(found)
        self.assertEqual(
            datetime.fromtimestamp(found).strftime("%H:%M"), "09:00")


class CaptureTimesMapTests(unittest.TestCase):
    """The persisted map: opened once, read from disk forever after,
    and touched again only where a file actually changed."""

    def raw(self, root: Path, name: str, when: str) -> Path:
        jpeg = root / "preview.jpg"
        timed_photo(jpeg, when)
        path = root / name
        path.write_bytes(b"FUJIFILMCCD-RAW 0201FF12345678"
                         + b"\x00" * 512 + jpeg.read_bytes())
        jpeg.unlink()
        return path

    def hhmm(self, stamp) -> str:
        return datetime.fromtimestamp(stamp).strftime("%H:%M")

    def test_the_map_is_written_beside_the_photographs(self):
        root = Path(tempfile.mkdtemp())
        self.raw(root, "DSCF0001.RAF", "2026:01:01 10:00:00")
        times = scenes.capture_times(root, ["DSCF0001.RAF"])
        self.assertEqual(self.hhmm(times["DSCF0001.RAF"]), "10:00")
        sidecar = root / ".darkimiya" / "capture-times.json"
        self.assertTrue(sidecar.is_file())
        stored = json.loads(sidecar.read_text())
        self.assertEqual(stored["format"], scenes.CAPTURE_TIMES_FORMAT)
        self.assertIn("DSCF0001.RAF", stored["frames"])

    def test_a_reopen_answers_from_the_map_without_reading_the_file(self):
        root = Path(tempfile.mkdtemp())
        self.raw(root, "DSCF0001.RAF", "2026:01:01 10:00:00")
        first = scenes.capture_times(root, ["DSCF0001.RAF"])
        # A second open must not scan the file again: make the scanner
        # explode, and prove it is never reached.
        with mock.patch.object(scenes, "_capture_time",
                               side_effect=AssertionError("rescanned")):
            second = scenes.capture_times(root, ["DSCF0001.RAF"])
        self.assertEqual(second, first)

    def test_a_changed_file_is_rescanned(self):
        root = Path(tempfile.mkdtemp())
        path = self.raw(root, "DSCF0001.RAF", "2026:01:01 10:00:00")
        scenes.capture_times(root, ["DSCF0001.RAF"])
        # Rewrite with a later clock, and move the mtime so the change is
        # visible -- size alone can match when only the timestamp differs.
        self.raw(root, "DSCF0001.RAF", "2026:01:01 18:00:00")
        moved = path.stat().st_mtime_ns + 10 ** 9
        os.utime(path, ns=(moved, moved))
        again = scenes.capture_times(root, ["DSCF0001.RAF"])
        self.assertEqual(self.hhmm(again["DSCF0001.RAF"]), "18:00")

    def test_a_partial_reopen_keeps_the_other_frames_cached(self):
        root = Path(tempfile.mkdtemp())
        self.raw(root, "A.RAF", "2026:01:01 10:00:00")
        self.raw(root, "B.RAF", "2026:01:01 11:00:00")
        scenes.capture_times(root, ["A.RAF", "B.RAF"])
        # A later view asks only about A; B's cached answer must survive.
        scenes.capture_times(root, ["A.RAF"])
        stored = json.loads(
            (root / ".darkimiya" / "capture-times.json").read_text())
        self.assertIn("B.RAF", stored["frames"])
        self.assertIn("A.RAF", stored["frames"])

    def test_a_missing_file_is_none_not_a_crash(self):
        root = Path(tempfile.mkdtemp())
        got = scenes.capture_times(root, ["ghost.RAF"])
        self.assertIsNone(got["ghost.RAF"])


class PlanTests(unittest.TestCase):
    class Shortlist:
        def __init__(self, entries):
            self.entries = entries

    def test_the_best_ranked_target_speaks_for_its_scene(self):
        shortlist = self.Shortlist([
            entry("DSCF0001.JPG", "c1", rank=5),
            entry("DSCF0002.JPG", "c2", rank=2),
            entry("DSCF0003.JPG", "c3", rank=9)])
        plan = scenes.plan_for(
            shortlist, ["DSCF0001.JPG", "DSCF0002.JPG", "DSCF0003.JPG"])
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["representative"], "DSCF0002.JPG")

    def test_the_plan_finds_the_photographs_without_being_told(self):
        """The index knows where its photographs are; the caller forgot.

        Every scene on the suggestion path was computed from filenames
        because one call site omitted the folder. The default closes that
        hole rather than trusting the next caller to remember.
        """
        root = Path(tempfile.mkdtemp())
        timed_photo(root / "A0001.JPG", "2026:01:01 10:00:00")
        timed_photo(root / "A0002.JPG", "2026:01:01 16:00:00")

        class Assets:
            def __init__(self, root):
                self.root = root

        shortlist = self.Shortlist([
            entry("A0001.JPG", "c1", rank=1),
            entry("A0002.JPG", "c2", rank=2)])
        shortlist.assets = Assets(root)
        plan = scenes.plan_for(shortlist, ["A0001.JPG", "A0002.JPG"])
        self.assertEqual(len(plan), 2)

    def test_frames_nobody_asked_about_stay_out_of_the_plan(self):
        shortlist = self.Shortlist([
            entry("DSCF0001.JPG", "c1", rank=1),
            entry("DSCF0002.JPG", "c2", rank=2)])
        plan = scenes.plan_for(shortlist, ["DSCF0002.JPG"])
        self.assertEqual(plan[0]["photos"], ["DSCF0002.JPG"])


class DerivationTests(unittest.TestCase):
    """The answered representative reaches its siblings, with provenance."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.report_path, self.photos = build_shoot(self.root)
        self.addCleanup(self._temporary.cleanup)

    def index(self, marked=(NAMES[0], NAMES[1]), answered=(NAMES[0],)):
        shortlist_path = assess_and_suggest(
            self.root, self.report_path, self.photos, marked=answered)
        report = load_report(self.report_path)
        shortlist = load_shortlist(shortlist_path, report, self.photos)
        default_shortlist_review_path(shortlist_path).unlink(missing_ok=True)
        reviews = ShortlistReviewStore(
            default_shortlist_review_path(shortlist_path), shortlist)
        for photo in marked:
            reviews.update(photo, "strong", False, "", True,
                           reviews.public_state()["revision"],
                           interesting=True)
        recipes = ensure_project_layout(self.photos)["Recipes"]
        return DirectionsIndex(shortlist, reviews, recipes), shortlist

    def plan(self, index, shortlist, photos, representative):
        scenes.write_plan(
            index.recipes, shortlist.path,
            index.reviews.public_state()["revision"],
            [{"id": "scene-0001", "photos": list(photos),
              "representative": representative}])

    def test_the_siblings_inherit_the_representatives_answer(self):
        index, shortlist = self.index()
        self.plan(index, shortlist, (NAMES[0], NAMES[1]), NAMES[0])
        payload = index.payload()
        self.assertIn(NAMES[1], payload["processed_photos"])
        entries = {item["photo"]: item
                   for item in payload["directions"]["entries"]}
        self.assertEqual(entries[NAMES[1]]["standard_title"],
                         entries[NAMES[0]]["standard_title"])

    def test_an_inherited_answer_names_the_frame_that_gave_it(self):
        index, shortlist = self.index()
        self.plan(index, shortlist, (NAMES[0], NAMES[1]), NAMES[0])
        entries = {item["photo"]: item
                   for item in index.payload()["directions"]["entries"]}
        self.assertEqual(entries[NAMES[1]]["derived_from"], NAMES[0])
        self.assertEqual(entries[NAMES[1]]["scene"], "scene-0001")
        self.assertNotIn("derived_from", entries[NAMES[0]])

    def test_the_suggestion_evidence_itself_is_never_edited(self):
        index, shortlist = self.index()
        source = next(index.recipes.glob("*.edit-directions-r1.json"))
        before = source.read_bytes()
        self.plan(index, shortlist, (NAMES[0], NAMES[1]), NAMES[0])
        index.payload()
        self.assertEqual(source.read_bytes(), before)

    def test_deriving_twice_writes_nothing_new(self):
        index, shortlist = self.index()
        self.plan(index, shortlist, (NAMES[0], NAMES[1]), NAMES[0])
        index.payload()
        derived = next(index.recipes.glob("*-scenes.json"))
        stamp = derived.stat().st_mtime_ns
        index.payload()
        self.assertEqual(derived.stat().st_mtime_ns, stamp)

    def test_a_plan_for_another_assessment_authorises_nothing(self):
        index, shortlist = self.index()
        self.plan(index, shortlist, (NAMES[0], NAMES[1]), NAMES[0])
        plan_file = scenes.plan_path(index.recipes, shortlist.path.stem)
        value = json.loads(plan_file.read_text(encoding="utf-8"))
        value["shortlist_sha256"] = "0" * 64
        plan_file.write_text(json.dumps(value), encoding="utf-8")
        index.payload()
        self.assertEqual(list(index.recipes.glob("*-scenes.json")), [])

    def test_an_unanswered_representative_derives_nothing_yet(self):
        index, shortlist = self.index(answered=())
        for stale in index.recipes.glob("*edit-directions*"):
            stale.unlink()
        self.plan(index, shortlist, (NAMES[0], NAMES[1]), NAMES[0])
        payload = index.payload()
        self.assertEqual(payload["processed_photos"], [])

    def test_the_develop_page_offers_the_inherited_treatments(self):
        from opencull_qt.develop import workspace_for

        index, shortlist = self.index()
        self.plan(index, shortlist, (NAMES[0], NAMES[1]), NAMES[0])
        report = load_report(self.report_path)
        workspace = workspace_for(report, self.photos, decoders=set())
        offered = [item["id"] for item in workspace.treatments(NAMES[1])]
        self.assertIn("standard", offered)

    def test_verification_reads_the_shared_promise(self):
        from opencull_qt.develop import workspace_for

        index, shortlist = self.index()
        self.plan(index, shortlist, (NAMES[0], NAMES[1]), NAMES[0])
        report = load_report(self.report_path)
        workspace = workspace_for(report, self.photos, decoders=set())
        self.assertIn("what standard is for",
                      workspace.suggestion_for(NAMES[1], "standard"))


if __name__ == "__main__":
    unittest.main()
