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


class CurveTests(unittest.TestCase):
    """The drawn curve folds into the recipe as one operation."""

    def test_a_curve_is_inserted_before_the_masks(self):
        base = recipe()
        base["operations"].append({
            "op": "mask.radial", "unit": "mask", "mode": "absolute",
            "value": {"anchor": "radial gradient at 50%, 50% radius 30%",
                      "effects": []}})
        out = adjustments.apply(base, {
            "curve": {"points": [[0, 0], [128, 190], [255, 255]]}})
        ops = [item["op"] for item in out["operations"]]
        self.assertIn("tone.curve", ops)
        self.assertLess(ops.index("tone.curve"), ops.index("mask.radial"))
        held = next(item for item in out["operations"]
                    if item["op"] == "tone.curve")
        self.assertEqual(held["value"]["points"][1], [128.0, 190.0])

    def test_drawing_again_replaces_rather_than_stacks(self):
        base = adjustments.apply(recipe(), {
            "curve": {"points": [[0, 0], [128, 190], [255, 255]]}})
        again = adjustments.apply(base, {
            "curve": {"points": [[0, 20], [255, 235]]}})
        curves = [item for item in again["operations"]
                  if item["op"] == "tone.curve"]
        self.assertEqual(len(curves), 1)
        self.assertEqual(curves[0]["value"]["points"][0], [0.0, 20.0])

    def test_points_are_clamped_to_the_eight_bit_frame(self):
        out = adjustments.apply(recipe(), {
            "curve": {"points": [[-40, 300], [512, -9]]}})
        held = next(item for item in out["operations"]
                    if item["op"] == "tone.curve")
        self.assertEqual(held["value"]["points"],
                         [[0.0, 255.0], [255.0, 0.0]])


class ColourMaskTests(unittest.TestCase):
    """Selected by what the pixels are, spelled like every other mask."""

    def test_the_anchor_carries_the_numbers_and_reads_back(self):
        out = adjustments.apply(recipe(), {"+mask": [{
            "shape": "color", "label": "the orange sun",
            "geometry": {"hue": 25, "range": 40, "softness": 25,
                         "sat_floor": 12, "opacity": 100, "feather": 100},
            "effects": [{"op": "tone.exposure", "value": 0.5}]}]})
        placed = adjustments.masks(out)
        self.assertEqual(placed[-1]["shape"], "color")
        told = placed[-1]["geometry"]
        self.assertEqual((told["hue"], told["range"],
                          told["softness"], told["sat_floor"]),
                         (25.0, 40.0, 25.0, 12.0))
        anchor = out["operations"][-1]["value"]["anchor"]
        self.assertIn("hue 25", anchor)
        self.assertIn("above 12% saturation", anchor)

    def test_an_unknown_shape_still_falls_back_to_radial(self):
        out = adjustments.apply(recipe(), {"+mask": [{
            "shape": "swirl", "geometry": {}, "effects": []}]})
        self.assertEqual(adjustments.masks(out)[-1]["shape"], "radial")


class MaskStructureTests(unittest.TestCase):
    """Renamed by hand, erased but never destroyed."""

    def masked(self):
        base = recipe()
        base["operations"].append({
            "op": "mask.radial", "unit": "mask", "mode": "absolute",
            "source": "sky", "enabled": True,
            "value": {"anchor": "radial gradient at 50%, 50% radius 30%",
                      "effects": []}})
        base["operations"].append({
            "op": "mask.linear", "unit": "mask", "mode": "absolute",
            "source": "ground", "enabled": True,
            "value": {"anchor": "linear gradient, from the bottom",
                      "effects": []}})
        return base

    def test_a_mask_can_be_renamed(self):
        out = adjustments.apply(self.masked(),
                                {"mask:2": {"label": "the horizon"}})
        self.assertEqual(adjustments.masks(out)[1]["label"], "the horizon")

    def test_erasure_disables_and_hides_but_never_removes(self):
        out = adjustments.apply(self.masked(), {"mask:1": {"deleted": True}})
        shown = adjustments.masks(out)
        self.assertEqual([m["id"] for m in shown], ["mask:2"])
        held = [op for op in out["operations"]
                if str(op.get("op", "")).startswith("mask.")]
        self.assertEqual(len(held), 2)          # still in the recipe
        self.assertTrue(held[0]["erased"])
        self.assertIs(held[0]["enabled"], False)

    def test_ordinals_survive_an_erasure(self):
        out = adjustments.apply(self.masked(), {"mask:1": {"deleted": True}})
        out = adjustments.apply(out, {"mask:2": {"geometry": {"reach": 25}}})
        linear = [op for op in out["operations"]
                  if op.get("op") == "mask.linear"][0]
        self.assertIn("up to 25", linear["value"]["anchor"])
        self.assertEqual(
            adjustments.mask_surface(out, 2)[0]["id"], "mask:2/tone.exposure")


