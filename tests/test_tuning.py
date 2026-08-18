"""The tuning ledger: what each frame's editing is, remembered."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opencull_gui.tuning import TuningLedger


class TuningLedgerTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary.name) / "finetune-state.json"
        self.addCleanup(self._temporary.cleanup)

    def test_a_profile_round_trips_through_disk(self):
        ledger = TuningLedger(self.path)
        ledger.save("A.JPG", "standard", {"curve": {"points": [[0, 0]]}}, 2)
        reopened = TuningLedger(self.path)
        held = reopened.get("A.JPG")
        self.assertEqual(held["treatment"], "standard")
        self.assertEqual(held["layer"], 2)
        self.assertEqual(held["changes"]["curve"]["points"], [[0, 0]])

    def test_no_changes_means_no_profile(self):
        ledger = TuningLedger(self.path)
        self.assertIsNone(ledger.get("A.JPG"))
        ledger.save("A.JPG", "standard", {"x": 1}, 0)
        ledger.settle("A.JPG")
        self.assertIsNone(ledger.get("A.JPG"))

    def test_unexported_follows_the_hand_and_the_export(self):
        ledger = TuningLedger(self.path)
        self.assertFalse(ledger.unexported("A.JPG"))
        ledger.save("A.JPG", "standard", {"x": 1}, 0)
        self.assertTrue(ledger.unexported("A.JPG"))
        ledger.mark_exported("A.JPG")
        self.assertFalse(ledger.unexported("A.JPG"))
        ledger.save("A.JPG", "standard", {"x": 2}, 0)   # edited again
        self.assertTrue(ledger.unexported("A.JPG"))

    def test_settling_keeps_the_export_on_record(self):
        ledger = TuningLedger(self.path)
        ledger.save("A.JPG", "standard", {"x": 1}, 0)
        ledger.mark_exported("A.JPG")
        ledger.save("A.JPG", "standard", {"x": 2}, 0)
        ledger.settle("A.JPG")
        self.assertFalse(ledger.unexported("A.JPG"))
        reopened = TuningLedger(self.path)
        self.assertFalse(reopened.unexported("A.JPG"))

    def test_an_unchanged_save_does_not_restamp_the_edit(self):
        ledger = TuningLedger(self.path)
        ledger.save("A.JPG", "standard", {"x": 1}, 0)
        first = ledger._states["A.JPG"]["edited_at"]
        ledger.save("A.JPG", "standard", {"x": 1}, 0)
        self.assertEqual(ledger._states["A.JPG"]["edited_at"], first)

    def test_a_broken_file_reads_as_empty(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text("{broken", encoding="utf-8")
        ledger = TuningLedger(self.path)
        self.assertIsNone(ledger.get("A.JPG"))


if __name__ == "__main__":
    unittest.main()
