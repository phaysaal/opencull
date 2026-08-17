import tempfile
import unittest
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

from development_engine import (
    _curve_lut,
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


class SwitchedOffTests(unittest.TestCase):
    """An operation the photographer switched off must not be applied.

    The fine-tuning page writes the switch onto the operation, and the
    renderer read past it: the control said the adjustment was off, the
    label said so, and the frame came back with it applied anyway.
    """

    def render(self, operations, name):
        root = Path(self._temporary.name)
        baseline = root / "baseline.tiff"
        write_baseline(baseline, np.full((16, 24, 3), 9000, dtype=np.uint16))
        result = render_recipe(baseline, {
            "format": "opencull-development-recipe-v1",
            "source_photo": "A.RAF", "source_kind": "raw",
            "style": "standard", "operations": operations,
        }, root / name)
        with Image.open(result["output"]["path"]) as image:
            grey = np.asarray(image.convert("L"), dtype=np.float32)
        return float(grey.mean()), result

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

    def lift(self, **extra):
        return [{"op": "tone.exposure", "value": 1.0, "unit": "EV",
                 "mode": "delta", **extra}]

    def test_a_switched_off_adjustment_changes_nothing(self):
        untouched, _ = self.render([], "none")
        applied, _ = self.render(self.lift(), "on")
        switched_off, _ = self.render(self.lift(enabled=False), "off")
        self.assertGreater(applied, untouched + 20)
        self.assertAlmostEqual(switched_off, untouched, places=4)

    def test_it_is_not_counted_or_named_while_it_is_not_happening(self):
        root = Path(self._temporary.name)
        baseline = root / "baseline.tiff"
        write_baseline(baseline, np.full((16, 24, 3), 9000, dtype=np.uint16))
        seen = []
        render_recipe(baseline, {
            "format": "opencull-development-recipe-v1",
            "source_photo": "A.RAF", "source_kind": "raw",
            "style": "standard",
            "operations": [
                *self.lift(enabled=False),
                {"op": "tone.contrast", "value": 8.0, "unit": "percent",
                 "mode": "delta"},
            ],
        }, root / "counted", progress=lambda done, total, what: seen.append(
            (done, total, what)))
        named = [what for _done, _total, what in seen]
        self.assertNotIn("tone.exposure", named)
        self.assertIn("tone.contrast", named)
        # Three stages plus the one adjustment that is actually happening.
        self.assertEqual({total for _done, total, _what in seen}, {4})

    def test_a_switched_off_crop_leaves_the_frame_its_own_shape(self):
        _brightness, kept = self.render([
            {"op": "geometry.crop_aspect", "value": [1, 1], "unit": "ratio",
             "mode": "absolute", "enabled": False}], "crop")
        with Image.open(kept["output"]["path"]) as image:
            self.assertEqual(image.size, (24, 16))


class InfraredOperationTests(unittest.TestCase):
    """The two moves infrared work needs and ordinary work does not."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

    def cast(self, red=0.18, green=0.18, blue=0.40):
        """A frame with the blue cast an infrared decode comes back with."""
        frame = np.zeros((6, 8, 3), np.float32)
        frame[..., 0], frame[..., 1], frame[..., 2] = red, green, blue
        return frame

    def averages(self, frame):
        return frame.reshape(-1, 3).mean(axis=0)

    def test_neutralising_brings_the_channels_into_agreement(self):
        out = _apply_global(self.cast(), [
            {"op": "color.neutralize", "value": 100, "unit": "percent",
             "mode": "absolute"}])
        red, green, blue = self.averages(out)
        self.assertAlmostEqual(red, blue, places=5)
        self.assertAlmostEqual(green, blue, places=5)

    def spread(self, frame):
        means = self.averages(frame)
        return float(means.max() - means.min())

    def test_it_can_be_asked_for_by_halves(self):
        before = self.spread(self.cast())
        out = _apply_global(self.cast(), [
            {"op": "color.neutralize", "value": 50, "unit": "percent",
             "mode": "absolute"}])
        self.assertAlmostEqual(self.spread(out), before / 2, places=4)

    def test_neutralising_nothing_changes_nothing(self):
        frame = self.cast()
        out = _apply_global(frame, [
            {"op": "color.neutralize", "value": 0, "unit": "percent",
             "mode": "absolute"}])
        self.assertTrue(np.allclose(out, frame))

    def test_an_already_neutral_frame_survives_it(self):
        frame = self.cast(0.2, 0.2, 0.2)
        out = _apply_global(frame, [
            {"op": "color.neutralize", "value": 100, "unit": "percent",
             "mode": "absolute"}])
        self.assertTrue(np.allclose(out, frame, atol=1e-6))

    def test_swapping_red_and_blue_swaps_red_and_blue(self):
        frame = self.cast(0.30, 0.20, 0.05)
        out = _apply_global(frame, [
            {"op": "color.channel_mixer",
             "value": [[0, 0, 1], [0, 1, 0], [1, 0, 0]],
             "unit": "matrix", "mode": "absolute"}])
        red, green, blue = self.averages(out)
        self.assertAlmostEqual(red, 0.05, places=5)
        self.assertAlmostEqual(green, 0.20, places=5)
        self.assertAlmostEqual(blue, 0.30, places=5)

    def test_a_mixer_that_is_not_a_matrix_is_ignored_not_fatal(self):
        frame = self.cast()
        out = _apply_global(frame, [
            {"op": "color.channel_mixer", "value": [1, 2, 3],
             "unit": "matrix", "mode": "absolute"}])
        self.assertTrue(np.allclose(out, frame))

    def test_a_mixer_never_produces_negative_light(self):
        frame = self.cast(0.30, 0.20, 0.05)
        out = _apply_global(frame, [
            {"op": "color.channel_mixer",
             "value": [[1, -2, 0], [0, 1, 0], [0, 0, 1]],
             "unit": "matrix", "mode": "absolute"}])
        self.assertGreaterEqual(float(out.min()), 0.0)

    def test_both_survive_a_whole_render(self):
        root = Path(self._temporary.name)
        baseline = root / "baseline.tiff"
        array = np.zeros((12, 16, 3), dtype=np.uint16)
        array[..., 0], array[..., 1], array[..., 2] = 3000, 3000, 9000
        write_baseline(baseline, array)
        result = render_recipe(baseline, {
            "format": "opencull-development-recipe-v1",
            "source_photo": "A.ARW", "source_kind": "raw", "style": "standard",
            "operations": [
                {"op": "color.neutralize", "value": 100, "unit": "percent",
                 "mode": "absolute"},
                {"op": "color.channel_mixer",
                 "value": [[0, 0, 1], [0, 1, 0], [1, 0, 0]],
                 "unit": "matrix", "mode": "absolute"},
            ],
        }, root / "out")
        with Image.open(result["output"]["path"]) as image:
            pixels = np.asarray(image.convert("RGB"), dtype=np.float32)
        means = pixels.reshape(-1, 3).mean(axis=0)
        self.assertLess(float(means.max() - means.min()), 1.5)


class TemperatureTests(unittest.TestCase):
    """White balance in both the modes a recipe can spell it.

    A delta warms or cools from where the frame is. An absolute kelvin
    names the light itself, read against the D65 base every reference here
    is developed to -- it was silently a no-op before, which made the
    compiler's own "white balance 5500K" spelling (and the fine-tune
    slider on such an op) change nothing at all.
    """

    def grey(self):
        frame = np.zeros((4, 6, 3), np.float32)
        frame[...] = 0.30
        return frame

    def averages(self, frame):
        return frame.reshape(-1, 3).mean(axis=0)

    def test_a_positive_delta_warms(self):
        red, _, blue = self.averages(_apply_global(self.grey(), [
            {"op": "color.temperature", "value": 600, "mode": "delta"}]))
        self.assertGreater(red, 0.30)
        self.assertLess(blue, 0.30)

    def test_an_absolute_below_d65_cools(self):
        # Naming the light 5400K on a D65-based reference asks for a
        # cooler rendering, the way a develop module's slider reads.
        red, _, blue = self.averages(_apply_global(self.grey(), [
            {"op": "color.temperature", "value": 5400,
             "mode": "absolute"}]))
        self.assertLess(red, 0.30)
        self.assertGreater(blue, 0.30)

    def test_an_absolute_above_d65_warms(self):
        red, _, blue = self.averages(_apply_global(self.grey(), [
            {"op": "color.temperature", "value": 8000,
             "mode": "absolute"}]))
        self.assertGreater(red, 0.30)
        self.assertLess(blue, 0.30)

    def test_an_absolute_at_d65_changes_nothing(self):
        frame = self.grey()
        out = _apply_global(frame, [
            {"op": "color.temperature", "value": 6500,
             "mode": "absolute"}])
        self.assertTrue(np.allclose(out, frame))

    def test_extreme_values_stay_bounded_and_positive(self):
        for value, mode in ((50000, "absolute"), (2000, "absolute"),
                            (-1999, "delta")):
            out = _apply_global(self.grey(), [
                {"op": "color.temperature", "value": value, "mode": mode}])
            self.assertTrue(np.isfinite(out).all(), (value, mode))
            self.assertGreaterEqual(float(out.min()), 0.0, (value, mode))


class CurveTests(unittest.TestCase):
    """The tone curve: monotone, exact through its points, honest at rest."""

    def grey(self, level=0.30):
        frame = np.zeros((4, 6, 3), np.float32)
        frame[...] = level
        return frame

    def curve(self, points):
        return [{"op": "tone.curve", "unit": "curve", "mode": "absolute",
                 "value": {"points": points}, "enabled": True}]

    def test_the_lut_is_monotone_and_passes_through_its_points(self):
        lut = _curve_lut([[0, 0], [96, 40], [200, 230], [255, 255]])
        self.assertTrue(np.all(np.diff(lut) >= -1e-6))
        self.assertAlmostEqual(float(lut[96]) * 255, 40, delta=1)
        self.assertAlmostEqual(float(lut[200]) * 255, 230, delta=1)

    def test_an_identity_curve_changes_nothing(self):
        frame = self.grey()
        out = _apply_global(frame, self.curve([[0, 0], [255, 255]]))
        self.assertTrue(np.allclose(out, frame, atol=1e-3))

    def test_a_lifting_curve_lifts_and_never_folds(self):
        out = _apply_global(self.grey(), self.curve(
            [[0, 0], [128, 200], [255, 255]]))
        self.assertGreater(float(out.mean()), 0.30)
        # Steeper input still comes out at least as bright: monotone.
        brighter = _apply_global(self.grey(0.5), self.curve(
            [[0, 0], [128, 200], [255, 255]]))
        self.assertGreaterEqual(float(brighter.mean()),
                                float(out.mean()) - 1e-4)


class ColourMaskTests(unittest.TestCase):
    """Weights from what the pixels are, gated against the greys."""

    def canvas(self):
        frame = np.zeros((1, 4, 3), np.float32)
        frame[0, 0] = (0.8, 0.15, 0.1)   # red-orange
        frame[0, 1] = (0.1, 0.8, 0.15)   # green
        frame[0, 2] = (0.5, 0.5, 0.5)    # grey: no honest hue
        frame[0, 3] = (0.8, 0.45, 0.1)   # orange
        return frame

    def weights(self, anchor):
        from development_engine import mask_weights

        return mask_weights(self.canvas(), "color", {"anchor": anchor})[0]

    def test_the_asked_hue_is_selected_and_the_rest_is_not(self):
        told = self.weights(
            "colour mask, hue 20, range 40, softness 15, "
            "above 10% saturation")
        self.assertGreater(float(told[0]), 0.8)     # red-orange in
        self.assertGreater(float(told[3]), 0.8)     # orange in
        self.assertLess(float(told[1]), 0.05)       # green out

    def test_grey_is_out_whatever_the_hue_says(self):
        told = self.weights(
            "colour mask, hue 20, range 360, softness 0, "
            "above 10% saturation")
        self.assertLess(float(told[2]), 0.05)

    def test_inverted_selects_everything_except_it(self):
        told = self.weights(
            "colour mask, hue 20, range 40, softness 15, "
            "above 10% saturation, inverted")
        self.assertLess(float(told[0]), 0.2)
        self.assertGreater(float(told[1]), 0.95)


class CurveColourHoldTests(unittest.TestCase):
    """The same drawing, two readings: film at 0, faithful at 100."""

    def curve(self, preserve):
        return [{"op": "tone.curve", "unit": "curve", "mode": "absolute",
                 "enabled": True,
                 "value": {"points": [[0, 0], [96, 48], [160, 208],
                                      [255, 255]],
                           "preserve": preserve}}]

    def hue_of(self, rgb):
        r, g, b = (float(v) for v in rgb)
        import colorsys

        return colorsys.rgb_to_hsv(r, g, b)[0] * 360.0

    def test_the_rgb_reading_bends_hue_and_the_luma_reading_does_not(self):
        from development_engine import _apply_global

        # A warm, saturated patch of one colour: the kind of pixel an
        # S-curve is hardest on.
        patch = np.full((4, 4, 3), (0.5, 0.22, 0.1), dtype=np.float32)
        before = self.hue_of(patch[0, 0])
        filmed = _apply_global(patch, self.curve(0))
        held = _apply_global(patch, self.curve(100))
        bent = abs(self.hue_of(filmed[0, 0]) - before)
        kept = abs(self.hue_of(held[0, 0]) - before)
        self.assertGreater(bent, 1.0)     # the film trade is real
        self.assertLess(kept, 0.75)       # and the hold really holds

    def test_the_luma_reading_still_moves_the_tones(self):
        from development_engine import _apply_global

        patch = np.full((4, 4, 3), (0.5, 0.22, 0.1), dtype=np.float32)
        held = _apply_global(patch, self.curve(100))
        self.assertFalse(np.allclose(held, patch, atol=1e-3))

    def test_halfway_sits_between_the_two_readings(self):
        from development_engine import _apply_global

        patch = np.full((4, 4, 3), (0.5, 0.22, 0.1), dtype=np.float32)
        filmed = _apply_global(patch, self.curve(0))[0, 0]
        held = _apply_global(patch, self.curve(100))[0, 0]
        mixed = _apply_global(patch, self.curve(50))[0, 0]
        for channel in range(3):
            low = min(filmed[channel], held[channel]) - 1e-4
            high = max(filmed[channel], held[channel]) + 1e-4
            self.assertGreaterEqual(float(mixed[channel]), low)
            self.assertLessEqual(float(mixed[channel]), high)

    def test_a_curve_without_the_field_is_the_classic_curve(self):
        from development_engine import _apply_global

        patch = np.full((4, 4, 3), (0.5, 0.22, 0.1), dtype=np.float32)
        bare = [{"op": "tone.curve", "unit": "curve", "mode": "absolute",
                 "enabled": True,
                 "value": {"points": [[0, 0], [96, 48], [160, 208],
                                      [255, 255]]}}]
        filmed = _apply_global(patch, self.curve(0))
        plain = _apply_global(patch, bare)
        self.assertTrue(np.allclose(filmed, plain, atol=1e-5))


class CleanColourTests(unittest.TestCase):
    """Colour cleaned by what green knows: speckle goes, edges stay."""

    def field(self):
        rng = np.random.default_rng(7)
        img = np.zeros((96, 128, 3), np.float32)
        img[..., 0] = 0.5 + rng.normal(0, 0.06, (96, 128))
        img[..., 1] = 0.4
        img[..., 2] = 0.3 + rng.normal(0, 0.06, (96, 128))
        img[:, 64:, :] *= 0.3         # one hard edge green knows about
        return np.clip(img, 0.0, None)

    def op(self, strength):
        return [{"op": "detail.clean_colour", "unit": "percent",
                 "mode": "delta", "value": strength, "enabled": True}]

    def test_colour_speckle_is_averaged_away(self):
        from development_engine import _apply_global

        img = self.field()
        out = _apply_global(img, self.op(100.0))
        before = float(np.std(img[10:40, 10:50, 0]))
        after = float(np.std(out[10:40, 10:50, 0]))
        self.assertLess(after, before * 0.35)

    def test_green_the_anchor_is_never_moved(self):
        from development_engine import _apply_global

        img = self.field()
        out = _apply_global(img, self.op(100.0))
        self.assertTrue(np.allclose(out[..., 1], img[..., 1], atol=1e-5))

    def test_the_edge_green_knows_about_survives(self):
        from development_engine import _apply_global

        img = self.field()
        out = _apply_global(img, self.op(100.0))
        step = float(np.mean(out[10:80, 55:62, 0])
                     - np.mean(out[10:80, 67:74, 0]))
        self.assertGreater(step, 0.25)     # bright side minus dark side

    def test_zero_strength_changes_nothing(self):
        from development_engine import _apply_global

        img = self.field()
        out = _apply_global(img, self.op(0.0))
        self.assertTrue(np.allclose(out, img, atol=1e-6))


class ColourWedgeWallTests(unittest.TestCase):
    """The wedge's other walls: saturation ceiling, brightness floor."""

    def weights(self, patch, anchor):
        from development_engine import mask_weights

        return mask_weights(patch, "color", {"anchor": anchor})

    def patch(self, rgb):
        return np.full((2, 2, 3), rgb, dtype=np.float32)

    def test_a_saturation_ceiling_keeps_the_neon_out(self):
        anchor = ("colour mask, hue 0, range 40, softness 10, "
                  "above 5% saturation, below 60% saturation")
        neon = self.weights(self.patch((0.8, 0.05, 0.05)), anchor)
        pastel = self.weights(self.patch((0.8, 0.6, 0.6)), anchor)
        self.assertLess(float(neon[0, 0]), 0.1)
        self.assertGreater(float(pastel[0, 0]), 0.9)

    def test_a_brightness_floor_keeps_the_shadows_out(self):
        anchor = ("colour mask, hue 0, range 40, softness 10, "
                  "above 5% saturation, brighter than 40%")
        dark = self.weights(self.patch((0.05, 0.01, 0.01)), anchor)
        lit = self.weights(self.patch((0.8, 0.4, 0.4)), anchor)
        self.assertLess(float(dark[0, 0]), 0.1)
        self.assertGreater(float(lit[0, 0]), 0.9)

    def test_a_brightness_ceiling_keeps_the_highlights_out(self):
        anchor = ("colour mask, hue 0, range 40, softness 10, "
                  "above 5% saturation, darker than 60%")
        blazing = self.weights(self.patch((0.95, 0.5, 0.5)), anchor)
        mid = self.weights(self.patch((0.25, 0.08, 0.08)), anchor)
        self.assertLess(float(blazing[0, 0]), 0.1)
        self.assertGreater(float(mid[0, 0]), 0.9)

    def test_a_wall_never_asked_for_moves_nothing(self):
        bare = ("colour mask, hue 0, range 40, softness 10, "
                "above 5% saturation")
        patch = self.patch((0.8, 0.05, 0.05))
        self.assertGreater(float(self.weights(patch, bare)[0, 0]), 0.9)


