"""Rating a shoot yourself, without buying a model's opinion first."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui.report import load_report  # noqa: E402
from opencull_gui.shortlist import (  # noqa: E402
    ASSESSMENT_FIELDS,
    UNRATED_AXIS,
    UNRATED_RATIONALE,
    UNRATED_TIER,
    ShortlistError,
    load_shortlist,
    rated_by_hand,
    write_manual_shortlist,
)
from tests.test_qt_develop import NAMES, build_shoot  # noqa: E402


class ManualShortlistTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.report_path, self.photos = build_shoot(self.root)
        self.report = load_report(self.report_path)
        self.destination = self.root / "by-hand.professional-shortlist.json"
        self.addCleanup(self._temporary.cleanup)

    def write(self, photos=None, **kwargs):
        return write_manual_shortlist(
            self.report, list(photos if photos is not None else NAMES),
            self.destination, **kwargs)

    def load(self, path=None):
        return load_shortlist(path or self.destination, self.report, self.photos)

    def test_what_is_written_is_a_shortlist_the_app_will_open(self):
        self.write()
        self.assertEqual(len(self.load().entries), len(NAMES))

    def test_it_asserts_nothing_about_any_photograph(self):
        self.write()
        for entry in self.load().entries:
            self.assertEqual(entry["score"], 0)
            self.assertEqual(entry["confidence"], 0)
            self.assertEqual(entry["rationale"], UNRATED_RATIONALE)
            self.assertIn("Not assessed", entry["rationale"])
            self.assertEqual(
                set(entry["assessment"].values()), {UNRATED_AXIS})

    def test_every_frame_starts_at_the_middle_of_the_scale(self):
        self.write()
        for entry in self.load().entries:
            self.assertEqual(entry["tier"], UNRATED_TIER)

    def test_the_order_the_cull_left_them_in_is_kept(self):
        self.write()
        self.assertEqual(
            [entry["photo"] for entry in self.load().entries], list(NAMES))

    def test_it_says_no_model_produced_it(self):
        self.write()
        self.assertTrue(rated_by_hand(self.load()))

    def test_an_assessed_shortlist_is_not_mistaken_for_a_hand_written_one(self):
        from tests.test_qt_develop import assess_and_suggest

        path = assess_and_suggest(self.root, self.report_path, self.photos)
        self.assertFalse(rated_by_hand(self.load(path)))

    def test_the_ten_axes_are_present_but_say_nothing(self):
        self.write()
        entry = self.load().entries[0]
        self.assertEqual(tuple(entry["assessment"]), ASSESSMENT_FIELDS)
        self.assertEqual(set(entry["assessment"].values()), {UNRATED_AXIS})

    def test_a_frame_the_report_never_saw_is_left_out(self):
        self.write(photos=[*NAMES, "GHOST.JPG"])
        self.assertEqual(len(self.load().entries), len(NAMES))

    def test_it_belongs_to_the_report_it_was_written_from(self):
        self.write()
        value = json.loads(self.destination.read_text(encoding="utf-8"))
        self.assertEqual(value["source_report_sha256"], self.report.sha256)

    def test_a_shortlist_from_another_cull_is_still_refused(self):
        # build_shoot is deterministic, so the second report has to actually
        # differ for this to be testing the guard rather than the fixture.
        self.write()
        other = self.root / "other"
        other_report_path, other_photos = build_shoot(other)
        value = json.loads(other_report_path.read_text(encoding="utf-8"))
        value["warnings"] = ["a different cull"]
        other_report_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ShortlistError):
            load_shortlist(
                self.destination, load_report(other_report_path), other_photos)

    def test_the_stance_it_was_written_under_is_recorded(self):
        self.write(stance="artistic")
        value = json.loads(self.destination.read_text(encoding="utf-8"))
        self.assertEqual(value["stance"], "artistic")

    def test_writing_it_twice_replaces_it_atomically(self):
        self.write()
        self.write(photos=[NAMES[0]])
        self.assertEqual(len(self.load().entries), 1)
        self.assertFalse(
            list(self.destination.parent.glob(".*.tmp")),
            "a temporary file was left behind")


if __name__ == "__main__":
    unittest.main()
