"""The prefilter: an ordinary human review, written before the paid run."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from opencull_gui.report import load_report  # noqa: E402
from opencull_gui.reviews import (  # noqa: E402
    PREFILTER_NOTE,
    ReviewError,
    ReviewStore,
    default_review_path,
    narrow_selection,
)

CLUSTERS = {
    "g1": ["A1.JPG", "A2.JPG"],
    "g2": ["B1.JPG"],
    "g3": ["C1.JPG", "C2.JPG"],
}
KEEP = {"g1": ["A1.JPG"], "g2": ["B1.JPG"], "g3": ["C1.JPG", "C2.JPG"]}


class NarrowSelectionTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name).resolve()
        self.photos = root / "photos"
        self.photos.mkdir()
        for names in CLUSTERS.values():
            for name in names:
                Image.new("RGB", (60, 40), (40, 70, 90)).save(
                    self.photos / name)
        self.report_path = root / "shoot-results.json"
        self.report_path.write_text(json.dumps({
            "format": "opencull-report-v2", "manifest_sha256": "x",
            "clusters": [
                {"cluster_id": key, "photos": names}
                for key, names in CLUSTERS.items()],
            "keep": [
                {"cluster_id": key, "photos": KEEP[key],
                 "rationale": "sharpest", "confidence": 0.8, "warning": "",
                 "fallback": False, "photographic_assessment": []}
                for key in CLUSTERS],
            "warnings": [], "adaptive_clustering": {"enabled": True},
            "notice": "read only",
        }), encoding="utf-8")
        self.report = load_report(self.report_path)
        self.store = ReviewStore(
            default_review_path(self.report_path), self.report, self.photos)
        self.addCleanup(self._temporary.cleanup)

    def keepers(self, cluster: str) -> list[str]:
        return self.store.public_state()["clusters"][cluster]["keepers"]

    def test_excluding_a_whole_cluster_reviews_it_empty(self):
        narrow_selection(self.store, ["A1.JPG", "C1.JPG", "C2.JPG"])
        entry = self.store.public_state()["clusters"]["g2"]
        self.assertTrue(entry["reviewed"])
        self.assertEqual(entry["keepers"], [])

    def test_a_partial_exclusion_keeps_the_rest(self):
        narrow_selection(self.store, ["A1.JPG", "B1.JPG", "C1.JPG"])
        self.assertEqual(self.keepers("g3"), ["C1.JPG"])

    def test_untouched_clusters_are_not_written(self):
        written = narrow_selection(self.store, ["A1.JPG", "C1.JPG"])
        state = self.store.public_state()
        # g1's effective keeper is already A1: no write, no claimed review.
        self.assertEqual(written, 2)
        self.assertNotIn("g1", state["clusters"])
        self.assertEqual(state["revision"], 2)

    def test_a_no_op_narrowing_writes_nothing_at_all(self):
        written = narrow_selection(
            self.store, ["A1.JPG", "B1.JPG", "C1.JPG", "C2.JPG"])
        self.assertEqual(written, 0)
        self.assertEqual(self.store.public_state()["revision"], 0)

    def test_an_empty_note_gets_the_prefilter_note(self):
        narrow_selection(self.store, ["A1.JPG", "C1.JPG"])
        entry = self.store.public_state()["clusters"]["g2"]
        self.assertEqual(entry["note"], PREFILTER_NOTE)

    def test_an_existing_note_is_preserved_verbatim(self):
        revision = self.store.public_state()["revision"]
        self.store.update_cluster(
            "g2", ["B1.JPG"], "the bride asked for this one", True, revision)
        narrow_selection(self.store, ["A1.JPG"])
        entry = self.store.public_state()["clusters"]["g2"]
        self.assertEqual(entry["note"], "the bride asked for this one")
        self.assertEqual(entry["keepers"], [])

    def test_a_human_review_is_narrowed_from_their_keepers_not_the_ais(self):
        revision = self.store.public_state()["revision"]
        # The photographer already overruled the AI on g1: kept A2 instead.
        self.store.update_cluster("g1", ["A2.JPG"], "", True, revision)
        narrow_selection(self.store, ["A1.JPG", "B1.JPG", "C1.JPG", "C2.JPG"])
        # A2 was their keeper; excluding it empties the cluster rather than
        # resurrecting the AI's A1.
        self.assertEqual(self.keepers("g1"), [])

    def test_the_history_names_the_act(self):
        narrow_selection(self.store, ["A1.JPG", "C1.JPG"])
        actions = {event["action"]
                   for event in self.store.public_state()["history"]}
        self.assertEqual(actions, {"prefilter"})

    def test_a_stale_store_raises_before_any_write(self):
        self.store.stale_reason = "review belongs to a different report"
        with self.assertRaises(ReviewError):
            narrow_selection(self.store, ["A1.JPG"])

    def test_the_run_reads_what_was_narrowed(self):
        from shortlist_kernel import chosen_photographs

        narrow_selection(self.store, ["A1.JPG", "C1.JPG"])
        review = json.loads(
            default_review_path(self.report_path).read_text(encoding="utf-8"))
        self.assertEqual(
            chosen_photographs(self.report.data, review),
            ["A1.JPG", "C1.JPG"])


if __name__ == "__main__":
    unittest.main()