class UniformityTests(unittest.TestCase):
    """The eveners: colours in the wedge walk toward one aim."""

    def masked(self, effects, anchor):
        return [{"op": "mask.color", "unit": "mask", "enabled": True,
                 "value": {"anchor": anchor, "opacity": 1.0,
                           "feather": 1.0, "effects": effects}}]

    def test_hues_converge_and_brightness_stays(self):
        import colorsys

        from development_engine import _apply_global

        patch = np.zeros((1, 2, 3), np.float32)
        patch[0, 0] = (0.8, 0.5, 0.4)
        patch[0, 1] = (0.8, 0.65, 0.4)
        out = _apply_global(patch, self.masked(
            [{"op": "uniformity.hue", "value": 100.0}],
            "colour mask, hue 25, range 60, softness 30, "
            "above 5% saturation"))
        first = colorsys.rgb_to_hsv(*out[0, 0])[0] * 360
        second = colorsys.rgb_to_hsv(*out[0, 1])[0] * 360
        self.assertAlmostEqual(first, 25.0, delta=1.0)
        self.assertAlmostEqual(second, 25.0, delta=1.0)
        self.assertAlmostEqual(float(out[0, 0].max()),
                               float(patch[0, 0].max()), places=3)

    def test_saturation_walks_to_the_stated_aim(self):
        import colorsys

        from development_engine import _apply_global

        patch = np.full((1, 1, 3), (0.8, 0.5, 0.4), np.float32)
        out = _apply_global(patch, self.masked(
            [{"op": "uniformity.saturation", "value": 100.0}],
            "colour mask, hue 25, range 60, softness 30, "
            "above 5% saturation, target saturation 40"))
        self.assertAlmostEqual(
            colorsys.rgb_to_hsv(*out[0, 0])[1], 0.40, places=2)

    def test_half_strength_walks_half_way(self):
        import colorsys

        from development_engine import _apply_global

        patch = np.full((1, 1, 3), (0.8, 0.5, 0.4), np.float32)
        before = colorsys.rgb_to_hsv(*patch[0, 0])[1]
        out = _apply_global(patch, self.masked(
            [{"op": "uniformity.saturation", "value": 50.0}],
            "colour mask, hue 25, range 60, softness 30, "
            "above 5% saturation, target saturation 40"))
        self.assertAlmostEqual(
            colorsys.rgb_to_hsv(*out[0, 0])[1],
            before + (0.40 - before) * 0.5, places=2)

    def test_lightness_walks_to_the_stated_aim(self):
        from development_engine import _ENCODE_GAMMA, _apply_global

        patch = np.full((1, 1, 3), (0.8, 0.5, 0.4), np.float32)
        out = _apply_global(patch, self.masked(
            [{"op": "uniformity.lightness", "value": 100.0}],
            "colour mask, hue 25, range 60, softness 30, "
            "above 5% saturation, target light 70"))
        self.assertAlmostEqual(
            float(out[0, 0].max()) ** (1.0 / _ENCODE_GAMMA), 0.70,
            places=2)

    def test_a_pixel_outside_the_wedge_is_untouched(self):
        from development_engine import _apply_global

        patch = np.full((1, 1, 3), (0.1, 0.2, 0.8), np.float32)   # blue
        out = _apply_global(patch, self.masked(
            [{"op": "uniformity.hue", "value": 100.0}],
            "colour mask, hue 25, range 30, softness 10, "
            "above 5% saturation"))
        self.assertTrue(np.allclose(out, patch, atol=1e-3))


