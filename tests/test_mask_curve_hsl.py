"""A mask layer answers to the same curve and colour-band controls
the base layer does -- scoped to what the mask actually covers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui import adjustments  # noqa: E402


def recipe_with_mask() -> dict:
    return {"format": "opencull-development-recipe-v1", "operations": [
        {"op": "mask.radial", "unit": "mask", "mode": "absolute",
         "enabled": True, "value": {
             "anchor": "radial gradient at 50%, 50% radius 60%",
             "opacity": 1.0, "feather": 1.0, "effects": []}},
    ]}


class MaskCurveCompileTests(unittest.TestCase):
    """Drawing a curve on a mask writes it into that mask alone."""

    def test_a_curve_lands_in_the_masks_own_effects(self):
        recipe = recipe_with_mask()
        applied = adjustments.apply(recipe, {"mask:1": {"curve": {
            "points": [[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]],
            "preserve": 40.0}}})
        placed = adjustments.masks(applied)
        self.assertEqual(placed[0]["curve"]["points"],
                         [[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        self.assertEqual(placed[0]["curve"]["preserve"], 40.0)
        # It lives inside the mask's own effects, not among the
        # frame's operations -- a base-layer reader sees nothing new.
        self.assertFalse(any(
            isinstance(op, dict) and op.get("op") == "tone.curve"
            for op in applied["operations"]))

    def test_redrawing_replaces_rather_than_duplicates(self):
        recipe = recipe_with_mask()
        applied = adjustments.apply(recipe, {"mask:1": {"curve": {
            "points": [[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]]}}})
        applied = adjustments.apply(applied, {"mask:1": {"curve": {
            "points": [[0.0, 0.0], [128.0, 60.0], [255.0, 255.0]]}}})
        effects = applied["operations"][0]["value"]["effects"]
        curves = [item for item in effects
                 if isinstance(item, dict) and item.get("op") == "tone.curve"]
        self.assertEqual(len(curves), 1)
        self.assertEqual(curves[0]["value"]["points"][1], [128.0, 60.0])

    def test_the_identity_curve_removes_the_effect(self):
        recipe = recipe_with_mask()
        applied = adjustments.apply(recipe, {"mask:1": {"curve": {
            "points": [[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]]}}})
        applied = adjustments.apply(applied, {"mask:1": {"curve": {
            "points": []}}})
        effects = applied["operations"][0]["value"]["effects"]
        self.assertFalse(any(
            isinstance(item, dict) and item.get("op") == "tone.curve"
            for item in effects))


class MaskHslCompileTests(unittest.TestCase):
    """A colour band moved on a mask lands inside that mask alone."""

    def test_a_band_lands_in_the_masks_own_effects(self):
        recipe = recipe_with_mask()
        applied = adjustments.apply(recipe, {"mask:1": {"hsl": [
            {"channel": "blue", "component": "saturation",
             "value": 30.0}]}})
        state = adjustments.mask_hsl_state(applied, 1)
        self.assertEqual(state[("blue", "saturation")]["value"], 30.0)
        # And the whole-frame reading knows nothing about it.
        self.assertEqual(adjustments.hsl_state(applied), {})

    def test_moving_it_back_to_zero_removes_the_effect(self):
        recipe = recipe_with_mask()
        applied = adjustments.apply(recipe, {"mask:1": {"hsl": [
            {"channel": "blue", "component": "saturation",
             "value": 30.0}]}})
        applied = adjustments.apply(applied, {"mask:1": {"hsl": [
            {"channel": "blue", "component": "saturation",
             "value": 0.0}]}})
        self.assertEqual(adjustments.mask_hsl_state(applied, 1), {})

    def test_bands_on_two_masks_stay_apart(self):
        recipe = {"format": "opencull-development-recipe-v1", "operations": [
            {"op": "mask.radial", "unit": "mask", "mode": "absolute",
             "enabled": True, "value": {
                 "anchor": "radial gradient", "opacity": 1.0,
                 "feather": 1.0, "effects": []}},
            {"op": "mask.linear", "unit": "mask", "mode": "absolute",
             "enabled": True, "value": {
                 "anchor": "linear gradient from the bottom",
                 "opacity": 1.0, "feather": 1.0, "effects": []}},
        ]}
        applied = adjustments.apply(recipe, {"mask:2": {"hsl": [
            {"channel": "orange", "component": "hue", "value": 10.0}]}})
        self.assertEqual(adjustments.mask_hsl_state(applied, 1), {})
        self.assertIn(("orange", "hue"),
                      adjustments.mask_hsl_state(applied, 2))


class MaskCurveRenderTests(unittest.TestCase):
    """The curve and the band actually change pixels, only where the
    mask reaches."""

    def test_the_curve_only_moves_pixels_inside_the_mask(self):
        from development_engine import _apply_global

        held = np.full((40, 40, 3), 0.5, np.float32)
        recipe = {"operations": [
            {"op": "mask.radial", "unit": "mask", "mode": "absolute",
             "enabled": True, "value": {
                 "anchor": "radial gradient at 20%, 20% radius 10%",
                 "opacity": 1.0, "feather": 0.3, "effects": [
                     {"op": "tone.curve", "unit": "curve", "mode": "absolute",
                      "value": {"points": [[0.0, 0.0], [128.0, 200.0],
                                           [255.0, 255.0]]}}]}}]}
        out = _apply_global(held, recipe["operations"])
        inside = out[8, 8].mean()
        outside = out[30, 30].mean()
        self.assertGreater(inside, held[8, 8].mean() + 0.05)
        self.assertAlmostEqual(float(outside), 0.5, places=2)

    def test_the_band_only_moves_pixels_inside_the_mask(self):
        from development_engine import _apply_global

        held = np.zeros((40, 40, 3), np.float32)
        held[..., 2] = 0.6                     # a plainly blue frame
        held[..., 0] = held[..., 1] = 0.1
        recipe = {"operations": [
            {"op": "mask.radial", "unit": "mask", "mode": "absolute",
             "enabled": True, "value": {
                 "anchor": "radial gradient at 20%, 20% radius 10%",
                 "opacity": 1.0, "feather": 0.3, "effects": [
                     {"op": "color.hsl_range", "channel": "blue",
                      "component": "lightness", "value": 80.0,
                      "unit": "percent", "mode": "delta"}]}}]}
        out = _apply_global(held, recipe["operations"])
        inside = float(out[8, 8].mean())
        outside = float(out[30, 30].mean())
        self.assertGreater(inside, float(held[8, 8].mean()) + 0.01)
        self.assertAlmostEqual(outside, float(held[30, 30].mean()),
                               places=2)


if __name__ == "__main__":                           # pragma: no cover
    unittest.main()