class HslFoldTests(unittest.TestCase):
    """The colour bands fold in like everything else: asked kept, bounded."""

    def test_a_band_move_updates_the_models_own_operation(self):
        base = recipe()
        base["operations"].append({
            "op": "color.hsl_range", "channel": "blue",
            "component": "saturation", "value": 20.0,
            "unit": "percent", "mode": "delta", "enabled": True})
        out = adjustments.apply(base, {"+hsl": [
            {"channel": "blue", "component": "saturation", "value": -30}]})
        told = adjustments.hsl_state(out)[("blue", "saturation")]
        self.assertEqual(told["value"], -30.0)
        self.assertEqual(told["asked"], 20.0)   # what the model asked, kept

    def test_a_new_band_move_is_inserted_before_the_masks(self):
        base = recipe()
        base["operations"].append({
            "op": "mask.radial", "unit": "mask", "mode": "absolute",
            "value": {"anchor": "radial gradient at 50%, 50% radius 30%",
                      "effects": []}})
        out = adjustments.apply(base, {"+hsl": [
            {"channel": "orange", "component": "lightness", "value": 15}]})
        ops = [item["op"] for item in out["operations"]]
        self.assertLess(ops.index("color.hsl_range"),
                        ops.index("mask.radial"))

    def test_a_hue_turn_is_bounded_tighter_than_the_rest(self):
        out = adjustments.apply(recipe(), {"+hsl": [
            {"channel": "red", "component": "hue", "value": 300},
            {"channel": "red", "component": "saturation", "value": 300}]})
        state = adjustments.hsl_state(out)
        self.assertEqual(state[("red", "hue")]["value"], 45.0)
        self.assertEqual(state[("red", "saturation")]["value"], 100.0)


class CropFoldTests(unittest.TestCase):
    """The photographer's frame folds in, and folds back out."""

    def test_a_frame_and_an_angle_become_operations(self):
        out = adjustments.apply(recipe(), {"crop": {
            "left": 0.1, "top": 0.2, "width": 0.5, "height": 0.5,
            "angle": 1.5}})
        held = {op["op"]: op for op in out["operations"]}
        self.assertEqual(held["geometry.crop"]["value"]["width"], 0.5)
        self.assertEqual(held["geometry.rotation"]["value"], 1.5)

    def test_the_whole_frame_level_removes_what_the_hand_added(self):
        framed = adjustments.apply(recipe(), {"crop": {
            "left": 0.1, "top": 0.2, "width": 0.5, "height": 0.5,
            "angle": 1.5}})
        back = adjustments.apply(framed, {"crop": {
            "left": 0, "top": 0, "width": 1, "height": 1, "angle": 0}})
        ops = [op["op"] for op in back["operations"]]
        self.assertNotIn("geometry.crop", ops)
        self.assertNotIn("geometry.rotation", ops)

    def test_a_degenerate_ask_is_bounded_not_believed(self):
        out = adjustments.apply(recipe(), {"crop": {
            "left": 0.99, "top": -1, "width": 0.001, "height": 9,
            "angle": 80}})
        held = {op["op"]: op for op in out["operations"]}
        value = held["geometry.crop"]["value"]
        self.assertGreaterEqual(value["width"], 0.05)
        self.assertLessEqual(value["left"] + value["width"], 1.0)
        self.assertEqual(held["geometry.rotation"]["value"], 15.0)


class BrushMaskTests(unittest.TestCase):
    """The painted mask travels inside the recipe, strokes and all."""

    def test_an_added_brush_carries_its_map(self):
        out = adjustments.apply(recipe(), {"+mask": [{
            "shape": "brush", "label": "dodge the rocks", "map": "AAAA",
            "geometry": {"opacity": 100, "feather": 60},
            "effects": [{"op": "tone.exposure", "value": 0.4}]}]})
        op = out["operations"][-1]
        self.assertEqual(op["op"], "mask.brush")
        self.assertEqual(op["value"]["map"], "AAAA")
        self.assertEqual(op["value"]["anchor"], "painted by hand")

    def test_a_new_stroke_replaces_the_map(self):
        base = adjustments.apply(recipe(), {"+mask": [{
            "shape": "brush", "map": "AAAA", "geometry": {},
            "effects": []}]})
        out = adjustments.apply(base, {"mask:1": {"map": "BBBB"}})
        self.assertEqual(out["operations"][-1]["value"]["map"], "BBBB")


