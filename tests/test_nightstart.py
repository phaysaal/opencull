"""A first version of a night frame, measured rather than guessed."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from development_engine import (_apply_global, _correlated, _decoded,
                                _encoded, _LUMA, _resized_shape,
                                _star_keeps, _star_shape)
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


class BackgroundTests(unittest.TestCase):
    """The sky's slope, measured where no star stands."""

    def sloped(self, tilt=0.03, sky=0.02, stars=60, size=(480, 640)):
        # Big enough for a real grid: tiles are floored at forty
        # pixels, so a small frame gets a surface too coarse to
        # follow anything.
        held = night_frame(sky=(sky, sky, sky), noise=0.003,
                           stars=stars, size=size)
        height, width = size
        grid_y, grid_x = np.mgrid[0:height, 0:width]
        # Brighter in the middle, as a lens vignettes a flat sky.
        away = np.sqrt(((grid_x - width / 2) / (width / 2)) ** 2
                       + ((grid_y - height / 2) / (height / 2)) ** 2)
        return np.clip(
            held + (tilt * (1.0 - np.clip(away, 0, 1)))[..., None],
            0, 1).astype(np.float32)

    def slope_of(self, held, tiles=6) -> float:
        height, width = held.shape[:2]
        lum = held[..., 0] * 0.2126 + held[..., 1] * 0.7152 \
            + held[..., 2] * 0.0722
        corners = [float(np.percentile(
            lum[r * height // tiles:(r + 1) * height // tiles,
                c * width // tiles:(c + 1) * width // tiles], 20))
            for r in range(tiles) for c in range(tiles)]
        return max(corners) - min(corners)

    def test_a_slope_is_measured_and_taken_out(self):
        held = self.sloped(tilt=0.03)
        before = self.slope_of(held)
        fitted = nightstart.fit_background(held)
        self.assertIsNotNone(fitted)
        out = np.clip(_encoded(_apply_global(
            _decoded(held).astype(np.float32), [fitted])), 0, 1)
        self.assertLess(self.slope_of(out), before * 0.4)

    def test_the_sky_keeps_its_level_and_loses_only_the_tilt(self):
        held = self.sloped(tilt=0.03, sky=0.02)
        fitted = nightstart.fit_background(held)
        out = np.clip(_encoded(_apply_global(
            _decoded(held).astype(np.float32), [fitted])), 0, 1)
        # Subtracting the surface outright would leave nothing to
        # stretch; only its unevenness comes off.
        self.assertGreater(float(np.median(out)), 0.01)

    def test_a_surface_cannot_follow_a_star(self):
        held = self.sloped(tilt=0.0, stars=40)
        fitted = nightstart.fit_background(held)
        if fitted is None:
            return                       # nothing to remove: also fine
        out = np.clip(_encoded(_apply_global(
            _decoded(held).astype(np.float32), [fitted])), 0, 1)
        star = np.unravel_index(
            np.argmax(held[..., 1]), held[..., 1].shape)
        # night_frame already speaks display, so comparing against
        # _encoded(held) would encode it twice and measure a star
        # that was never there.
        self.assertGreater(float(out[star][1]),
                           float(held[star][1]) * 0.98)

    def test_strength_scales_how_much_comes_off(self):
        held = self.sloped(tilt=0.03)
        full = nightstart.fit_background(held, strength=100.0)
        half = nightstart.fit_background(held, strength=50.0)
        def left(op):
            return self.slope_of(np.clip(_encoded(_apply_global(
                _decoded(held).astype(np.float32), [op])), 0, 1))
        self.assertGreater(left(half), left(full))

    def test_a_frame_too_small_to_tile_is_refused(self):
        self.assertIsNone(nightstart.fit_background(
            night_frame(size=(20, 24))))

    def test_an_even_sky_is_left_alone_by_the_night_start(self):
        with tempfile.TemporaryDirectory() as folder:
            held = night_frame(sky=(0.02, 0.02, 0.02), noise=0.003)
            path = Path(folder) / "even.png"
            Image.fromarray(
                (held * 255 + 0.5).astype(np.uint8)).save(path)
            told = nightstart.night_start(path, shown=held)
            # An operation that does nothing is worse than none: it
            # invites the photographer to wonder what it did.
            self.assertFalse(told["flattened"])

    def test_the_windowed_surface_is_the_whole_one_cropped(self):
        from development_engine import _apply_global as apply_ops

        held = self.sloped(tilt=0.04)
        fitted = nightstart.fit_background(held)
        linear = _decoded(held).astype(np.float32)
        whole = apply_ops(linear, [fitted])
        y0, x0, tall, wide = 60, 80, 100, 120
        crop = linear[y0:y0 + tall, x0:x0 + wide].copy()
        part = apply_ops(crop, [fitted], window={
            "x": x0 / held.shape[1], "y": y0 / held.shape[0],
            "w": wide / held.shape[1], "h": tall / held.shape[0]})
        self.assertLess(float(np.abs(
            part - whole[y0:y0 + tall, x0:x0 + wide]).max()), 1e-6)


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
            # Before anything multiplies it -- whatever else the
            # measurement decided to put in front.
            self.assertIn("color.sky_offset", names)
            self.assertLess(names.index("color.sky_offset"),
                            names.index("tone.stretch"))
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

    def quieting(self, told) -> float:
        return next((item["value"] for item in told["operations"]
                     if item["op"] == "detail.night_clean"), 0.0)

    def test_a_cleaner_picture_is_smoothed_less(self):
        """What earns less smoothing is BEING cleaner, not claiming to be.

        The frame count is not arithmetic here. A stack's own measured
        noise already carries what the stacking bought, and taking the
        credit twice once left a real ten-frame stack prescribed no
        quieting at all -- so what the numbers answer to is the
        picture, and a picture that is genuinely quieter says so by
        being quieter.
        """
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            noisy = night_frame(sky=(0.02, 0.02, 0.02), noise=0.006,
                                seed=4)
            clean = night_frame(sky=(0.02, 0.02, 0.02), noise=0.002,
                                seed=4)
            alone = nightstart.night_start(
                self.written(root, noisy, "one.png"), shown=noisy,
                frames=1)
            stacked = nightstart.night_start(
                self.written(root, clean, "many.png"), shown=clean,
                frames=9)
            self.assertGreater(self.quieting(alone),
                               self.quieting(stacked))

    def test_the_black_point_does_not_cancel_the_noise_out(self):
        """A cleaner frame must end up with less grain, not the same.

        Setting the black point a few deviations under the sky leaves
        the sky exactly that many deviations above it, so the lift is
        the target divided by those deviations and the noise cancels
        out of the answer -- every bit of what stacking bought spent
        on a longer lift. Measured on a real ten-frame stack before
        this was fixed: 2.9 times cleaner going in, identical grain
        coming out.
        """
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            noisy = night_frame(sky=(0.02, 0.02, 0.02), noise=0.006,
                                seed=4)
            clean = night_frame(sky=(0.02, 0.02, 0.02), noise=0.002,
                                seed=4)
            rough = nightstart.night_start(
                self.written(root, noisy, "one.png"), shown=noisy)
            smooth = nightstart.night_start(
                self.written(root, clean, "many.png"), shown=clean)
            self.assertLess(smooth["noise_after"],
                            rough["noise_after"] * 0.6)

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


class StarShapeTests(unittest.TestCase):
    """The shape a star lands as, measured and then decided with."""

    def starry(self, noise=0.010, stars=90, size=(400, 520), seed=5):
        return night_frame(sky=(0.03, 0.03, 0.03), noise=noise,
                           stars=stars, size=size, seed=seed)

    def test_the_shape_is_a_centred_normalised_smudge(self):
        found = nightstart.measure_psf(self.starry())
        self.assertIsNotNone(found)
        self.assertEqual(found["op"], "detail.star_shape")
        shape = _star_shape(found["value"])
        self.assertIsNotNone(shape)
        size = found["value"]["size"]
        self.assertEqual(shape.shape, (size, size))
        self.assertAlmostEqual(float(shape.sum()), 1.0, places=4)
        self.assertGreaterEqual(float(shape.min()), 0.0)
        # The brightest point of a star's shape is the star.
        peak = np.unravel_index(int(shape.argmax()), shape.shape)
        self.assertEqual(peak, (size // 2, size // 2))

    def test_it_says_what_size_it_was_measured_at(self):
        held = self.starry()
        found = nightstart.measure_psf(held)
        self.assertEqual(found["value"]["edge"],
                         max(held.shape[0], held.shape[1]))

    def test_a_frame_without_stars_measures_nothing(self):
        blank = night_frame(sky=(0.03, 0.03, 0.03), noise=0.004, stars=0)
        self.assertIsNone(nightstart.measure_psf(blank))

    def test_the_shape_scales_to_the_size_being_drawn(self):
        shape = _star_shape(nightstart.measure_psf(self.starry())["value"])
        half = _resized_shape(shape, 0.5)
        self.assertLess(half.shape[0], shape.shape[0])
        self.assertEqual(half.shape[0] % 2, 1)
        self.assertAlmostEqual(float(half.sum()), 1.0, places=4)
        # Same scale, same shape, untouched.
        self.assertIs(_resized_shape(shape, 1.0), shape)

    def test_correlating_is_quieter_than_the_picture_it_came_from(self):
        """The whole point: a star adds up where the grain cancels."""
        rng = np.random.default_rng(4)
        grain = rng.normal(0.0, 0.01, (300, 300)).astype(np.float32)
        shape = _star_shape(
            nightstart.measure_psf(self.starry())["value"])
        filtered = _correlated(grain, shape)
        self.assertLess(float(np.std(filtered)),
                        float(np.std(grain)) * 0.5)

    def test_the_gate_finds_the_stars_and_not_the_sky(self):
        held = self.starry(noise=0.012, stars=40, seed=9)
        found = nightstart.measure_psf(held)
        lum = (held * _LUMA).sum(axis=2)
        sure = _star_keeps(lum, found["value"],
                           float(max(held.shape[0], held.shape[1])))
        self.assertIsNotNone(sure)
        # Most of a night sky is sky, and the gate should say so.
        self.assertLess(float((sure > 0.5).mean()), 0.05)
        # And the brightest star in the frame is not in doubt.
        star = np.unravel_index(int(lum.argmax()), lum.shape)
        self.assertGreater(float(sure[star]), 0.9)

    def test_the_shape_makes_the_quieting_keep_more_and_smooth_more(self):
        """Both at once, which is what a better detector buys."""
        held = self.starry(noise=0.014, stars=50, seed=13)
        found = nightstart.measure_psf(held)
        clean = {"op": "detail.night_clean", "unit": "percent",
                 "mode": "delta", "value": 90.0, "enabled": True}
        plain = _apply_global(held, [clean])
        shaped = _apply_global(held, [found, clean])
        lum = (held * _LUMA).sum(axis=2)
        sure = _star_keeps(lum, found["value"],
                           float(max(held.shape[0], held.shape[1])))
        sky = sure < 0.01
        self.assertGreater(int(sky.sum()), 1000)
        # The sky is quieter, because less of it was mistaken for stars.
        self.assertLess(float(np.std(shaped[sky])),
                        float(np.std(plain[sky])))

    def test_a_shape_alone_changes_no_pixel(self):
        held = self.starry()
        found = nightstart.measure_psf(held)
        self.assertTrue(np.array_equal(_apply_global(held, [found]), held))

    def test_a_nonsense_shape_is_ignored(self):
        held = self.starry()
        for value in ({"size": 4, "shape": "AAAA"},
                      {"size": 13, "shape": ""},
                      {"size": 13, "shape": "not base64 at all!!"},
                      {"size": 99, "shape": "AAAA"}):
            self.assertIsNone(_star_shape(value))
            self.assertTrue(np.array_equal(_apply_global(held, [{
                "op": "detail.star_shape", "unit": "shape",
                "mode": "absolute", "value": value, "enabled": True}]),
                held))

    def test_without_a_shape_the_old_gate_still_runs(self):
        """A frame that is not a starfield still gets its sky quieted."""
        held = self.starry(noise=0.014, stars=50, seed=21)
        clean = {"op": "detail.night_clean", "unit": "percent",
                 "mode": "delta", "value": 90.0, "enabled": True}
        out = _apply_global(held, [clean])
        self.assertLess(float(np.std(out)), float(np.std(held)))

    def test_a_shape_does_not_leak_out_of_a_mask(self):
        """A mask's own little chain is not the frame's chain.

        A shape published inside a mask must not quietly change what
        the quieting outside that mask believes about stars.
        """
        held = self.starry(noise=0.014, stars=50, seed=17)
        found = nightstart.measure_psf(held)
        clean = {"op": "detail.night_clean", "unit": "percent",
                 "mode": "delta", "value": 90.0, "enabled": True}
        inside = {"op": "mask.radial", "unit": "mask", "mode": "absolute",
                  "value": {"x": 0.5, "y": 0.5, "radius": 0.4,
                            "feather": 0.2, "opacity": 1.0,
                            "effects": [found]},
                  "enabled": True}
        # The mask changes nothing by itself, so the quieting after it
        # must land exactly where it lands with no mask at all.
        # Not bit-for-bit: the mask blends with its own opacity, and
        # a smoothing pass carries that last bit forward. But three
        # parts in a million against the fifty thousand the shape
        # itself is worth, measured just below.
        self.assertTrue(np.allclose(
            _apply_global(held, [inside, clean]),
            _apply_global(held, [clean]), atol=1e-4))
        # And plainly not where the shape WOULD have put it.
        self.assertGreater(float(np.abs(
            _apply_global(held, [inside, clean])
            - _apply_global(held, [found, clean])).max()), 0.01)

    def test_night_start_publishes_the_shape_before_the_quieting(self):
        held = self.starry(noise=0.02, stars=60)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sky.png"
            Image.fromarray(
                (np.clip(held, 0, 1) * 255).astype(np.uint8)).save(path)
            result = nightstart.night_start(path, shown=held)
        names = [item["op"] for item in result["operations"]]
        if "detail.night_clean" not in names:
            self.skipTest("this frame needed no quieting")
        self.assertIn("detail.star_shape", names)
        self.assertLess(names.index("detail.star_shape"),
                        names.index("detail.night_clean"))
        self.assertTrue(result["star_shape"])
        self.assertIn("shape", result["note"])



if __name__ == "__main__":
    unittest.main()
