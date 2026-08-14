"""Protect then reveal: the reasoning a development is arrived at by.

These hold the parts that decide what gets rendered and what gets paid
for -- the budget, what carries forward between rounds, what the panel
is shown -- without spending a model call to find out.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import treatment_kernel as treatment  # noqa: E402


def frame(path: Path, bright=(700, 400)) -> Path:
    """A dark frame with one clipped light in it: the shape this is for."""
    pixels = np.full((600, 900, 3), 8, dtype=np.uint8)
    pixels[430:520, 200:640] = 3                      # a silhouette
    ys, xs = np.ogrid[:600, :900]
    # Tight, the way a specular source is: the surround has to be
    # dark or 'how far it stands out' measures nothing.
    glow = np.exp(-(((xs - bright[0]) ** 2 + (ys - bright[1]) ** 2) / 1200.0))
    for channel in range(3):
        pixels[..., channel] = np.clip(
            pixels[..., channel] + glow * 400, 0, 255).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels).save(path, quality=95)
    return path


def critique(finished: bool, change: str = "lift the ghost") -> str:
    return json.dumps({"improved": "the silhouette reads", "regressed": "",
                       "next_change": change, "finished": finished,
                       "rationale": "because"})


class MeasuringTests(unittest.TestCase):
    """The numbers the model reasons with, which cost nothing."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_it_finds_the_light_and_how_far_it_stands_out(self):
        measured = treatment.measure_frame(frame(self.root / "a.jpg"))
        across, down = measured["brightest_at"]
        self.assertAlmostEqual(across, 700 / 899, places=1)
        self.assertAlmostEqual(down, 400 / 599, places=1)
        self.assertGreater(measured["subject_contrast"], 50)

    def test_it_reports_what_is_clipped_and_what_is_already_black(self):
        measured = treatment.measure_frame(frame(self.root / "a.jpg"))
        self.assertGreater(measured["clipped_percent"], 0)
        self.assertGreater(measured["silhouette_percent"], 50)

    def test_the_evidence_carries_the_spectrum_and_the_photographers_words(self):
        evidence = json.loads(treatment.evidence_json(
            "A.ARW", str(frame(self.root / "a.jpg")), "infrared", 760,
            "A partial solar eclipse; the crescent is the sun."))
        self.assertEqual(evidence["spectrum"], "infrared")
        self.assertEqual(evidence["cutoff_nm"], 760.0)
        self.assertIn("crescent is the sun", evidence["about"])
        self.assertIn("subject_contrast", evidence["baseline"])


class BudgetTests(unittest.TestCase):
    """A photographer does not give one frame infinite time."""

    def round(self, number: int, finished: bool, sections: str = "{}") -> str:
        return json.dumps({"round": number, "sections": sections,
                           "critique": json.loads(critique(finished))})

    def test_the_first_round_always_runs(self):
        self.assertTrue(treatment.keep_going([], 3))

    def test_a_finished_critique_stops_it_early(self):
        self.assertFalse(treatment.keep_going([self.round(1, True)], 3))

    def test_an_unfinished_one_buys_another_round(self):
        self.assertTrue(treatment.keep_going([self.round(1, False)], 3))

    def test_the_budget_stops_it_when_the_critique_will_not(self):
        spent = [self.round(n, False) for n in (1, 2, 3)]
        self.assertFalse(treatment.keep_going(spent, 3))

    def test_a_budget_of_one_buys_exactly_one(self):
        self.assertTrue(treatment.keep_going([], 1))
        self.assertFalse(treatment.keep_going([self.round(1, False)], 1))

    def test_a_round_with_no_critique_does_not_stop_the_loop(self):
        """A round whose recipe would not compile has nothing to say."""
        failed = json.dumps({"round": 1, "sections": "{}", "critique": None})
        self.assertTrue(treatment.keep_going([failed], 3))


class CarriedForwardTests(unittest.TestCase):
    """What one round tells the next. Without this it is three guesses."""

    def test_the_critique_reaches_the_next_recipe(self):
        record = json.dumps({
            "round": 1, "sections": "{}", "unsupported": "",
            "critique": json.loads(critique(False, "lift the left crescent"))})
        carried = treatment.latest_critique([record])
        self.assertIn("lift the left crescent", carried)
        self.assertIn("Improved", carried)

    def test_what_would_not_compile_is_carried_too(self):
        record = json.dumps({
            "round": 1, "sections": "{}",
            "unsupported": "These instructions did not compile: Use AgX",
            "critique": json.loads(critique(False))})
        self.assertIn("did not compile", treatment.latest_critique([record]))

    def test_the_first_round_is_told_nothing_and_asks_for_nothing(self):
        self.assertEqual(treatment.latest_critique([]), "")
        self.assertEqual(treatment.latest_sections([]), "")


