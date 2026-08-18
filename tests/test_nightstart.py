"""A first version of a night frame, measured rather than guessed."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from development_engine import _apply_global, _decoded, _encoded
from opencull_gui import nightstart


def night_frame(sky=(0.02, 0.02, 0.02), noise=0.005, stars=60,
                size=(240, 320), seed=3):
    """A synthetic night frame with a stated sky, cast and noise."""
    height, width = size
    rng = np.random.default_rng(seed)
    field = np.zeros((height, width, 3), np.float32)
    for index in range(3):
        field[..., index] = sky[index]
    grid_y, grid_x = np.mgrid[0:height, 0:width]
    for _ in range(stars):
        x = rng.uniform(10, width - 10)
        y = rng.uniform(10, height - 10)
        bright = rng.uniform(0.05, 0.6)
        field += (bright * np.exp(
            -(((grid_x - x) ** 2 + (grid_y - y) ** 2)
              / (2 * 1.3 ** 2))))[..., None]
    return np.clip(field + rng.normal(0, noise, field.shape),
                   0, 1).astype(np.float32)


def developed(shown, operations):
    return np.clip(_encoded(_apply_global(
        _decoded(shown).astype(np.float32), operations)), 0, 1)


class MeasureTests(unittest.TestCase):
    def test_the_sky_and_its_noise_are_found(self):
        told = nightstart.measure(night_frame(
            sky=(0.03, 0.03, 0.03), noise=0.006))
        self.assertAlmostEqual(told["sky"], 0.03, delta=0.01)
        self.assertAlmostEqual(told["noise"], 0.006, delta=0.004)

    def test_a_field_of_stars_does_not_read_as_noise(self):
        quiet = nightstart.measure(night_frame(noise=0.001, stars=200))
        self.assertLess(quiet["noise"], 0.01)

    def test_the_sky_s_own_colour_is_measured(self):
        told = nightstart.measure(night_frame(sky=(0.01, 0.04, 0.18)))
        self.assertAlmostEqual(told["channels"][2], 0.18, delta=0.02)
        self.assertGreater(told["cast"], 0.15)


class StretchTests(unittest.TestCase):
    def test_the_stretch_lifts_the_faint_and_compresses_the_bright(self):
        held = np.full((8, 8, 3), 0.004, np.float32)
        held[4, 4] = 0.9
        out = _apply_global(held, [{
            "op": "tone.stretch", "unit": "percent", "mode": "delta",
            "value": 50.0, "enabled": True}])
        faint_before = float(_encoded(held)[0, 0, 0])
        faint_after = float(_encoded(out)[0, 0, 0])
        bright_after = float(_encoded(out)[4, 4, 0])
        self.assertGreater(faint_after, faint_before * 3)
        self.assertLess(bright_after, 1.0)      # compressed, not clipped

    def test_zero_stretch_is_identity(self):
        held = night_frame()
        self.assertTrue(np.allclose(_apply_global(held, [{
            "op": "tone.stretch", "unit": "percent", "mode": "delta",
            "value": 0.0, "enabled": True}]), held))

    def test_the_sky_offset_is_subtracted_not_gained(self):
        # Stated in the domain the operation works in: display.
        shown = np.full((6, 6, 3), 0.5, np.float32)
        shown[3, 3] = (0.9, 0.5, 0.5)
        out = _encoded(_apply_global(_decoded(shown), [{
            "op": "color.sky_offset", "unit": "levels",
            "mode": "absolute", "enabled": True,
            "value": {"red": 0.0, "green": 0.0, "blue": 0.2}}]))
        # Every pixel loses the same amount of blue, and nothing else
        # changes -- a gain would have rescaled the other channels.
        self.assertAlmostEqual(float(out[0, 0, 2]), 0.3, places=3)
        self.assertAlmostEqual(float(out[3, 3, 0]), 0.9, places=3)
        self.assertAlmostEqual(float(out[3, 3, 1]), 0.5, places=3)


class NightCleanTests(unittest.TestCase):
    def empty_patch(self, held):
        """The emptiest 40x40 of sky, because that is where sky is.

        Measuring "sky noise" over a patch with a star in it measures
        the star: it dominates the deviation, and an operation that
        correctly preserves it looks like one that did nothing.
        """
        lum = held.mean(axis=2)
        best, faintest = (0, 0), 1e9
        for y in range(0, held.shape[0] - 40, 20):
            for x in range(0, held.shape[1] - 40, 20):
                brightest = float(lum[y:y + 40, x:x + 40].max())
                if brightest < faintest:
                    best, faintest = (y, x), brightest
        return best

    def test_the_sky_is_quietened_and_the_stars_are_not(self):
        held = night_frame(noise=0.012, stars=8, seed=11)
        out = _apply_global(held, [{
            "op": "detail.night_clean", "unit": "percent",
            "mode": "delta", "value": 85.0, "enabled": True}])
        y, x = self.empty_patch(held)
        before = float(np.std(held[y:y + 40, x:x + 40]))
        after = float(np.std(out[y:y + 40, x:x + 40]))
        self.assertLess(after, before * 0.6)
        star = np.unravel_index(
            np.argmax(held[..., 1]), held[..., 1].shape)
        self.assertGreater(float(out[star][1]),
                           float(held[star][1]) * 0.95)

    def test_zero_is_a_no_op(self):
        held = night_frame()
        self.assertTrue(np.allclose(_apply_global(held, [{
            "op": "detail.night_clean", "unit": "percent",
            "mode": "delta", "value": 0.0, "enabled": True}]), held))


class NightStartTests(unittest.TestCase):
    def written(self, folder: Path, shown, name="N.png"):
        # The pixels are handed in directly; the file only has to
        # exist for the settings to be looked for.
        path = folder / name
        Image.fromarray(
            (np.clip(shown, 0, 1) * 255 + 0.5).astype(np.uint8)).save(path)
        return path

    def test_a_dark_sky_is_lifted_to_where_a_sky_sits(self):
        with tempfile.TemporaryDirectory() as folder:
            shown = night_frame(sky=(0.02, 0.02, 0.02), noise=0.004)
            path = self.written(Path(folder), shown)
            told = nightstart.night_start(path, shown=shown, frames=1)
            out = developed(shown, told["operations"])
            lifted = float(np.median(
                out[..., 0] * 0.2126 + out[..., 1] * 0.7152
                + out[..., 2] * 0.0722))
            self.assertAlmostEqual(
                lifted, nightstart.SKY_TARGET, delta=0.06)
            self.assertTrue(told["reached_target"])

    def test_light_pollution_is_taken_off_before_it_is_multiplied(self):
        with tempfile.TemporaryDirectory() as folder:
            shown = night_frame(sky=(0.01, 0.04, 0.16), noise=0.004)
            path = self.written(Path(folder), shown)
            told = nightstart.night_start(path, shown=shown, frames=1)
            names = [item["op"] for item in told["operations"]]
            self.assertEqual(names[0], "color.sky_offset")
            out = developed(shown, told["operations"])
            channels = [float(np.median(out[..., index]))
                        for index in range(3)]
            # The wash is gone: the sky is grey within a shade.
            self.assertLess(max(channels) - min(channels), 0.09)

    def test_a_frame_too_noisy_to_lift_says_so_instead(self):
        with tempfile.TemporaryDirectory() as folder:
            # Almost no signal and a great deal of noise.
            shown = night_frame(sky=(0.004, 0.004, 0.004), noise=0.02,
                                stars=10)
            path = self.written(Path(folder), shown)
            told = nightstart.night_start(path, shown=shown, frames=1)
            self.assertFalse(told["reached_target"])
            self.assertIn("noise allows", told["note"])
            self.assertIn("more frames", told["note"].casefold())

    def test_a_stack_is_not_smoothed_like_a_single_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            shown = night_frame(sky=(0.02, 0.02, 0.02), noise=0.006)
            path = self.written(Path(folder), shown)
            alone = nightstart.night_start(path, shown=shown, frames=1)
            stacked = nightstart.night_start(path, shown=shown,
                                             frames=25)

            def quieting(told):
                return next((item["value"] for item in told["operations"]
                             if item["op"] == "detail.night_clean"), 0.0)
            self.assertGreater(quieting(alone), quieting(stacked))

    def test_a_black_frame_is_refused_rather_than_amplified(self):
        with tempfile.TemporaryDirectory() as folder:
            shown = np.zeros((60, 80, 3), np.float32)
            path = self.written(Path(folder), shown)
            told = nightstart.night_start(path, shown=shown)
            self.assertEqual(told["operations"], [])
            self.assertIn("nothing to lift", told["note"])

    def test_the_note_says_what_was_decided_and_why(self):
        with tempfile.TemporaryDirectory() as folder:
            shown = night_frame(sky=(0.02, 0.02, 0.02), noise=0.005)
            path = self.written(Path(folder), shown)
            told = nightstart.night_start(path, shown=shown, frames=4)
            self.assertIn("black point", told["note"])
            self.assertIn("4 frames were averaged", told["note"])

    def test_the_trailing_limit_follows_the_old_rule(self):
        self.assertAlmostEqual(
            nightstart.trailing_limit(16.0, 1.5), 500 / 24, places=3)
        self.assertLess(nightstart.trailing_limit(200.0),
                        nightstart.trailing_limit(16.0))


class CameraFactsTests(unittest.TestCase):
    def test_the_settings_are_read_from_an_ordinary_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "F.jpg"
            exif = Image.Exif()
            exif[34855] = 1600
            exif[33434] = 3.0
            exif[37386] = 16.0
            exif[33437] = 1.8
            Image.new("RGB", (40, 30), (10, 10, 20)).save(
                path, exif=exif)
            told = nightstart.camera_facts(path)
            self.assertEqual(told["iso"], 1600)
            self.assertEqual(told["seconds"], 3.0)
            self.assertEqual(told["focal"], 16.0)

    def test_a_file_with_nothing_to_say_says_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plain.jpg"
            Image.new("RGB", (20, 20), (5, 5, 5)).save(path)
            self.assertEqual(nightstart.camera_facts(path), {})
            self.assertEqual(
                nightstart.camera_facts(Path(folder) / "gone.jpg"), {})


class MeasuredOperationsTests(unittest.TestCase):
    """Whole operations a measurement produced, carried as a change."""

    def test_they_land_in_the_recipe_and_replace_on_asking_again(self):
        from opencull_gui import adjustments

        recipe = {"operations": [
            {"op": "tone.exposure", "unit": "EV", "mode": "delta",
             "value": 0.2, "id": "op-1"}]}
        out = adjustments.apply(recipe, {"+ops": [
            {"op": "tone.stretch", "unit": "percent", "mode": "delta",
             "value": 40.0}]})
        self.assertEqual([item["op"] for item in out["operations"]],
                         ["tone.exposure", "tone.stretch"])
        again = adjustments.apply(out, {"+ops": [
            {"op": "tone.stretch", "unit": "percent", "mode": "delta",
             "value": 22.0}]})
        held = [item for item in again["operations"]
                if item["op"] == "tone.stretch"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["value"], 22.0)

    def test_they_sit_above_the_masks_that_read_them(self):
        from opencull_gui import adjustments

        recipe = {"operations": [
            {"op": "mask.radial", "unit": "mask", "value": {
                "anchor": "radial gradient", "effects": []}}]}
        out = adjustments.apply(recipe, {"+ops": [
            {"op": "tone.stretch", "unit": "percent", "mode": "delta",
             "value": 40.0}]})
        self.assertEqual([item["op"] for item in out["operations"]],
                         ["tone.stretch", "mask.radial"])


if __name__ == "__main__":
    unittest.main()
