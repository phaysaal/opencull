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


class MaskModelTests(unittest.TestCase):
    """The mask made movable: parse, move, rebuild, and never drift.

    The anchor sentence is the only geometry the engine reads, so the
    kernel that rewrites it is the only place a slider's number can
    part company with the render. These hold that seam.
    """

    def recipe(self):
        return {"format": "opencull-development-recipe-v1", "operations": [
            {"op": "tone.exposure", "value": -1.5, "unit": "EV",
             "mode": "delta"},
            {"op": "mask.radial", "unit": "mask", "mode": "absolute",
             "source": "the surroundings",
             "value": {"anchor": "radial gradient, at 49%, 52%, "
                                 "radius 36%, inverted, feather 80%",
                       "opacity": 1.0, "feather": 0.8,
                       "effects": [{"op": "tone.exposure", "value": 0.8,
                                    "unit": "EV", "mode": "delta"}]}},
            {"op": "mask.linear", "unit": "mask", "mode": "absolute",
             "source": "the rooftop",
             "value": {"anchor": "linear gradient, from the bottom, "
                                 "up to 35%",
                       "opacity": 1.0, "feather": 0.6,
                       "effects": [{"op": "tone.shadow", "value": 25.0,
                                    "unit": "percent", "mode": "delta"}]}},
        ]}

    def test_the_masks_read_back_with_their_geometry(self):
        placed = adjustments.masks(self.recipe())
        self.assertEqual([item["id"] for item in placed],
                         ["mask:1", "mask:2"])
        radial = placed[0]["geometry"]
        self.assertEqual((radial["centre_x"], radial["centre_y"],
                          radial["radius"]), (49.0, 52.0, 36.0))
        self.assertTrue(radial["inverted"])
        self.assertEqual(radial["feather"], 80)
        linear = placed[1]["geometry"]
        self.assertEqual((linear["edge"], linear["reach"]),
                         ("bottom", 35.0))

    def test_moving_geometry_rewrites_the_sentence_the_engine_reads(self):
        moved = adjustments.apply(self.recipe(), {
            "mask:1": {"geometry": {"radius": 28.0, "centre_x": 44.0}}})
        anchor = moved["operations"][1]["value"]["anchor"]
        self.assertIn("at 44%, 52%", anchor)
        self.assertIn("radius 28%", anchor)
        self.assertIn("inverted", anchor)

    def test_geometry_survives_a_round_trip_unchanged(self):
        """Parse then rebuild must be a fixed point, or every unrelated
        adjustment would quietly move every mask."""
        recipe = self.recipe()
        placed = adjustments.masks(recipe)
        for mask in placed:
            rebuilt = adjustments._build_anchor(mask["shape"],
                                                mask["geometry"])
            reparsed = adjustments._parse_anchor(mask["shape"], rebuilt)
            for key, value in reparsed.items():
                if key in mask["geometry"]:
                    self.assertEqual(value, mask["geometry"][key],
                                     f"{mask['id']}.{key}")

    def test_feather_and_opacity_move_the_value_fields(self):
        moved = adjustments.apply(self.recipe(), {
            "mask:1": {"geometry": {"feather": 60, "opacity": 80}}})
        value = moved["operations"][1]["value"]
        self.assertAlmostEqual(value["feather"], 0.6)
        self.assertAlmostEqual(value["opacity"], 0.8)

    def test_a_mask_effect_is_moved_inside_the_compilers_bounds(self):
        moved = adjustments.apply(self.recipe(), {
            "mask:1/tone.exposure": {"value": 40.0}})
        effect = moved["operations"][1]["value"]["effects"][0]
        self.assertEqual(effect["value"], 5.0)
        self.assertEqual(effect["asked_value"], 0.8)

    def test_a_disabled_effect_is_removed_because_the_engine_applies_lists(self):
        moved = adjustments.apply(self.recipe(), {
            "mask:2/tone.shadow": {"enabled": False}})
        self.assertEqual(moved["operations"][2]["value"]["effects"], [])

    def test_a_disabled_mask_is_marked_for_the_active_filter(self):
        moved = adjustments.apply(self.recipe(), {
            "mask:1": {"enabled": False}})
        self.assertIs(moved["operations"][1]["enabled"], False)

    def test_a_new_mask_lands_at_the_end_with_bounded_effects(self):
        moved = adjustments.apply(self.recipe(), {"+mask": [{
            "shape": "luma", "geometry": {"band": "midtones"},
            "effects": [{"op": "tone.exposure", "value": 40.0}]}]})
        added = moved["operations"][-1]
        self.assertEqual(added["op"], "mask.luma")
        self.assertIn("midtones", added["value"]["anchor"])
        self.assertEqual(added["value"]["effects"][0]["value"], 5.0)

    def test_mask_ordinals_hold_still_while_globals_are_inserted(self):
        """mask:1 must name the same mask before and after an insert."""
        moved = adjustments.apply(self.recipe(), {
            "+insert": [{"op": "detail.dehaze", "value": 10.0}],
            "mask:1": {"geometry": {"radius": 20.0}}})
        radial = next(item for item in moved["operations"]
                      if item["op"] == "mask.radial")
        self.assertIn("radius 20%", radial["value"]["anchor"])

    def test_the_changes_dict_is_json_serializable_for_the_cache(self):
        changes = {"+insert": [{"op": "detail.dehaze", "value": 10.0}],
                   "mask:1": {"geometry": {"radius": 20.0}},
                   "mask:1/tone.exposure": {"value": 1.0}}
        json.dumps(changes)
        adjustments.apply(self.recipe(), json.loads(json.dumps(changes)))