class RecipeTests(unittest.TestCase):
    """Turning an answer into something that renders, or saying why not."""

    def test_sections_are_read_out_of_the_answer(self):
        answer = json.dumps({
            "global_exposure": ["Exposure -1.80", "Contrast +26"],
            "layers_and_masks": ["Radial gradient on the sun, inverted: "
                                 "exposure +0.60"]})
        sections = json.loads(treatment.recipe_sections(answer))
        self.assertEqual(sections["global_exposure"][0], "Exposure -1.80")
        self.assertIn("layers_and_masks", sections)

    def test_a_section_nobody_recognises_is_dropped(self):
        answer = json.dumps({"global_exposure": ["Contrast +10"],
                             "vibes": ["make it feel nostalgic"]})
        self.assertNotIn("vibes", json.loads(treatment.recipe_sections(answer)))

    def test_one_string_where_a_list_was_wanted_is_met_halfway(self):
        """The round is already paid for."""
        answer = json.dumps({"global_exposure": "Contrast +10"})
        sections = json.loads(treatment.recipe_sections(answer))
        self.assertEqual(sections["global_exposure"], ["Contrast +10"])

    def test_an_answer_that_is_not_json_costs_the_round_and_not_the_run(self):
        self.assertEqual(treatment.recipe_sections("sorry, I cannot"), "{}")

    def test_a_recipe_that_does_nothing_is_not_usable(self):
        compiled = treatment.compiled_treatment(
            "A.ARW", "t", "i", json.dumps({"global_exposure": ["be lovely"]}))
        self.assertFalse(treatment.treatment_usable(compiled))

    def test_a_recipe_that_does_something_is(self):
        compiled = treatment.compiled_treatment(
            "A.ARW", "t", "i", json.dumps({"global_exposure": ["Contrast +12"]}))
        self.assertTrue(treatment.treatment_usable(compiled))
        self.assertTrue(json.loads(compiled)["operations"])

    def test_what_did_not_compile_is_named_for_the_next_round(self):
        compiled = treatment.compiled_treatment(
            "A.ARW", "t", "i", json.dumps({
                "global_exposure": ["Contrast +12", "Use the AgX transform"]}))
        self.assertIn("AgX", treatment.unsupported_note(compiled))


class KeepingTheWorkingTests(unittest.TestCase):
    """Every round on disk, and the choice between them."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def directory(self) -> str:
        return treatment.treatment_directory(
            str(self.root), "DSC00703.ARW", "20260814T000000Z")

    def test_a_round_is_written_before_the_next_one_starts(self):
        where = self.directory()
        compiled = treatment.compiled_treatment(
            "A.ARW", "t", "i", json.dumps({"global_exposure": ["Contrast +12"]}))
        treatment.round_record(where, [], compiled, "{}", "render.jpg",
                               json.dumps({"subject_contrast": 70}),
                               critique(False))
        written = Path(where) / "round-1.json"
        self.assertTrue(written.is_file())
        self.assertEqual(json.loads(written.read_text())["round"], 1)

    def test_rounds_number_themselves_in_order(self):
        where = self.directory()
        compiled = treatment.compiled_treatment(
            "A.ARW", "t", "i", json.dumps({"global_exposure": ["Contrast +12"]}))
        records = []
        for _ in range(3):
            records.append(treatment.round_record(
                where, records, compiled, "{}", "r.jpg",
                json.dumps({"subject_contrast": 70}), critique(False)))
        self.assertEqual([json.loads(item)["round"] for item in records],
                         [1, 2, 3])

    def test_the_round_offered_is_the_one_that_measured_best(self):
        """Not merely the last one tried."""
        rounds = [
            json.dumps({"round": 1, "measurements": {"subject_contrast": 70}}),
            json.dumps({"round": 2, "measurements": {"subject_contrast": 92}}),
            json.dumps({"round": 3, "measurements": {"subject_contrast": 81}}),
        ]
        self.assertEqual(treatment.best_round(rounds), 2)

    def test_a_tie_goes_to_the_round_that_heard_more_criticism(self):
        rounds = [
            json.dumps({"round": 1, "measurements": {"subject_contrast": 88}}),
            json.dumps({"round": 2, "measurements": {"subject_contrast": 88}}),
        ]
        self.assertEqual(treatment.best_round(rounds), 2)


class WarrantTests(unittest.TestCase):
    """What the panel is shown, and what it is asked."""

    def report(self, rounds=2, renders=True) -> str:
        made = [json.dumps({
            "round": n, "sections": "{}",
            "recipe": {"operations": [{"op": "tone.exposure", "value": -1.8}]},
            "unsupported": "",
            "render": f"round-{n}.jpg" if renders else "",
            "measurements": {"subject_contrast": 70 + n, "clipped_percent": 0.2},
            "critique": json.loads(critique(n == rounds)),
        }) for n in range(1, rounds + 1)]
        return treatment.treatment_report(
            "/where", "A.ARW",
            json.dumps({"photo": "A.ARW", "about": "an eclipse",
                        "baseline": {"subject_contrast": 60}}),
            "the sun is clipped beyond recovery", "protect the core",
            "one mask and its inverse", made, treatment.best_round(made))

    def test_a_finished_treatment_passes_its_own_checks(self):
        self.assertTrue(treatment.treatment_valid(self.report()))

    def test_a_treatment_that_never_rendered_does_not(self):
        self.assertFalse(treatment.treatment_valid(self.report(renders=False)))

    def test_the_panel_is_shown_the_plan_and_the_numbers_not_the_picture(self):
        warrant = treatment.treatment_evidence(self.report())
        self.assertIn("subject_contrast", warrant)
        self.assertIn("protect the core", warrant)
        self.assertNotIn(".jpg", warrant)

    def test_every_round_is_in_the_warrant_including_the_ones_not_chosen(self):
        warrant = json.loads(
            treatment.treatment_evidence(self.report(rounds=3)).split("\n", 1)[1])
        self.assertEqual(len(warrant["each_round"]), 3)
        self.assertEqual(warrant["rounds_spent"], 3)

    def test_the_policy_says_it_must_not_chase_the_irrecoverable(self):
        policy = treatment.treatment_policy(
            json.dumps({"about": "a partial solar eclipse"}))
        self.assertIn("irrecoverable", policy)
        self.assertIn("solar eclipse", policy)

    def test_an_abstention_keeps_the_evidence(self):
        refusal = treatment.treatment_abstention("the numbers")
        self.assertIn("still on disk", refusal)
        self.assertIn("the numbers", refusal)


if __name__ == "__main__":
    unittest.main()