class BrushMaskWeightTests(unittest.TestCase):
    """Ink where the hand painted, nothing where it did not."""

    def encoded(self):
        import base64
        import io

        from PIL import Image, ImageDraw

        sheet = Image.new("L", (64, 48), 0)
        ImageDraw.Draw(sheet).rectangle((0, 0, 31, 47), fill=255)
        buffer = io.BytesIO()
        sheet.save(buffer, "PNG")
        return base64.b64encode(buffer.getvalue()).decode()

    def test_the_map_stretches_to_the_render_and_keeps_its_sides(self):
        from development_engine import mask_weights

        weights = mask_weights(
            np.zeros((480, 640, 3), np.float32), "brush",
            {"anchor": "painted by hand", "map": self.encoded(),
             "feather": 0.0})
        self.assertGreater(float(weights[240, 100]), 0.9)   # painted side
        self.assertLess(float(weights[240, 540]), 0.1)      # clean side

    def test_feather_softens_the_strokes_edge(self):
        from development_engine import mask_weights

        hard = mask_weights(
            np.zeros((48, 64, 3), np.float32), "brush",
            {"anchor": "x", "map": self.encoded(), "feather": 0.0})
        soft = mask_weights(
            np.zeros((48, 64, 3), np.float32), "brush",
            {"anchor": "x", "map": self.encoded(), "feather": 1.0})
        edge_hard = float(np.abs(np.diff(hard[24])).max())
        edge_soft = float(np.abs(np.diff(soft[24])).max())
        self.assertLess(edge_soft, edge_hard)

    def test_an_empty_map_masks_nothing_rather_than_everything(self):
        from development_engine import mask_weights

        weights = mask_weights(
            np.zeros((24, 32, 3), np.float32), "brush", {"anchor": "x"})
        self.assertEqual(float(weights.max()), 0.0)


