"""The bar a shoot is judged against, and choosing it on purpose."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui import criteria  # noqa: E402


class StanceTests(unittest.TestCase):
    def test_every_stance_names_its_bar_as_a_question(self):
        for item in criteria.STANCES:
            self.assertTrue(item["name"])
            self.assertTrue(item["bar"].endswith("?"), item["id"])
            self.assertTrue(item["detail"])

    def test_the_four_bars_are_the_ones_the_prompt_understands(self):
        self.assertEqual(
            criteria.STANCE_IDS,
            ("professional", "artistic", "documentary", "family"))

    def test_an_unset_stance_is_professional_not_the_kernels_family(self):
        # The kernel falls back to family, the gentlest bar there is. An
        # application for deciding what to develop must not apply it silently.
        self.assertEqual(criteria.normalise(""), "professional")
        self.assertEqual(criteria.normalise(None), "professional")
        self.assertEqual(criteria.DEFAULT_STANCE, "professional")

    def test_a_stance_that_does_not_exist_falls_back_rather_than_raising(self):
        self.assertEqual(criteria.normalise("gallery"), "professional")

    def test_case_and_spacing_do_not_invent_a_new_bar(self):
        self.assertEqual(criteria.normalise("  ARTISTIC "), "artistic")

    def test_asking_for_a_stance_that_does_not_exist_is_refused(self):
        with self.assertRaises(criteria.CriteriaError):
            criteria.stance("gallery")

    def test_a_run_records_the_bar_it_was_measured_against(self):
        record = criteria.record("artistic")
        self.assertEqual(record["format"], criteria.FORMAT)
        self.assertEqual(record["stance"], "artistic")
        self.assertTrue(record["bar"].endswith("?"))

    def test_a_bar_is_described_in_one_line(self):
        self.assertIn("Professional", criteria.describe("professional"))
        self.assertIn("client", criteria.describe("professional"))


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class CriteriaDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def dialog(self, frames: int = 23, culled: bool = False, stance: str = ""):
        from opencull_qt.criteria import CriteriaDialog

        value = CriteriaDialog("A Journey", frames, culled, stance)
        self.addCleanup(value.deleteLater)
        return value

    def text(self, dialog) -> str:
        from PySide6.QtWidgets import QLabel

        return "\n".join(
            label.text() for label in dialog.findChildren(QLabel))

    def test_all_four_bars_are_offered(self):
        self.assertEqual(len(self.dialog().stances), 4)

    def test_it_opens_on_the_deliberate_default(self):
        self.assertEqual(self.dialog().stance(), "professional")

    def test_it_reopens_on_the_bar_used_last_time(self):
        self.assertEqual(self.dialog(stance="documentary").stance(),
                         "documentary")

    def test_choosing_a_bar_is_what_comes_back(self):
        dialog = self.dialog()
        dialog.stances[1].choice.setChecked(True)
        self.assertEqual(dialog.stance(), "artistic")

    def test_it_says_what_the_run_will_cost(self):
        self.assertIn("23 model calls", self.text(self.dialog(23, culled=False)))

    def test_a_culled_folder_is_told_it_is_reading_keepers(self):
        shown = self.text(self.dialog(6, culled=True))
        self.assertIn("6 frames are read", shown)
        self.assertNotIn("has not been culled", shown)

    def test_an_unculled_folder_is_warned_what_it_is_paying_for(self):
        shown = self.text(self.dialog(23, culled=False))
        self.assertIn("has not been culled", shown)
        self.assertIn("rather than one per keeper", shown)

    def test_the_button_names_the_number_it_will_read(self):
        self.assertEqual(self.dialog(23).run.text(), "Assess 23 frames")

    def test_it_says_the_axes_do_not_change(self):
        self.assertIn("same ten axes", self.text(self.dialog()))


if __name__ == "__main__":
    unittest.main()
