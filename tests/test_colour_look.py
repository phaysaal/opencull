"""The look tool: fitted from a chart, or authored as a preference."""

from __future__ import annotations

import colorsys
import tempfile
import unittest
from pathlib import Path

import numpy as np

import colour_look as cl
from development_engine import _ENCODE_GAMMA, _apply_global


def paint_chart(patches: np.ndarray) -> np.ndarray:
    """A 600x400 chart image with the 24 patches in their grid."""
    image = np.zeros((400, 600, 3), np.float32)
    for row in range(4):
        for col in range(6):
            image[row * 100:(row + 1) * 100,
                  col * 100:(col + 1) * 100] = patches[row * 6 + col]
    return image


FULL = [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]


class PatchSamplingTests(unittest.TestCase):
    def test_a_painted_chart_reads_back_exactly(self):
        wanted = np.asarray(cl.CC24_SRGB, np.float32) / 255.0
        sampled = cl.patch_means(paint_chart(wanted), FULL)
        self.assertTrue(np.allclose(sampled, wanted, atol=1e-3))

    def test_eight_numbers_or_nothing(self):
        with self.assertRaises(cl.LookError):
            cl.patch_means(paint_chart(
                np.zeros((24, 3), np.float32)), [0, 0, 1, 1])


class ChartFitTests(unittest.TestCase):
    """A known distortion goes in; the fitted look takes it back out."""

    def distorted(self):
        true_display = np.asarray(cl.CC24_SRGB, np.float32) / 255.0
        cast = np.array([1.12, 1.0, 0.86], np.float32)
        mix = np.array([[0.92, 0.10, -0.02],
                        [0.04, 0.94, 0.02],
                        [-0.02, 0.12, 0.90]], np.float32)
        linear = true_display ** _ENCODE_GAMMA
        measured_linear = (linear @ mix.T) * cast
        measured_display = np.clip(
            measured_linear, 1e-6, None) ** (1 / _ENCODE_GAMMA)
        return true_display, measured_linear, measured_display

    def test_the_fit_takes_the_cast_and_crosstalk_back_out(self):
        true_display, measured_linear, measured_display = self.distorted()
        ops, report = cl.fit_look(paint_chart(measured_display), FULL)
        out = _apply_global(
            measured_linear.reshape(4, 6, 3).astype(np.float32), ops)
        out_display = np.clip(out, 1e-6, None) ** (1 / _ENCODE_GAMMA)
        before = float(np.abs(measured_display - true_display).mean())
        after = float(np.abs(
            out_display.reshape(24, 3) - true_display).mean())
        self.assertLess(after, before / 10.0)
        self.assertLess(report["residual_after"],
                        report["residual_before"])

    def test_the_lattice_carries_only_a_residue(self):
        _true, _lin, measured_display = self.distorted()
        _ops, report = cl.fit_look(paint_chart(measured_display), FULL)
        # The matrix does the heavy lifting; the warp whispers.
        self.assertLess(report["lattice_peak"], 0.05)

    def test_the_lattice_cannot_hold_a_cliff(self):
        _true, _lin, measured_display = self.distorted()
        ops, _report = cl.fit_look(paint_chart(measured_display), FULL)
        import base64

        value = ops[-1]["value"]
        size = value["size"]
        lattice = np.frombuffer(
            base64.b64decode(value["lattice"]),
            dtype=np.float16).astype(np.float32).reshape(
                size, size, size, 3)
        steps = [np.abs(np.diff(lattice, axis=axis)).max()
                 for axis in (0, 1, 2)]
        self.assertLess(float(max(steps)), 0.03)


class StandardLookTests(unittest.TestCase):
    def walked(self, patch, strength=100.0):
        ops = cl.standard_look(strength)
        linear = (np.asarray(patch, np.float32)
                  ** _ENCODE_GAMMA).reshape(1, 1, 3)
        out = _apply_global(linear, ops)
        return np.clip(out[0, 0], 1e-6, None) ** (1 / _ENCODE_GAMMA)

    def test_skin_walks_toward_red_and_keeps_its_light(self):
        before = (0.76, 0.59, 0.50)
        after = self.walked(before)
        self.assertLess(colorsys.rgb_to_hsv(*after)[0],
                        colorsys.rgb_to_hsv(*before)[0])
        self.assertAlmostEqual(float(after.max()), 0.76, places=2)

    def test_sky_walks_toward_cyan(self):
        before = (0.35, 0.55, 0.85)
        after = self.walked(before)
        self.assertLess(colorsys.rgb_to_hsv(*after)[0] * 360,
                        colorsys.rgb_to_hsv(*before)[0] * 360 - 2.0)

    def test_colour_gains_a_breath_of_chroma(self):
        before = (0.76, 0.59, 0.50)
        after = self.walked(before)
        self.assertGreater(colorsys.rgb_to_hsv(*after)[1],
                           colorsys.rgb_to_hsv(*before)[1])

    def test_neutrals_never_move(self):
        for level in (0.1, 0.5, 0.9):
            after = self.walked((level, level, level))
            self.assertTrue(
                np.allclose(after, level, atol=2e-3), msg=str(level))

    def test_zero_strength_is_identity(self):
        after = self.walked((0.76, 0.59, 0.50), strength=0.0)
        self.assertTrue(np.allclose(
            after, (0.76, 0.59, 0.50), atol=2e-3))


class KeepingALookTests(unittest.TestCase):
    def test_a_look_is_an_ordinary_preset(self):
        from opencull_gui import presets

        with tempfile.TemporaryDirectory() as folder:
            kept = cl.save_look(
                "Test standard", cl.standard_look(100.0),
                intent="test", root=Path(folder))
            self.assertEqual(kept["name"], "Test standard")
            listed = presets.saved(Path(folder))
            self.assertEqual(len(listed), 1)
            ops = listed[0]["operations"]
            self.assertEqual(ops[-1]["op"], "color.warp")
            self.assertIn("lattice", ops[-1]["value"])


class LookThroughTheStripTests(unittest.TestCase):
    def test_a_saved_look_renders_through_the_preset_path(self):
        from opencull_gui import presets

        with tempfile.TemporaryDirectory() as folder:
            cl.save_look("Fuji daylight", cl.standard_look(100.0),
                         root=Path(folder))
            look = presets.saved(Path(folder))[0]
            recipe = presets.recipe_for(look, "A.RAF", "raw")
            self.assertIn("color.warp",
                          [op["op"] for op in recipe["operations"]])
            img = np.full((4, 4, 3), (0.5, 0.3, 0.2), np.float32)
            out = _apply_global(img, recipe["operations"])
            self.assertFalse(np.allclose(out, img, atol=1e-4))


if __name__ == "__main__":
    unittest.main()