class MidtoneBandTests(unittest.TestCase):
    """One tone equalizer band: the clouds without the sky or the sun."""

    def band(self, luminance: float) -> float:
        from development_engine import _spatial_mask

        frame = np.full((2, 2, 3), luminance, dtype=np.float32)
        return float(_spatial_mask(
            frame, "luma", {"anchor": "luma mask on the midtones"}).mean())

    def test_it_peaks_in_the_middle_and_falls_away_at_both_ends(self):
        middle = self.band(0.5 ** 2.2)
        self.assertAlmostEqual(middle, 1.0, places=2)
        self.assertLess(self.band(0.001), middle)
        self.assertLess(self.band(0.99), middle)

    def test_it_leaves_a_silhouette_and_a_blown_sun_alone(self):
        """The two things this band exists not to touch."""
        self.assertAlmostEqual(self.band(0.0), 0.0, places=3)
        self.assertAlmostEqual(self.band(1.0), 0.0, places=3)

    def test_shadows_and_highlights_are_unchanged_by_its_arrival(self):
        from development_engine import _spatial_mask

        dark = np.full((2, 2, 3), 0.05, dtype=np.float32)
        self.assertGreater(
            float(_spatial_mask(dark, "luma", {"anchor": "shadows"}).mean()),
            0.8)
        bright = np.full((2, 2, 3), 0.9, dtype=np.float32)
        self.assertGreater(
            float(_spatial_mask(bright, "luma", {"anchor": "highlights"}).mean()),
            0.8)


