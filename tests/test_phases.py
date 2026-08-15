"""The phases of a shoot: what has happened, and what can be entered now."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui import phases  # noqa: E402


def project(**changes) -> dict:
    value = {
        "id": "abc", "name": "A shoot", "available": True,
        "report_available": False, "shortlist_available": False,
        "culling": {}, "assessment": {}, "project": "",
    }
    value.update(changes)
    return value


def manifest(**artifacts) -> dict:
    return {"artifacts": {key: [{}] * count for key, count in artifacts.items()}}


def state(plan: list[dict], key: str) -> str:
    return next(item["state"] for item in plan if item["id"] == key)


def reason(plan: list[dict], key: str) -> str:
    return next(item["reason"] for item in plan if item["id"] == key)


class OrderTests(unittest.TestCase):
    def test_every_phase_is_described_in_order(self):
        plan = phases.plan(project(), manifest={})
        self.assertEqual([item["id"] for item in plan], list(phases.ORDER))
        self.assertEqual([item["number"] for item in plan],
                         list(range(1, len(phases.ORDER) + 1)))

    def test_every_phase_has_a_title_and_a_purpose(self):
        for item in phases.plan(project(), manifest={}):
            self.assertTrue(item["title"])
            self.assertTrue(item["purpose"].endswith("."))

    def test_a_blocked_phase_always_says_why(self):
        for item in phases.plan(project(), manifest={}):
            if item["state"] == "blocked":
                self.assertTrue(item["reason"], item["id"])


class CullTests(unittest.TestCase):
    def test_a_fresh_folder_can_be_culled(self):
        self.assertEqual(state(phases.plan(project(), manifest={}), phases.CULL),
                         "ready")

    def test_a_culled_folder_reports_the_cull_as_done(self):
        plan = phases.plan(project(report_available=True), manifest={})
        self.assertEqual(state(plan, phases.CULL), "done")

    def test_a_cull_in_flight_is_running_and_stays_open(self):
        plan = phases.plan(
            project(culling={"status": "running"}), manifest={})
        self.assertEqual(state(plan, phases.CULL), "running")
        self.assertIn(phases.CULL, phases.openable(plan))


class AssessmentTests(unittest.TestCase):
    def test_assessment_is_blocked_with_no_selection_at_all(self):
        plan = phases.plan(project(), manifest={})
        self.assertEqual(state(plan, phases.ASSESSMENT), "blocked")
        self.assertIn("has none yet", reason(plan, phases.ASSESSMENT))

    def test_a_cull_opens_the_assessment(self):
        plan = phases.plan(project(report_available=True), manifest={})
        self.assertEqual(state(plan, phases.ASSESSMENT), "ready")

    def test_a_selection_without_a_cull_also_opens_it(self):
        # Opening a folder writes an everything-included selection, so a cull
        # is not the only way to have one.
        plan = phases.plan(
            project(report_available=True), culled=False, manifest={})
        self.assertEqual(state(plan, phases.ASSESSMENT), "ready")

    def test_assessing_an_unculled_folder_says_what_it_will_read(self):
        detail = next(
            item["detail"] for item in phases.plan(
                project(report_available=True), culled=False, manifest={})
            if item["id"] == phases.ASSESSMENT)
        self.assertIn("every frame in the folder", detail)
        self.assertIn("model call", detail)

    def test_assessing_a_culled_folder_reads_only_the_keepers(self):
        detail = next(
            item["detail"] for item in phases.plan(
                project(report_available=True), culled=True, manifest={})
            if item["id"] == phases.ASSESSMENT)
        self.assertIn("kept", detail)

    def test_an_assessment_says_how_many_are_marked(self):
        plan = phases.plan(
            project(report_available=True, shortlist_available=True),
            marked=3, manifest={})
        self.assertEqual(state(plan, phases.ASSESSMENT), "done")
        self.assertIn("3 marked", next(
            item["detail"] for item in plan if item["id"] == phases.ASSESSMENT))


class ProfileTests(unittest.TestCase):
    def test_ai_editing_is_never_gated_on_an_assessment(self):
        """It needs frames to name, not a model's opinion of them; the
        phase lays out an unrated shortlist for itself on the way in.
        "Assess first" had read as "pay first"."""
        plan = phases.plan(project(report_available=True), manifest={})
        self.assertEqual(state(plan, phases.SUGGESTIONS), "ready")
        self.assertIn("Not assessed", plan[
            [item["id"] for item in plan].index(phases.SUGGESTIONS)]["detail"])

    def test_the_profile_is_never_blocked_because_it_is_not_the_shoots(self):
        plan = phases.plan(project(available=False), manifest={})
        self.assertNotEqual(state(plan, phases.PROFILE), "blocked")

    def test_a_selected_profile_reads_as_done(self):
        plan = phases.plan(project(), profile_selected=True, manifest={})
        self.assertEqual(state(plan, phases.PROFILE), "done")

    def test_without_one_it_says_what_is_lost(self):
        detail = next(item["detail"] for item in phases.plan(project(), manifest={})
                      if item["id"] == phases.PROFILE)
        self.assertIn("personal treatment", detail)


class SuggestionTests(unittest.TestCase):
    def test_suggestions_never_need_an_assessment(self):
        """The old contract, inverted on purpose: the phase lays out an
        unrated shortlist for itself, so nobody pays to be allowed in."""
        plan = phases.plan(project(report_available=True), manifest={})
        self.assertEqual(state(plan, phases.SUGGESTIONS), "ready")

    def test_an_unread_assessment_does_not_block_suggestions(self):
        # marked=None means nobody has looked, which is not the same as
        # having looked and marked nothing.
        plan = phases.plan(
            project(report_available=True, shortlist_available=True),
            marked=None, manifest={})
        self.assertEqual(state(plan, phases.SUGGESTIONS), "ready")

    def test_marking_nothing_opens_anyway_and_says_what_is_missing(self):
        # The frames are chosen on the page itself, so an empty selection
        # is a thing to do there rather than a locked door.
        plan = phases.plan(
            project(report_available=True, shortlist_available=True),
            marked=0, manifest={})
        self.assertEqual(state(plan, phases.SUGGESTIONS), "ready")
        entry = next(
            item for item in plan if item["id"] == phases.SUGGESTIONS)
        self.assertIn("Nothing marked yet", entry["detail"])

    def test_asked_rounds_are_counted(self):
        plan = phases.plan(
            project(report_available=True, shortlist_available=True),
            marked=2, manifest=manifest(edit_directions=2))
        self.assertEqual(state(plan, phases.SUGGESTIONS), "done")


class DevelopmentTests(unittest.TestCase):
    def test_development_is_never_gated_on_the_ai(self):
        plan = phases.plan(project(), manifest={})
        self.assertEqual(state(plan, phases.DEVELOPMENT), "ready")

    def test_it_says_the_baseline_needs_no_suggestions(self):
        detail = next(item["detail"] for item in phases.plan(project(), manifest={})
                      if item["id"] == phases.DEVELOPMENT)
        self.assertIn("baseline", detail)

    def test_renders_make_it_done(self):
        plan = phases.plan(project(), manifest=manifest(renders=4))
        self.assertEqual(state(plan, phases.DEVELOPMENT), "done")


class ExportTests(unittest.TestCase):
    def test_export_needs_something_rendered(self):
        plan = phases.plan(project(), manifest={})
        self.assertEqual(state(plan, phases.EXPORT), "blocked")
        self.assertIn("Develop a frame first", reason(plan, phases.EXPORT))

    def test_a_render_opens_the_export(self):
        plan = phases.plan(project(), manifest=manifest(renders=1))
        self.assertEqual(state(plan, phases.EXPORT), "ready")

    def test_deliveries_are_counted(self):
        plan = phases.plan(
            project(), manifest=manifest(renders=1, exports=2))
        self.assertEqual(state(plan, phases.EXPORT), "done")


class OfflineTests(unittest.TestCase):
    def test_a_missing_folder_blocks_the_shoots_phases(self):
        plan = phases.plan(project(available=False), manifest={})
        for key in (phases.CULL, phases.ASSESSMENT, phases.DEVELOPMENT):
            self.assertEqual(state(plan, key), "blocked", key)
            self.assertIn("not on disk", reason(plan, key))

    def test_what_already_happened_is_still_reported(self):
        plan = phases.plan(
            project(available=False, report_available=True), manifest={})
        self.assertEqual(state(plan, phases.CULL), "done")


class ManifestTests(unittest.TestCase):
    def test_an_unreadable_manifest_reads_as_nothing_done(self):
        self.assertEqual(
            phases.manifest_for(project(project="/nowhere/project.json")), {})

    def test_openable_excludes_only_the_blocked(self):
        plan = phases.plan(project(), manifest={})
        blocked = {item["id"] for item in plan if item["state"] == "blocked"}
        self.assertEqual(
            phases.openable(plan), set(phases.ORDER) - blocked)


if __name__ == "__main__":
    unittest.main()


class DebriefTests(unittest.TestCase):
    def test_the_debrief_needs_judgements_to_read(self):
        plan = phases.plan(project(report_available=True), manifest={})
        self.assertEqual(state(plan, phases.DEBRIEF), "blocked")
        self.assertIn("Assess first", reason(plan, phases.DEBRIEF))

    def test_an_assessed_shoot_can_be_read_back(self):
        plan = phases.plan(
            project(report_available=True, shortlist_available=True),
            marked=1, manifest={})
        self.assertEqual(state(plan, phases.DEBRIEF), "ready")

    def test_it_is_never_done_because_it_can_always_be_reread(self):
        plan = phases.plan(
            project(report_available=True, shortlist_available=True),
            marked=1,
            manifest=manifest(edit_directions=1, renders=1, exports=1))
        self.assertEqual(state(plan, phases.DEBRIEF), "ready")
