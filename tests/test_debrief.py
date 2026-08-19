"""The debrief: the shoot's own numbers, read back."""

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

from opencull_gui.debrief import aggregate, disagreements, ladder  # noqa: E402
from opencull_gui.shortlist_reviews import (  # noqa: E402
    default_shortlist_review_path,
)
from tests.test_provenance import bench_for  # noqa: E402
from tests.test_qt_develop import NAMES, assess_and_suggest  # noqa: E402


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class AggregateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.addCleanup(self._temporary.cleanup)

    def assessed_bench(self):
        bench, report_path, photos = bench_for(self.root)
        shortlist_path = assess_and_suggest(
            self.root, report_path, photos, marked=(NAMES[0],))
        default_shortlist_review_path(shortlist_path).unlink(missing_ok=True)
        bench.shortlist_reviews.update(
            NAMES[0], "reject", False, "", True,
            bench.shortlist_reviews.public_state()["revision"],
            interesting=True)
        return bench

    def test_an_untouched_shoot_counts_only_what_the_camera_made(self):
        bench, _report, _photos = bench_for(self.root)
        numbers = aggregate(bench)
        self.assertEqual(numbers["total"], len(NAMES))
        self.assertEqual(numbers["assessed"], 0)
        self.assertEqual(numbers["marked"], 0)

    def test_an_assessed_shoot_counts_the_whole_chain(self):
        numbers = aggregate(self.assessed_bench())
        self.assertEqual(numbers["assessed"], len(NAMES))
        self.assertEqual(numbers["marked"], 1)
        self.assertEqual(numbers["suggested"], 1)
        self.assertEqual(numbers["tiers"].get("strong"), len(NAMES))
        self.assertEqual(numbers["your_tiers"].get("reject"), 1)
        self.assertEqual(numbers["rating_overrules"], 1)

    def test_an_overruled_cull_review_is_counted(self):
        bench, _report, _photos = bench_for(self.root)
        bench.reviews.update_cluster("group-0001", [NAMES[1]], "", True, 0)
        numbers = aggregate(bench)
        self.assertEqual(numbers["overruled_groups"], 1)
        self.assertIn("overruled the cull", " ".join(disagreements(numbers)))

    def test_the_ladder_tells_the_shoots_own_story(self):
        steps = ladder(aggregate(self.assessed_bench()))
        stages = [stage for stage, _count, _who in steps]
        self.assertEqual(stages[0], "In the folder")
        self.assertIn("Assessed", stages)
        self.assertIn("Marked worth developing", stages)
        self.assertIn("Given treatments", stages)

    def test_agreement_everywhere_is_said_as_agreement(self):
        bench, _report, _photos = bench_for(self.root)
        lines = disagreements(aggregate(bench))
        self.assertEqual(len(lines), 1)
        self.assertIn("No recorded disagreements", lines[0])

    def test_the_page_renders_the_numbers(self):
        from PySide6.QtWidgets import QLabel

        from opencull_qt.debrief import DebriefPage

        page = DebriefPage(aggregate(self.assessed_bench()))
        self.addCleanup(page.deleteLater)
        shown = "\n".join(
            label.text() for label in page.findChildren(QLabel))
        self.assertIn("THE SHOOT'S OWN LADDER", shown)
        self.assertIn("WHERE YOU PARTED WAYS", shown)
        self.assertIn("not built yet", shown)


if __name__ == "__main__":
    unittest.main()
