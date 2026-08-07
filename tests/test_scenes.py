"""Scenes: one treatment shared across frames that honestly share it."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

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
        self.root = Path(self._temporary.name)
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
