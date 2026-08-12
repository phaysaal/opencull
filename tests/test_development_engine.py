import tempfile
import unittest
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

from development_engine import (
    DevelopmentError,
    _apply_global,
    apply_adjustment_draft,
    render_recipe,
)


def write_baseline(path: Path, array: np.ndarray) -> Path:
    """Write a 16-bit linear baseline the way the renderer writes one.

    Pillow cannot represent 16-bit RGB, so building these fixtures through
    Image.fromarray both failed and diverged from renderer_comparison, which
    writes baselines with tifffile and reads them back the same way.
    """
    tifffile.imwrite(path, np.ascontiguousarray(array, dtype=np.uint16))
    return path


class DevelopmentEngineTests(unittest.TestCase):
    def recipe(self, diagnostics=None):
        return {
            "format": "opencull-development-recipe-v1",
            "source_photo": "A.RAF", "source_kind": "raw", "style": "standard",
            "operations": [{"op": "tone.exposure", "value": 1, "mode": "delta"},
                           {"op": "color.saturation", "value": 10, "mode": "delta"}],
            "diagnostics": diagnostics or [],
        }

    def test_render_is_new_jpeg_with_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.tiff"
            array = np.full((32, 48, 3), 12000, dtype=np.uint16)
            write_baseline(baseline, array)
            result = render_recipe(baseline, self.recipe(), root / "out")
            output = Path(result["output"]["path"])
            self.assertTrue(output.is_file())
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (48, 32))
            self.assertTrue(Path(output.with_suffix(".render.json")).is_file())
            self.assertTrue(baseline.is_file())

    def test_unsupported_diagnostics_are_never_executed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.tiff"
            Image.new("RGB", (8, 8), "white").save(baseline)
            with self.assertRaisesRegex(DevelopmentError, "unsupported"):
                render_recipe(baseline, self.recipe([{"instruction": "mask"}]), root / "out")

    def test_allow_incomplete_marks_preview_as_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.tiff"
            Image.new("RGB", (8, 8), "white").save(baseline)
            result = render_recipe(
                baseline, self.recipe([{"instruction": "mask"}]), root / "out",
                allow_incomplete=True)
            self.assertFalse(result["complete"])

    def test_hsl_range_only_changes_matching_colour_family(self):
        rgb = np.zeros((1, 2, 3), dtype=np.float32)
        rgb[0, 0] = (0.9, 0.1, 0.1)  # red
        rgb[0, 1] = (0.1, 0.1, 0.9)  # blue
        result = _apply_global(rgb, [{
            "op": "color.hsl_range", "channel": "red",
            "component": "saturation", "value": -50,
        }])
        self.assertLess(result[0, 0, 0] - result[0, 0, 1],
                        rgb[0, 0, 0] - rgb[0, 0, 1])
        np.testing.assert_allclose(result[0, 1], rgb[0, 1], atol=1e-5)

    def saturation(self, rgb):
        top, bottom = rgb.max(axis=2), rgb.min(axis=2)
        return float(np.mean(np.where(top > 0, (top - bottom) / np.maximum(top, 1e-6), 0)))

    def test_a_tonal_move_changes_brightness_and_leaves_colour_alone(self):
        """Contrast is a tone control, and was acting as a colour one.

        Applied per channel it pulls them apart, so a recipe that asked for
        less blue and more contrast got a bluer sky: measured on a real
        frame, three colour families the model asked to quieten all rose,
        one of them by 32 points.
        """
        rgb = np.zeros((1, 3, 3), dtype=np.float32)
        rgb[0, 0] = (0.36, 0.09, 0.05)
        rgb[0, 1] = (0.05, 0.14, 0.40)
        rgb[0, 2] = (0.22, 0.24, 0.10)
        raised = _apply_global(rgb, [{"op": "tone.contrast", "value": 20}])

        luma = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        self.assertGreater(float((raised * luma).sum(axis=2).max()),
                           float((rgb * luma).sum(axis=2).max()))
        self.assertAlmostEqual(
            self.saturation(raised), self.saturation(rgb), places=3)

    def test_a_colour_instruction_survives_the_tone_instructions_after_it(self):
        rgb = np.full((1, 1, 3), 0.1, dtype=np.float32)
        rgb[0, 0] = (0.06, 0.16, 0.45)
        quieter = _apply_global(rgb, [
            {"op": "color.hsl_range", "channel": "blue/teal",
             "component": "saturation", "value": -20},
            {"op": "tone.contrast", "value": 18},
            {"op": "tone.black", "value": -12},
        ])
        self.assertLess(self.saturation(quieter), self.saturation(rgb))

    def test_a_dense_tonal_recipe_does_not_crush_or_clip_the_frame(self):
        # The numbers from a real signature treatment. Rendered per channel
        # in linear light they crushed 6% of the frame to black and clipped
        # 5% to white.
        gradient = np.linspace(0.002, 0.98, 256, dtype=np.float32)
        rgb = np.repeat(np.repeat(gradient[None, :, None], 64, axis=0), 3, axis=2)
        rgb[..., 2] *= 1.15
        result = _apply_global(rgb, [
            {"op": "tone.contrast", "value": 17},
            {"op": "tone.highlight", "value": -35},
            {"op": "tone.shadow", "value": 11.5},
            {"op": "tone.white", "value": -14},
            {"op": "tone.black", "value": -13},
        ])
        def crushed(image):
            return float((image.max(axis=2) <= 0.004).mean())

        def clipped(image):
            return float((image.max(axis=2) >= 1.0).mean())

        # Measured against what the frame arrived with, so the fixture's
        # own blown corner is not counted against the recipe.
        self.assertLess(crushed(result) - crushed(rgb), 0.02,
                        "the recipe crushed the shadows away")
        self.assertLess(clipped(result) - clipped(rgb), 0.02,
                        "the recipe clipped the highlights away")

    def test_the_detail_controls_are_executed_rather_than_recorded(self):
        """Six sharpening instructions across seven frames did nothing.

        A recipe that names a control the renderer ignores is a treatment
        the photographer was shown and did not receive.
        """
        rng = np.random.default_rng(7)
        frame = (rng.random((48, 64, 3)).astype(np.float32) * 0.4 + 0.3)

        def energy(image):
            grey = (image * np.array([0.2126, 0.7152, 0.0722])).sum(axis=2)
            return float(np.mean(np.abs(np.diff(grey, axis=0)))
                         + np.mean(np.abs(np.diff(grey, axis=1))))

        sharper = _apply_global(frame, [
            {"op": "detail.sharpen_amount", "value": 150}])
        self.assertGreater(energy(sharper), energy(frame))

        softer = _apply_global(frame, [
            {"op": "detail.denoise_luminance", "value": 40}])
        self.assertLess(energy(softer), energy(frame))

        for op, value in (("levels.midpoint", 0.8), ("finish.vignette", -20)):
            changed = _apply_global(frame, [{"op": op, "value": value}])
            self.assertFalse(np.allclose(changed, frame), f"{op} did nothing")

    def test_sharpening_does_not_colour_the_edges_it_sharpens(self):
        # Brightness only: sharpening the colour channels separately is
        # how an edge picks up a fringe that was never photographed.
        frame = np.zeros((8, 8, 3), dtype=np.float32)
        frame[:, 4:] = 0.6
        sharper = _apply_global(frame, [
            {"op": "detail.sharpen_amount", "value": 200}])
        spread = sharper.max(axis=2) - sharper.min(axis=2)
        self.assertLess(float(spread.max()), 0.02)

    def test_linear_mask_blends_only_its_anchor_region(self):
        rgb = np.full((4, 1, 3), 0.5, dtype=np.float32)
        result = _apply_global(rgb, [{
            "op": "mask.linear",
            "value": {"anchor": "bottom up", "opacity": 1.0,
                       "effects": [{"op": "tone.exposure", "value": 1,
                                     "mode": "delta"}]},
        }])
        self.assertGreater(result[-1, 0, 0], result[0, 0, 0])

    def test_small_brightness_does_not_raise_scene_black_floor(self):
        rgb = np.zeros((2, 2, 3), dtype=np.float32)
        result = _apply_global(rgb, [{"op": "tone.brightness", "value": 5}])
        self.assertLess(float(result.max()), 0.002)

    def test_reference_jpeg_is_recorded_as_calibration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.tiff"
            reference = root / "camera.jpg"
            write_baseline(baseline, np.full((12, 16, 3), 7000, dtype=np.uint16))
            Image.new("RGB", (16, 12), (180, 150, 120)).save(reference)
            result = render_recipe(
                baseline, self.recipe(), root / "out",
                reference_jpeg=reference)
            self.assertEqual(
                result["calibration"]["method"],
                "per-channel-srgb-quantile-lut-with-highlight-rolloff")
            # The match is held in the tones and released in the
            # highlights, and the record says where.
            self.assertLess(result["calibration"]["rolloff"]["holds_below"],
                            result["calibration"]["rolloff"]["released_above"])

    def test_distinct_standard_and_personal_operations_change_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.tiff"
            gradient = np.linspace(2000, 50000, 48, dtype=np.uint16)
            array = np.repeat(gradient[None, :, None], 32, axis=0)
            array = np.repeat(array, 3, axis=2)
            write_baseline(baseline, array)
            standard = self.recipe()
            standard["style"] = "standard"
            standard["operations"] = [{
                "op": "tone.contrast", "value": 5, "mode": "delta"}]
            personal = self.recipe()
            personal["style"] = "personal"
            personal["operations"] = [
                {"op": "tone.contrast", "value": 20, "mode": "delta"},
                {"op": "color.temperature", "value": -400,
                 "mode": "delta"},
            ]
            first = render_recipe(baseline, standard, root / "out")
            second = render_recipe(baseline, personal, root / "out")
            self.assertNotEqual(
                first["output"]["sha256"], second["output"]["sha256"])

    def test_adjustment_draft_creates_new_bounded_recipe_revision(self):
        recipe = self.recipe()
        revised = apply_adjustment_draft(recipe, {
            "revision": 2, "changes": [{"control": "midtones"}, {"control": "cyan"}],
        })
        self.assertEqual(revised["revision"], 1)
        self.assertEqual(len(recipe["operations"]), 2)
        self.assertEqual([item["op"] for item in revised["operations"]][-2:],
                         ["tone.exposure", "color.hsl_range"])


if __name__ == "__main__":
    unittest.main()
