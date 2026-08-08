"""Bounded adjustment of a compiled recipe: what moves, and what cannot."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui import adjustments  # noqa: E402
from recipe_compiler import compile_recipe  # noqa: E402


def recipe(**changes) -> dict:
    value = compile_recipe(
        "A.JPG", "standard", "Low evening key", "hold the light back",
        json.dumps({
            "global_exposure": ["exposure +0.45", "contrast +4"],
            "hdr_levels_curves": ["shadows +18"],
            "white_balance_and_color": ["temperature 5400 kelvin"],
            "finishing_and_output": ["vignette -12"],
        }),
        "keep skin believable", "jpeg")
    value.update(changes)
    return value


def control(value: dict, operation: str) -> dict:
    return next(item for item in adjustments.controls(value)
                if item["op"] == operation)


class ControlTests(unittest.TestCase):
    def test_every_bounded_number_is_a_control(self):
        found = {item["op"] for item in adjustments.controls(recipe())}
        self.assertIn("tone.exposure", found)
        self.assertIn("tone.shadow", found)
        self.assertIn("color.temperature", found)

    def test_a_control_carries_the_sentence_that_produced_it(self):
        self.assertEqual(
            control(recipe(), "tone.exposure")["source"], "exposure +0.45")

    def test_a_control_carries_the_compilers_own_bounds(self):
        exposure = control(recipe(), "tone.exposure")
        self.assertEqual((exposure["low"], exposure["high"]), (-5.0, 5.0))
        self.assertEqual(exposure["unit"], "EV")

    def test_a_guardrail_is_not_a_control(self):
        found = {item["op"] for item in adjustments.controls(recipe())}
        self.assertFalse(any(name.startswith("guardrail.") for name in found))

    def test_the_guardrails_are_still_reported(self):
        self.assertTrue(adjustments.guardrails(recipe()))

    def test_an_unbounded_operation_is_not_offered(self):
        value = recipe()
        value["operations"].append({
            "id": "op-999", "op": "crop.aspect", "value": 1.5,
            "unit": "ratio", "enabled": True})
        found = {item["op"] for item in adjustments.controls(value)}
        self.assertNotIn("crop.aspect", found)

    def test_controls_are_grouped_the_way_they_are_reached_for(self):
        sections = [item["section"] for item in adjustments.controls(recipe())]
        self.assertEqual(sections, sorted(sections, key=[
            "Tone", "Levels", "Colour", "Detail", "Finish"].index))


class ApplyTests(unittest.TestCase):
    def test_moving_a_control_changes_what_will_be_rendered(self):
        value = recipe()
        exposure = control(value, "tone.exposure")
        after = adjustments.apply(value, {exposure["id"]: {"value": 0.30}})
        self.assertAlmostEqual(control(after, "tone.exposure")["value"], 0.30)

    def test_the_treatment_it_came_from_is_never_touched(self):
        value = recipe()
        before = json.dumps(value, sort_keys=True)
        adjustments.apply(
            value, {control(value, "tone.exposure")["id"]: {"value": 1.0}})
        self.assertEqual(json.dumps(value, sort_keys=True), before)

    def test_what_the_model_asked_for_survives_the_change(self):
        value = recipe()
        asked = control(value, "tone.exposure")["value"]
        after = adjustments.apply(
            value, {control(value, "tone.exposure")["id"]: {"value": -1.0}})
        self.assertAlmostEqual(control(after, "tone.exposure")["asked"], asked)

    def test_what_the_model_asked_for_survives_being_moved_twice(self):
        value = recipe()
        asked = control(value, "tone.exposure")["value"]
        once = adjustments.apply(
            value, {control(value, "tone.exposure")["id"]: {"value": -1.0}})
        twice = adjustments.apply(
            once, {control(once, "tone.exposure")["id"]: {"value": 2.0}})
        self.assertAlmostEqual(control(twice, "tone.exposure")["asked"], asked)

    def test_a_value_outside_the_range_is_brought_back_inside_it(self):
        value = recipe()
        after = adjustments.apply(
            value, {control(value, "tone.exposure")["id"]: {"value": 500.0}})
        self.assertEqual(control(after, "tone.exposure")["value"], 5.0)

    def test_an_operation_can_be_switched_off(self):
        value = recipe()
        after = adjustments.apply(
            value, {control(value, "tone.shadow")["id"]: {"enabled": False}})
        self.assertFalse(control(after, "tone.shadow")["enabled"])

    def test_the_changes_are_recorded_beside_the_recipe(self):
        value = recipe()
        after = adjustments.apply(
            value, {control(value, "tone.exposure")["id"]: {"value": 0.1}})
        record = after["adjustments"]
        self.assertEqual(record["format"], adjustments.FORMAT)
        self.assertEqual(len(record["changes"]), 1)
        self.assertAlmostEqual(record["changes"][0]["set"], 0.1)

    def test_an_adjusted_recipe_is_a_new_revision(self):
        value = recipe()
        after = adjustments.apply(
            value, {control(value, "tone.exposure")["id"]: {"value": 0.1}})
        self.assertEqual(after["revision"],
                         int(value.get("revision", 0) or 0) + 1)

    def test_changing_nothing_leaves_the_revision_alone(self):
        value = recipe()
        self.assertNotIn("adjustments", adjustments.apply(value, {}))

    def test_an_unknown_control_is_ignored_rather_than_invented(self):
        value = recipe()
        after = adjustments.apply(value, {"op-nonexistent": {"value": 3.0}})
        self.assertNotIn("adjustments", after)

    def test_a_recipe_that_is_not_an_object_is_refused(self):
        with self.assertRaises(adjustments.AdjustmentError):
            adjustments.apply("not a recipe", {})


class ReadingTests(unittest.TestCase):
    def test_an_untouched_recipe_has_nothing_moved(self):
        self.assertEqual(adjustments.moved(recipe()), [])

    def test_a_moved_control_is_reported_as_moved(self):
        value = recipe()
        after = adjustments.apply(
            value, {control(value, "tone.exposure")["id"]: {"value": -2.0}})
        self.assertEqual(
            [item["op"] for item in adjustments.moved(after)], ["tone.exposure"])

    def test_a_switched_off_control_counts_as_moved(self):
        value = recipe()
        after = adjustments.apply(
            value, {control(value, "tone.shadow")["id"]: {"enabled": False}})
        self.assertIn("tone.shadow", [item["op"] for item in adjustments.moved(after)])

    def test_a_change_is_described_as_a_disagreement(self):
        value = recipe()
        after = adjustments.apply(
            value, {control(value, "tone.exposure")["id"]: {"value": 0.30}})
        line = adjustments.describe(control(after, "tone.exposure"))
        self.assertIn("model asked", line)
        self.assertIn("you set +0.30 EV", line)

    def test_an_untouched_control_says_it_is_as_asked(self):
        self.assertIn(
            "as asked",
            adjustments.describe(control(recipe(), "tone.exposure")))

    def test_values_are_written_in_the_units_they_are_read_in(self):
        self.assertEqual(adjustments.written(0.3, "EV"), "+0.30 EV")
        self.assertEqual(adjustments.written(-18, "percent"), "-18%")
        self.assertEqual(adjustments.written(5400, "kelvin"), "5,400 K")


if __name__ == "__main__":
    unittest.main()


class SpokenTests(unittest.TestCase):
    """Typed words compile through the same grammar as the suggestions."""

    def test_plain_moves_are_heard(self):
        heard, unheard = adjustments.compile_words(
            "shadows +12, vignette -8, temperature 5400 kelvin")
        self.assertEqual(
            [(op["op"], op["value"]) for op in heard],
            [("tone.shadow", 12.0), ("finish.vignette", -8.0),
             ("color.temperature", 5400.0)])
        self.assertEqual(unheard, [])

    def test_free_words_are_said_back_not_swallowed(self):
        heard, unheard = adjustments.compile_words("make it moody")
        self.assertEqual(heard, [])
        self.assertEqual(unheard, ["make it moody"])

    def test_lines_and_semicolons_separate_phrases_too(self):
        heard, _ = adjustments.compile_words("exposure +0.3; contrast +4\nclarity +10")
        self.assertEqual(len(heard), 3)

    def test_an_empty_box_is_nothing_at_all(self):
        self.assertEqual(adjustments.compile_words("   "), ([], []))
