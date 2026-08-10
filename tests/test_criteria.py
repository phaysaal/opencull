"""The composed bar: one strictness, any lenses, one run."""

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


class CriteriaModelTests(unittest.TestCase):
    def test_every_strictness_names_its_bar_as_a_question(self):
        for item in criteria.STRICTNESS:
            self.assertTrue(item["bar"].endswith("?"), item["id"])

    def test_the_composition_reads_as_one_description(self):
        composed = criteria.compose({
            "strictness": "gentle",
            "lenses": ["family", "place"],
            "words": "quiet light, nothing posed",
        })
        self.assertIn("gentle bar", composed)
        self.assertIn("family", composed)
        self.assertIn("place", composed)
        self.assertIn("quiet light, nothing posed", composed)

    def test_one_run_means_one_string_whatever_is_chosen(self):
        self.assertIsInstance(
            criteria.compose({"strictness": "strict",
                              "lenses": list(criteria.LENS_IDS)}), str)

    def test_the_single_choice_era_still_reads(self):
        for legacy, expected_strictness in (
            ("professional", "strict"), ("artistic", "balanced"),
            ("documentary", "balanced"), ("family", "gentle"),
        ):
            choice = criteria.normalise_choice(legacy)
            self.assertEqual(choice["strictness"], expected_strictness)
            self.assertEqual(len(choice["lenses"]), 1)

    def test_garbage_falls_back_to_the_deliberate_strict_default(self):
        for value in ("", None, "banquet", 7, {"strictness": "x"},
                      {"lenses": ["y"]}):
            choice = criteria.normalise_choice(value)
            self.assertEqual(choice["strictness"], "strict")
            self.assertEqual(choice["lenses"], ["craft"])

    def test_lenses_deduplicate_and_words_are_bounded(self):
        choice = criteria.normalise_choice({
            "strictness": "balanced",
            "lenses": ["family", "family", "place"],
            "words": "x" * 1000,
        })
        self.assertEqual(choice["lenses"], ["family", "place"])
        self.assertEqual(len(choice["words"]), criteria.WORDS_LIMIT)

    def test_a_run_records_the_bar_it_was_measured_against(self):
        value = criteria.record({"strictness": "gentle",
                                 "lenses": ["family", "place"],
                                 "words": "w"})
        self.assertEqual(value["format"], criteria.FORMAT)
        self.assertEqual(value["strictness"], "gentle")
        self.assertEqual(value["lenses"], ["family", "place"])
        self.assertEqual(value["stance"],
                         criteria.compose(value))

    def test_a_bar_is_described_in_one_line(self):
        self.assertEqual(
            criteria.describe({"strictness": "gentle",
                               "lenses": ["family", "place"]}),
            "Gentle · Family + Place & moment")


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class CriteriaDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def dialog(self, frames: int = 23, culled: bool = False, stance=""):
        from opencull_qt.criteria import CriteriaDialog

        value = CriteriaDialog("A Journey", frames, culled, stance)
        self.addCleanup(value.deleteLater)
        return value

    def text(self, dialog) -> str:
        from PySide6.QtWidgets import QLabel

        return "\n".join(
            label.text() for label in dialog.findChildren(QLabel))

    def test_three_bar_heights_and_four_lenses_are_offered(self):
        dialog = self.dialog()
        self.assertEqual(len(dialog.strictness_options), 3)
        self.assertEqual(len(dialog.lens_options), 4)

    def test_it_opens_on_the_deliberate_default(self):
        choice = self.dialog().choice()
        self.assertEqual(choice["strictness"], "strict")
        self.assertEqual(choice["lenses"], ["craft"])

    def test_it_reopens_on_the_bar_used_last_time(self):
        remembered = {"strictness": "gentle",
                      "lenses": ["family", "place"], "words": "soft"}
        dialog = self.dialog(stance=remembered)
        self.assertEqual(dialog.choice(), criteria.normalise_choice(
            remembered))

    def test_a_legacy_stance_preselects_its_translation(self):
        self.assertEqual(
            self.dialog(stance="documentary").choice()["lenses"],
            ["place"])

    def test_several_lenses_compose_into_one_stance(self):
        dialog = self.dialog()
        for widget in dialog.lens_options:
            widget.choice.setChecked(
                widget.item["id"] in {"family", "place"})
        stance = dialog.stance()
        self.assertIn("family", stance)
        self.assertIn("place", stance)

    def test_no_lens_disables_the_run(self):
        dialog = self.dialog()
        for widget in dialog.lens_options:
            widget.choice.setChecked(False)
        self.assertFalse(dialog.run.isEnabled())
        dialog.lens_options[0].choice.setChecked(True)
        self.assertTrue(dialog.run.isEnabled())

    def test_own_words_ride_along(self):
        dialog = self.dialog()
        dialog.words.setText("quiet mornings")
        self.assertIn("quiet mornings", dialog.stance())

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
