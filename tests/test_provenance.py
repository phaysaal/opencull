"""Decision archaeology: the recorded story of one frame."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui.provenance import frame_story  # noqa: E402
from opencull_gui.shortlist import write_manual_shortlist  # noqa: E402
from opencull_gui.shortlist_reviews import (  # noqa: E402
    default_shortlist_review_path,
)
from tests.test_qt_develop import NAMES, assess_and_suggest, build_shoot  # noqa: E402


def bench_for(root: Path):
    from opencull_qt.bench import Bench

    report_path, photos = build_shoot(root)
    project = {"id": "p1", "name": "A", "photos": str(photos),
               "report": str(report_path), "available": True,
               "report_available": True}
    return Bench(project, root / "cache", report_path), report_path, photos


def labels(story) -> list[str]:
    return [chapter["label"] for chapter in story]


def text(story) -> str:
    return "\n".join(
        line for chapter in story for line in chapter["lines"])


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class FrameStoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.addCleanup(self._temporary.cleanup)

    def test_a_frame_nobody_has_touched_still_has_a_story(self):
        bench, _report, _photos = bench_for(self.root)
        story = frame_story(bench, NAMES[0])
        self.assertIn("CULLED", labels(story))
        self.assertIn("NOT ASSESSED", labels(story))
        self.assertIn("sharpest", text(story))

    def test_a_frame_outside_the_shoot_says_so(self):
        bench, _report, _photos = bench_for(self.root)
        story = frame_story(bench, "GHOST.JPG")
        self.assertEqual(labels(story), ["NOT IN THIS SHOOT"])

    def test_an_overruled_cull_review_is_part_of_the_story(self):
        bench, _report, _photos = bench_for(self.root)
        # The AI kept A; the photographer kept B instead.
        bench.reviews.update_cluster(
            "group-0001", [NAMES[1]], "the eyes are open in this one",
            True, 0)
        story = frame_story(bench, NAMES[1])
        self.assertIn("YOUR CULL REVIEW", labels(story))
        self.assertIn("That overruled the proposal.", text(story))
        self.assertIn("the eyes are open in this one", text(story))

    def test_an_assessed_and_marked_frame_tells_the_whole_chain(self):
        bench, report_path, photos = bench_for(self.root)
        shortlist_path = assess_and_suggest(
            self.root, report_path, photos, marked=(NAMES[0],))
        default_shortlist_review_path(shortlist_path).unlink(missing_ok=True)
        bench.shortlist_reviews.update(
            NAMES[0], "reject", False, "not for this client", True,
            bench.shortlist_reviews.public_state()["revision"],
            interesting=True)
        story = frame_story(bench, NAMES[0])
        shown = text(story)
        self.assertIn("ASSESSED", labels(story))
        self.assertIn("YOUR RATING", labels(story))
        self.assertIn("SUGGESTED", labels(story))
        self.assertIn("You set REJECT where the assessment said STRONG",
                      shown)
        self.assertIn("Marked worth developing.", shown)
        self.assertIn("not for this client", shown)
        self.assertIn("Standard treatment", shown)

    def test_a_hand_rated_shoot_says_no_model_was_asked(self):
        bench, _report, _photos = bench_for(self.root)
        write_manual_shortlist(
            bench.report, bench.selection(), bench.shortlist_path)
        story = frame_story(bench, bench.selection()[0])
        self.assertIn("LAID OUT FOR RATING", labels(story))
        self.assertIn("No model was asked", text(story))

    def test_a_scene_shared_answer_names_its_source(self):
        from opencull_gui import scenes

        bench, report_path, photos = bench_for(self.root)
        shortlist_path = assess_and_suggest(
            self.root, report_path, photos, marked=(NAMES[0],))
        default_shortlist_review_path(shortlist_path).unlink(missing_ok=True)
        for photo in (NAMES[0], NAMES[1]):
            bench.shortlist_reviews.update(
                photo, "strong", False, "", True,
                bench.shortlist_reviews.public_state()["revision"],
                interesting=True)
        scenes.write_plan(
            bench.directions.recipes, bench.shortlist_path,
            bench.shortlist_reviews.public_state()["revision"],
            [{"id": "scene-0001", "photos": [NAMES[0], NAMES[1]],
              "representative": NAMES[0]}])
        story = frame_story(bench, NAMES[1])
        self.assertIn(f"Shared from {NAMES[0]}", text(story))

    def test_the_dialog_renders_every_chapter(self):
        from PySide6.QtWidgets import QLabel

        from opencull_qt.provenance import ProvenanceDialog

        bench, _report, _photos = bench_for(self.root)
        dialog = ProvenanceDialog(NAMES[0], frame_story(bench, NAMES[0]))
        self.addCleanup(dialog.deleteLater)
        shown = "\n".join(
            label.text() for label in dialog.findChildren(QLabel))
        self.assertIn("CULLED", shown)
        self.assertIn("nothing was computed", shown.lower())


if __name__ == "__main__":
    unittest.main()