class FeatherTests(unittest.TestCase):
    """The engine finally reads the feather every schema promised."""

    def weights(self, feather=None):
        import numpy as np

        from development_engine import mask_weights

        value = {"anchor": "radial gradient, at 50%, 50%, radius 30%"}
        if feather is not None:
            value["feather"] = feather
        rgb = np.zeros((200, 300, 3), dtype=np.float32)
        return mask_weights(rgb, "radial", value)

    def test_absent_feather_renders_exactly_as_before(self):
        import numpy as np

        self.assertTrue(np.array_equal(self.weights(), self.weights(1.0)))

    def test_a_tight_feather_holds_a_core_at_full_weight(self):
        loose = self.weights(1.0)
        tight = self.weights(0.3)
        # Half way out: the tight mask still holds nearly full weight.
        self.assertGreater(float(tight[100, 150 + 45]),
                           float(loose[100, 150 + 45]))
        self.assertEqual(float(tight[100, 150]), 1.0)


class OverlayTests(unittest.TestCase):
    """The tint is the renderer's own weights, or it is a lie."""

    def test_the_overlay_is_a_png_with_weighted_alpha(self):
        import io

        from PIL import Image

        from opencull_gui import maskpaint

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "frame.png"
            Image.new("RGB", (300, 200), (40, 40, 60)).save(source)
            png = maskpaint.overlay_png(
                source, "radial",
                {"anchor": "radial gradient, at 50%, 50%, radius 30%",
                 "opacity": 1.0}, edge=300)
            sheet = Image.open(io.BytesIO(png))
            self.assertEqual(sheet.mode, "RGBA")
            centre = sheet.getpixel((150, 100))
            corner = sheet.getpixel((5, 5))
            self.assertGreater(centre[3], 100)
            self.assertEqual(corner[3], 0)

    def test_an_inverted_mask_tints_the_outside(self):
        import io

        from PIL import Image

        from opencull_gui import maskpaint

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "frame.png"
            Image.new("RGB", (300, 200), (40, 40, 60)).save(source)
            png = maskpaint.overlay_png(
                source, "radial",
                {"anchor": "radial gradient, at 50%, 50%, radius 30%, "
                           "inverted", "opacity": 1.0}, edge=300)
            sheet = Image.open(io.BytesIO(png))
            self.assertEqual(sheet.getpixel((150, 100))[3], 0)
            self.assertGreater(sheet.getpixel((5, 5))[3], 100)


if __name__ == "__main__":
    unittest.main()
