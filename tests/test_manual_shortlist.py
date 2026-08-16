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
    ensure_shortlist,
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

    def test_a_by_hand_shortlist_registers_on_the_project(self):
        """The bug as met: "Rate them myself" wrote the file, the
        assessment page opened on it, and the phase bar kept AI editing
        blocked -- because the catalog derives "shortlist available"
        from the manifest, and only the paid job ever registered."""
        from opencull_gui.project import (
            load_or_create_folder_project,
            load_project,
        )

        project_path, _manifest = load_or_create_folder_project(
            self.photos, self.report.path.stem)
        self.write(project_path=project_path)
        registered = load_project(project_path).get(
            "artifacts", {}).get("shortlist") or []
        self.assertEqual(
            [Path(item["path"]).name for item in registered],
            [self.destination.name])

    def test_ensure_shortlist_lays_one_out_only_where_none_exists(self):
        """AI editing's silent door: written once, never overwritten --
        a paid assessment that later lands must not be replaced by an
        unrated one on the next visit."""
        from opencull_gui.project import load_or_create_folder_project

        project_path, _manifest = load_or_create_folder_project(
            self.photos, self.report.path.stem)
        first = ensure_shortlist(self.report, list(NAMES),
                                 self.destination, project_path=project_path)
        self.assertTrue(first.is_file())
        self.assertTrue(rated_by_hand(self.load()))
        stamp = self.destination.stat().st_mtime_ns
        ensure_shortlist(self.report, list(NAMES[:1]),
                         self.destination, project_path=project_path)
        self.assertEqual(self.destination.stat().st_mtime_ns, stamp)

    def test_the_number_is_the_shooting_position_not_the_file_name(self):
        """A "rank" that was really cull order read 299, 1, 300, 2 down
        a time-ordered list once the counter had wrapped. On a by-hand
        shortlist the number is the order the shutter went."""
        from PIL import Image

        def stamp(name: str, when: str) -> None:
            path = self.photos / name
            image = Image.open(path)
            exif = image.getexif()
            exif[0x9003] = when
            image.save(path, exif=exif.tobytes())

        # Names say A < B < C; the clock says C first, then A, then B.
        stamp("C.JPG", "2026:08:12 10:00:00")
        stamp("A.JPG", "2026:08:12 10:05:00")
        stamp("B.JPG", "2026:08:12 10:10:00")
        self.write(photos_root=self.photos)
        ranked = {entry["photo"]: entry["rank"]
                  for entry in self.load().entries}
        self.assertEqual(ranked, {"C.JPG": 1, "A.JPG": 2, "B.JPG": 3})

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