class GreyMixTests(unittest.TestCase):
    """Monochrome by choosing which channel carries the picture."""

    def test_a_grey_mix_leaves_no_colour_and_weights_the_channels(self):
        frame = np.zeros((2, 2, 3), dtype=np.float32)
        frame[..., 0], frame[..., 1], frame[..., 2] = 0.8, 0.4, 0.1
        row = [0.5, 0.4, 0.1]
        out = _apply_global(frame, [{
            "op": "color.channel_mixer", "value": [row, row, row],
            "unit": "matrix", "mode": "absolute"}])
        expected = 0.8 * 0.5 + 0.4 * 0.4 + 0.1 * 0.1
        self.assertAlmostEqual(float(out[..., 0].mean()), expected, places=5)
        self.assertAlmostEqual(float(out[..., 0].mean()),
                               float(out[..., 2].mean()), places=6)

    def test_a_different_weighting_gives_a_different_grey(self):
        """Which channel leads is the decision infrared work turns on."""
        frame = np.zeros((2, 2, 3), dtype=np.float32)
        frame[..., 0], frame[..., 1], frame[..., 2] = 0.8, 0.4, 0.1
        greys = []
        for row in ([1.0, 0.0, 0.0], [0.0, 0.0, 1.0]):
            out = _apply_global(frame, [{
                "op": "color.channel_mixer", "value": [row, row, row],
                "unit": "matrix", "mode": "absolute"}])
            greys.append(round(float(out[..., 0].mean()), 4))
        self.assertEqual(greys, [0.8, 0.1])


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

    def test_straightening_leaves_no_black_corners(self):
        """A straighten that keeps the whole turned canvas is not one.

        Rotating and keeping everything left 5.7% of a frame black at a
        degree and a half -- wedges where the picture no longer reaches.
        """
        from development_engine import _geometry

        frame = Image.new("RGB", (400, 300), (120, 150, 90))
        for angle in (1.5, -3.0, 8.0):
            turned = _geometry(frame, [
                {"op": "geometry.rotation", "value": angle}])
            pixels = np.asarray(turned, dtype=np.float32)
            empty = float((pixels.max(axis=2) <= 2).mean())
            self.assertEqual(empty, 0.0, f"{angle} degrees left empty corners")
            self.assertLess(turned.size[0], frame.size[0])

    def test_a_crop_is_taken_after_the_frame_is_straightened(self):
        # A crop chosen on a crooked picture is not the crop that was
        # asked for, whatever order the recipe lists them in.
        from development_engine import _geometry

        frame = Image.new("RGB", (400, 300), (120, 150, 90))
        crop_first = _geometry(frame, [
            {"op": "geometry.crop_aspect", "value": [5, 4]},
            {"op": "geometry.rotation", "value": 2.0}])
        rotate_first = _geometry(frame, [
            {"op": "geometry.rotation", "value": 2.0},
            {"op": "geometry.crop_aspect", "value": [5, 4]}])
        self.assertEqual(crop_first.size, rotate_first.size)
        self.assertAlmostEqual(
            crop_first.size[0] / crop_first.size[1], 1.25, places=2)

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
