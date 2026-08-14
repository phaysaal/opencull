"""The advice bands under every slider, and the full control surface.

The bands say how far a control can be pushed before the photograph
pays: teal where a professional moves without comment, amber where the
move is a statement, red where it is damage. Advice, not walls -- the
handle still travels the compiler's whole range.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui import adjustments, zones  # noqa: E402
from recipe_compiler import RANGES  # noqa: E402


class ZoneTests(unittest.TestCase):
    """Where the bands sit, and who may say so."""

    def test_every_control_has_default_bands_inside_its_range(self):
        for op in RANGES:
            told = zones.zones_for(op)
            low, high = RANGES[op][0], RANGES[op][1]
            self.assertTrue(told, op)
            s0, s1 = told["safe"]; a0, a1 = told["artistic"]
            self.assertLessEqual(low, a0, op)
            self.assertLessEqual(a1, high, op)
            self.assertLessEqual(a0, s0, op)
            self.assertLessEqual(s1, a1, op)

    def test_the_bands_are_asymmetric_where_the_damage_is(self):
        """Exposure ruins highlights faster than shadows; dehaze halos
        long before negative dehaze fogs. The defaults must know."""
        exposure = zones.zones_for("tone.exposure")
        self.assertLess(abs(exposure["safe"][1]), abs(exposure["safe"][0]))
        dehaze = zones.zones_for("detail.dehaze")
        self.assertGreater(dehaze["safe"][1], abs(dehaze["safe"][0]))

    def test_a_model_that_looked_at_the_frame_overrides_the_default(self):
        told = zones.zones_for("tone.exposure",
                               {"safe": [-0.3, 0.2], "artistic": [-1.0, 0.6]})
        self.assertEqual(told["safe"], (-0.3, 0.2))
        self.assertEqual(told["artistic"], (-1.0, 0.6))

    def test_a_stated_band_is_clamped_to_the_compilers_range(self):
        told = zones.zones_for("tone.exposure",
                               {"safe": [-90, 90], "artistic": [-90, 90]})
        self.assertEqual(told["artistic"], (-5.0, 5.0))

    def test_safe_is_forced_inside_artistic(self):
        told = zones.zones_for("tone.exposure",
                               {"safe": [-4.0, 4.0], "artistic": [-1.0, 1.0]})
        s0, s1 = told["safe"]; a0, a1 = told["artistic"]
        self.assertGreaterEqual(s0, a0)
        self.assertLessEqual(s1, a1)

    def test_nonsense_in_the_file_falls_back_to_the_default(self):
        told = zones.zones_for("tone.exposure", {"safe": "very", "artistic": 3})
        self.assertEqual(told, zones.zones_for("tone.exposure"))

    def test_the_per_photo_file_is_read_from_beside_the_recipes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = zones.zones_path(root, "DSC00703.ARW")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "format": zones.FORMAT, "photo": "DSC00703.ARW",
                "zones": {"tone.exposure": {"safe": [-0.3, 0.2],
                                            "artistic": [-1.0, 0.6]}}}))
            told = zones.load(root, "DSC00703.ARW")
            self.assertEqual(told["tone.exposure"]["safe"], (-0.3, 0.2))
            # A control the file does not name keeps the default.
            self.assertEqual(told["detail.dehaze"],
                             zones.zones_for("detail.dehaze"))

    def test_a_missing_file_is_the_defaults_for_everything(self):
        with tempfile.TemporaryDirectory() as temporary:
            told = zones.load(Path(temporary), "DSC00703.ARW")
            self.assertEqual(set(told), set(RANGES))


class FullSurfaceTests(unittest.TestCase):
    """The whole instrument, whether the treatment used it or not."""

    def recipe(self):
        return {"format": "opencull-development-recipe-v1",
                "operations": [
                    {"op": "tone.exposure", "value": -1.5, "unit": "EV",
                     "mode": "delta", "source_instruction": "exposure -1.5"}]}

    def test_the_surface_holds_every_labelled_control(self):
        surface = adjustments.full_surface(self.recipe())
        self.assertEqual({item["op"] for item in surface},
                         set(adjustments.LABELS))

    def test_a_compiled_control_is_not_doubled_by_its_absent_twin(self):
        surface = adjustments.full_surface(self.recipe())
        exposures = [item for item in surface
                     if item["op"] == "tone.exposure"]
        self.assertEqual(len(exposures), 1)
        self.assertFalse(exposures[0].get("absent"))

    def test_an_absent_control_sits_at_its_neutral(self):
        surface = adjustments.full_surface(self.recipe())
        midpoint = next(item for item in surface
                        if item["op"] == "levels.midpoint")
        self.assertTrue(midpoint["absent"])
        self.assertEqual(midpoint["value"], 1.0)
        white = next(item for item in surface
                     if item["op"] == "levels.white_input")
        self.assertEqual(white["value"], 255.0)

    def test_inserting_makes_the_absent_operation_real(self):
        made = adjustments.insert(self.recipe(), "tone.shadow", 25.0)
        names = [item["op"] for item in made["operations"]]
        self.assertIn("tone.shadow", names)
        added = next(item for item in made["operations"]
                     if item["op"] == "tone.shadow")
        self.assertEqual(added["value"], 25.0)
        self.assertEqual(added["source"], "added by hand")
        # And the original recipe was never touched.
        self.assertNotIn("tone.shadow",
                         [item["op"] for item in self.recipe()["operations"]])

    def test_an_insert_lands_in_section_order_before_the_masks(self):
        recipe = self.recipe()
        recipe["operations"].append({
            "op": "mask.radial", "unit": "mask", "mode": "absolute",
            "value": {"anchor": "on the sun", "effects": []}})
        made = adjustments.insert(recipe, "detail.dehaze", 10.0)
        names = [item["op"] for item in made["operations"]]
        self.assertLess(names.index("tone.exposure"), names.index("detail.dehaze"))
        self.assertLess(names.index("detail.dehaze"), names.index("mask.radial"))

    def test_inserting_a_present_operation_is_refused(self):
        with self.assertRaises(adjustments.AdjustmentError):
            adjustments.insert(self.recipe(), "tone.exposure", 1.0)

    def test_an_inserted_control_can_then_be_adjusted_like_any_other(self):
        made = adjustments.insert(self.recipe(), "tone.shadow", 25.0)
        moved = adjustments.apply(made, {"tone.shadow": {"value": 40.0}})
        shadow = next(item for item in moved["operations"]
                      if item["op"] == "tone.shadow")
        self.assertEqual(shadow["value"], 40.0)
        # Its asked mark is neutral: the model never asked for it.
        self.assertEqual(shadow["asked_value"], 0.0)


if __name__ == "__main__":
    unittest.main()
