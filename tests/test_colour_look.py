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


class MimicTests(unittest.TestCase):
    """The camera's own JPEG teaches; the fit must learn what it knows."""

    SECRET = [
        {"op": "color.channel_mixer", "unit": "matrix",
         "mode": "absolute", "enabled": True,
         "value": [[1.08, -0.06, -0.02], [-0.04, 1.1, -0.06],
                   [-0.02, -0.08, 1.1]]},
        {"op": "color.saturation", "unit": "percent", "mode": "delta",
         "value": 22.0, "enabled": True},
        {"op": "tone.contrast", "unit": "percent", "mode": "delta",
         "value": 18.0, "enabled": True},
    ]

    def scene(self, seed):
        from development_engine import _box_mean

        rng = np.random.default_rng(seed)
        base = rng.random((96, 128, 3)).astype(np.float32) * 0.75 + 0.05
        return np.stack(
            [_box_mean(base[..., c], 6) for c in range(3)], -1)

    def pair(self, seed):
        linear = self.scene(seed)
        source = np.clip(linear, 0, 1) ** (1 / _ENCODE_GAMMA)
        taught = _apply_global(linear, self.SECRET)
        target = np.clip(taught, 0, 1) ** (1 / _ENCODE_GAMMA)
        return source, target

    def test_a_hidden_simulation_is_learned_back(self):
        ops, report = cl.fit_mimic(
            [self.pair(seed) for seed in (1, 2, 3)])
        held = self.scene(99)
        truth = np.clip(_apply_global(held, self.SECRET), 0, 1) \
            ** (1 / _ENCODE_GAMMA)
        learned = np.clip(_apply_global(held, ops), 1e-6, None) \
            ** (1 / _ENCODE_GAMMA)
        before = float(np.abs(
            np.clip(held, 0, 1) ** (1 / _ENCODE_GAMMA) - truth).mean())
        after = float(np.abs(learned - truth).mean())
        self.assertLess(after, before / 10.0)
        self.assertGreater(report["samples"], 1000)

    def test_clipped_pixels_do_not_teach(self):
        source = np.full((10, 10, 3), 0.995, np.float32)
        target = np.full((10, 10, 3), 0.5, np.float32)
        kept_s, _kept_t = cl.mimic_samples(source, target)
        self.assertEqual(len(kept_s), 0)

    def test_too_little_honesty_is_refused(self):
        source = np.full((10, 10, 3), 0.995, np.float32)
        with self.assertRaises(cl.LookError):
            cl.fit_mimic([(source, source)])

    def test_raws_find_their_sibling_jpegs(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "DSCF0001.RAF"
            raw.write_bytes(b"x")
            (Path(folder) / "DSCF0001.JPG").write_bytes(b"x")
            pairs = cl._paired([str(raw)])
            self.assertEqual(pairs[0][1].name, "DSCF0001.JPG")
            lonely = Path(folder) / "DSCF0002.RAF"
            lonely.write_bytes(b"x")
            with self.assertRaises(cl.LookError):
                cl._paired([str(lonely)])

    def test_explicit_pairs_say_so_outright(self):
        pairs = cl._paired(["a.jpg=b.jpg"])
        self.assertEqual((pairs[0][0].name, pairs[0][1].name),
                         ("a.jpg", "b.jpg"))


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


class LearnLookProgramTests(unittest.TestCase):
    """The kim program's kernels: pair, fit, judge, keep."""

    def shoot(self, folder: Path):
        from unittest import mock

        from PIL import Image
        rng = np.random.default_rng(7)
        from development_engine import _box_mean
        for index in (1, 2, 3):
            base = rng.random((120, 160, 3)).astype(np.float32) * 0.7 + 0.1
            lin = np.stack([_box_mean(base[..., c], 8)
                            for c in range(3)], -1)
            src = np.clip(lin, 0, 1) ** (1 / _ENCODE_GAMMA)
            tgt = np.clip(lin * [1.1, 1.0, 0.9], 0, 1) \
                ** (1 / _ENCODE_GAMMA)
            Image.fromarray((src * 255 + 0.5).astype(np.uint8)).save(
                folder / f"DSCF000{index}.tif")
            Image.fromarray((tgt * 255 + 0.5).astype(np.uint8)).save(
                folder / f"DSCF000{index}.JPG")
        return mock.patch.object(
            cl, "RAW_SUFFIXES", cl.RAW_SUFFIXES | {".tif"})

    def test_the_program_learns_and_writes_a_real_preset(self):
        import look_kernel

        with tempfile.TemporaryDirectory() as folder:
            with self.shoot(Path(folder)):
                told = look_kernel.learn_look(
                    folder, "*.tif", "Warm test look", 12)
            self.assertTrue(look_kernel.look_valid(told))
            self.assertIn("Warm test look", look_kernel.look_note(told))
            import json as json_module
            value = json_module.loads(told)
            self.assertEqual(value["format"], "darkimiya-preset-v1")
            ops = [item["op"] for item in value["operations"]]
            self.assertIn("color.warp", ops)
            # written where look_home says, the store reads it back
            import os
            held = os.environ.get("DARKIMIYA_PRESETS")
            os.environ["DARKIMIYA_PRESETS"] = str(
                Path(folder) / "presets")
            try:
                where = Path(look_kernel.look_home(told))
                where.write_text(told, encoding="utf-8")
                from opencull_gui import presets
                listed = presets.saved()
                self.assertEqual(listed[0]["name"], "Warm test look")
            finally:
                if held is None:
                    os.environ.pop("DARKIMIYA_PRESETS", None)
                else:
                    os.environ["DARKIMIYA_PRESETS"] = held

    def test_an_empty_folder_fails_the_check_politely(self):
        import look_kernel

        with tempfile.TemporaryDirectory() as folder:
            told = look_kernel.learn_look(folder, "*.RAF", "Nothing", 12)
            self.assertFalse(look_kernel.look_valid(told))
            self.assertIn("no RAW+JPEG pairs", look_kernel.look_note(told))

    def test_the_program_is_in_the_catalogue(self):
        from opencull_gui.programs import BUILT_INS

        self.assertIn("learn_look.kim",
                      [name for name, _purpose in BUILT_INS])