class CurveColourHoldFoldTests(unittest.TestCase):
    """The colour-hold dial travels inside the drawn curve."""

    def test_preserve_folds_into_the_curve_operation(self):
        out = adjustments.apply(recipe(), {"curve": {
            "points": [[0, 0], [128, 96], [255, 255]], "preserve": 60}})
        op = next(item for item in out["operations"]
                  if item.get("op") == "tone.curve")
        self.assertEqual(op["value"]["preserve"], 60.0)

    def test_redrawing_without_the_field_drops_the_hold(self):
        base = adjustments.apply(recipe(), {"curve": {
            "points": [[0, 0], [128, 96], [255, 255]], "preserve": 60}})
        out = adjustments.apply(base, {"curve": {
            "points": [[0, 0], [128, 110], [255, 255]]}})
        op = next(item for item in out["operations"]
                  if item.get("op") == "tone.curve")
        self.assertNotIn("preserve", op["value"])

    def test_the_hold_is_bounded(self):
        out = adjustments.apply(recipe(), {"curve": {
            "points": [[0, 0], [128, 96], [255, 255]], "preserve": 250}})
        op = next(item for item in out["operations"]
                  if item.get("op") == "tone.curve")
        self.assertEqual(op["value"]["preserve"], 100.0)


class WedgeAndEvenerTests(unittest.TestCase):
    """The colour wedge's walls and eveners travel through the anchor."""

    def colour_mask(self, geometry=None):
        return adjustments.apply(recipe(), {"+mask": [{
            "shape": "color",
            "geometry": {"hue": 200, "range": 40, "softness": 20,
                         "sat_floor": 8, **(geometry or {})},
            "effects": []}]})

    def test_walls_round_trip_through_the_anchor(self):
        out = self.colour_mask({"sat_ceiling": 70, "light_floor": 25,
                                "light_ceiling": 90})
        placed = adjustments.masks(out)[-1]
        self.assertEqual(placed["geometry"]["sat_ceiling"], 70.0)
        self.assertEqual(placed["geometry"]["light_floor"], 25.0)
        self.assertEqual(placed["geometry"]["light_ceiling"], 90.0)

    def test_unasked_walls_stay_out_of_the_sentence(self):
        out = self.colour_mask()
        anchor = out["operations"][-1]["value"]["anchor"]
        self.assertNotIn("below", anchor)
        self.assertNotIn("brighter", anchor)
        self.assertNotIn("darker", anchor)

    def test_the_aim_rides_the_anchor(self):
        base = self.colour_mask()
        out = adjustments.apply(base, {"mask:1": {"geometry": {
            "aim_sat": 55, "aim_light": 60}}})
        anchor = out["operations"][-1]["value"]["anchor"]
        self.assertIn("target saturation 55", anchor)
        self.assertIn("target light 60", anchor)

    def test_an_evener_lands_and_moves_when_asked_again(self):
        base = self.colour_mask()
        once = adjustments.apply(base, {"mask:1": {"add_effects": [
            {"op": "uniformity.hue", "value": 40.0}]}})
        held = once["operations"][-1]["value"]["effects"]
        self.assertEqual(held[-1]["op"], "uniformity.hue")
        self.assertEqual(held[-1]["value"], 40.0)
        again = adjustments.apply(once, {"mask:1": {"add_effects": [
            {"op": "uniformity.hue", "value": 70.0}]}})
        held = again["operations"][-1]["value"]["effects"]
        self.assertEqual(len([e for e in held
                              if e["op"] == "uniformity.hue"]), 1)
        self.assertEqual(held[-1]["value"], 70.0)

    def test_eveners_stay_off_the_control_surface(self):
        out = adjustments.apply(self.colour_mask(), {"mask:1": {
            "add_effects": [{"op": "uniformity.hue", "value": 40.0}]}})
        names = [item["op"] for item in adjustments.mask_surface(out, 1)]
        self.assertNotIn("uniformity.hue", names)


class CleanColourControlTests(unittest.TestCase):
    """The cleaner is an ordinary control in the Detail section."""

    def test_clean_colour_sits_on_the_full_surface(self):
        surface = adjustments.full_surface(recipe())
        found = next(item for item in surface
                     if item["op"] == "detail.clean_colour")
        self.assertEqual(found["section"], "Detail")
        self.assertEqual(found["label"], "Clean colour")
        self.assertEqual((found["low"], found["high"]), (0.0, 100.0))
