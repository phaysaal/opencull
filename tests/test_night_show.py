"""One click from night frames to the picture, gated at every step."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kimiya_probe import kimiya_runs  # noqa: E402
from test_superimpose import turning_night  # noqa: E402

import night_show_kernel as nk  # noqa: E402


def report(black=0.0, white=0.0, sky=0.07, honesty=0.6, error=None):
    told = {
        "range": {"black_clipped_percent": black,
                  "white_clipped_percent": white},
        "sky": {"level": sky},
        "honesty": {"honesty": honesty},
    }
    if error:
        told = {"error": error}
    return json.dumps(told)


class ConfidenceGateTests(unittest.TestCase):
    """A stack is only as honest as its least honest frame."""

    def sequence(self, *frames):
        placed = [{"name": "ref", "agreed": 140, "stars": 140}]
        placed += [dict(item) for item in frames]
        return json.dumps({"frames": placed})

    def test_the_coincidence_that_ghosted_a_real_stack_is_refused(self):
        # The album that taught the lesson: eleven agreeing stars of a
        # hundred and forty, accepted, and every star doubled.
        told = self.sequence({"name": "a", "agreed": 11, "stars": 140})
        self.assertFalse(nk.frames_confident(told))

    def test_the_roll_rescue_result_is_believed(self):
        # And what the rescue turned it into: fifty-two of a hundred
        # and forty, at the roll the tripod actually suffered.
        told = self.sequence({"name": "a", "agreed": 52, "stars": 140})
        self.assertTrue(nk.frames_confident(told))

    def test_a_pole_placed_frame_is_vouched_for(self):
        told = self.sequence(
            {"name": "a", "agreed": 5, "stars": 140, "placed_by": "pole"})
        self.assertTrue(nk.frames_confident(told))

    def test_a_frame_with_few_stars_keeps_its_honest_answer(self):
        # Thirty stars, twenty agreeing: weak in absolute numbers and
        # entirely convincing for the frame it is.
        told = self.sequence({"name": "a", "agreed": 20, "stars": 30})
        self.assertTrue(nk.frames_confident(told))

    def test_frames_set_aside_do_not_vote(self):
        told = json.dumps({"frames": [
            {"name": "ref", "agreed": 140, "stars": 140},
            {"name": "a", "agreed": 52, "stars": 140},
            {"name": "cloud", "agreed": 3, "stars": 12, "keep": False},
        ]})
        self.assertTrue(nk.frames_confident(told))


class RuinGateTests(unittest.TestCase):
    """The three ways a week of comparisons actually ruined pictures."""

    def test_a_sound_picture_passes(self):
        self.assertTrue(nk.shown_well(report()))

    def test_clipped_blacks_fail(self):
        self.assertFalse(nk.shown_well(report(black=5.0)))

    def test_blown_whites_fail(self):
        self.assertFalse(nk.shown_well(report(white=1.0)))

    def test_a_deleted_sky_fails_whatever_the_other_numbers_say(self):
        # The crushed renders scored SPECTACULARLY on some counts;
        # their skies sat at nothing. The sky floor cannot be gamed.
        self.assertFalse(nk.shown_well(report(sky=0.0006)))

    def test_the_honesty_backstop_holds(self):
        # The worst ruin ever measured verified a quarter of its
        # claims; every legitimate rendering measured over forty
        # percent. The backstop sits between.
        self.assertFalse(nk.shown_well(report(honesty=0.2)))

    def test_an_error_is_not_a_picture(self):
        self.assertFalse(nk.shown_well(report(error="no frames")))


class StackTruthTests(unittest.TestCase):
    """The photographer's test: a star must appear once."""

    def test_a_clean_synthetic_sequence_passes_every_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            turning_night(root, [0.0, 1.003, 2.005])
            placed = nk.register(str(root), "*.jpg", "", 24.0, 23.5, False)
            screened = nk.screen_frames(placed)
            self.assertTrue(nk.frames_confident(screened))
            stacked = nk.superimpose(screened, "clipped", str(root / "out"))
            self.assertTrue(nk.stack_valid(stacked))
            self.assertTrue(nk.stack_true(stacked, "*.jpg"))

    def test_a_stack_that_errored_is_not_true(self):
        self.assertFalse(nk.stack_true(json.dumps({"error": "nothing"})))


class CoverageTests(unittest.TestCase):
    """The band only some frames cover is cropped, not shown."""

    def placed(self, *frames):
        return json.dumps({"frames": [
            {"name": "ref", "dx": 0.0, "dy": 0.0, "agreed": 140},
            *frames]})

    def test_shifts_set_the_inset(self):
        told = self.placed({"name": "a", "dx": -7.8, "dy": 2.0,
                            "agreed": 120})
        self.assertGreaterEqual(nk.coverage_inset(told), 8)
        self.assertLess(nk.coverage_inset(told), 20)

    def test_a_roll_widens_it(self):
        # A one-degree roll carries the far edge sideways by around
        # seventy pixels; the wedge it leaves must be inside the crop.
        told = self.placed({"name": "a", "dx": -40.0, "dy": 4.0,
                            "turn": 1.0, "agreed": 52})
        self.assertGreater(nk.coverage_inset(told), 90)

    def test_nothing_to_crop_for_a_steady_tripod(self):
        told = self.placed({"name": "a", "dx": -1.2, "dy": 0.1,
                            "agreed": 130})
        self.assertLessEqual(nk.coverage_inset(told), 6)

    def test_garbage_asks_for_no_crop(self):
        self.assertEqual(nk.coverage_inset(""), 0)
        self.assertEqual(nk.coverage_inset("{}"), 0)


class LevelCapTests(unittest.TestCase):
    """However dark a look is wanted, the cut stops above the sky."""

    def test_the_level_is_capped_by_the_darkest_percent(self):
        import tifffile


        rng = np.random.default_rng(9)
        # a sky whose corners arrive much lower than its middle
        field = np.full((300, 400, 3), 0.06, np.float32)
        yy, xx = np.mgrid[0:300, 0:400]
        edge = np.hypot((yy - 150) / 150.0, (xx - 200) / 200.0)
        field *= (1.0 - 0.55 * np.clip(edge - 0.4, 0, 1))[..., None]
        field += rng.normal(0, 0.004, field.shape).astype(np.float32)
        field = np.clip(field, 0, 1)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            tifffile.imwrite(root / "stack.tiff",
                             (field * 65535).astype(np.uint16))
            from PIL import Image

            Image.fromarray((field * 255).astype(np.uint8)).save(
                root / "frame.png")
            told = json.dumps({"stack": str(root / "stack.tiff"),
                               "photos": str(root), "frames_used": 5})
            shown = nk.develop_show(told, "", "*.png", 100.0, 40.0)
        report_ = json.loads(shown)
        self.assertLess(report_["level_used"], report_["level_asked"])
        self.assertLessEqual(
            report_["range"]["black_clipped_percent"], 2.0)


class FlattenDialTests(unittest.TestCase):
    """The sky's weather is a choice, not an inevitability."""

    def sloped_stack(self, root: Path) -> str:
        import tifffile
        from PIL import Image

        rng = np.random.default_rng(11)
        field = np.full((300, 400, 3), 0.05, np.float32)
        # a strong slope, so the night start reaches for the flatten
        field *= np.linspace(0.55, 1.45, 400)[None, :, None]
        field += rng.normal(0, 0.003, field.shape).astype(np.float32)
        field = np.clip(field, 0, 1)
        tifffile.imwrite(root / "stack.tiff",
                         (field * 65535).astype(np.uint16))
        Image.fromarray((field * 255).astype(np.uint8)).save(
            root / "frame.png")
        return json.dumps({"stack": str(root / "stack.tiff"),
                           "photos": str(root), "frames_used": 5})

    def test_zero_keeps_the_weather_and_a_hundred_takes_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            told = self.sloped_stack(root)
            flat = json.loads(nk.develop_show(
                told, "", "*.png", 100.0, 10.0, 100.0))
            weather = json.loads(nk.develop_show(
                told, "", "*.png", 100.0, 10.0, 0.0))
        self.assertIn("color.background", flat["operations"])
        self.assertNotIn("color.background", weather["operations"])
        self.assertEqual(flat["flatten"], 100.0)
        self.assertEqual(weather["flatten"], 0.0)
        # And the dial did what it says: the flattened sky is more
        # even than the one that kept its slope.
        self.assertLess(flat["sky"]["unevenness"],
                        weather["sky"]["unevenness"] * 0.7)

    def test_halfway_is_written_into_the_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            told = self.sloped_stack(root)
            half = json.loads(nk.develop_show(
                told, "", "*.png", 100.0, 10.0, 50.0))
        self.assertEqual(half["flatten"], 50.0)
        self.assertIn("color.background", half["operations"])


class ProgramTests(unittest.TestCase):
    def test_the_program_is_a_built_in(self):
        from opencull_gui.programs import BUILT_INS

        names = [name for name, _purpose in BUILT_INS]
        self.assertIn("night_show.kim", names)

    @unittest.skipUnless(kimiya_runs(), "no Kimiya compiler here")
    def test_the_program_compiles(self):
        from opencull_gui.programs import ProgramStore

        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as folder:
            store = ProgramStore(Path(folder) / "Programs", root,
                                 python=sys.executable)
            ok, said = store.check("night_show.kim")
        self.assertTrue(ok, said)


class ColourPreserveTests(unittest.TestCase):
    """The stretch lifts the sky without bleaching the stars."""

    def test_the_dial_keeps_a_red_star_red(self):
        from development_engine import _apply_global, _decoded, _encoded

        shown = np.full((24, 24, 3), 0.02, np.float32)
        shown[12, 12] = (0.30, 0.12, 0.08)          # a faint red star
        linear = _decoded(shown).astype(np.float32)
        stretch = {"op": "tone.stretch", "unit": "percent",
                   "mode": "delta", "value": 60.0, "enabled": True}
        keep = {"op": "tone.preserve", "unit": "percent",
                "mode": "delta", "value": 100.0, "enabled": True}
        plain = np.clip(_encoded(np.clip(_apply_global(
            linear, [stretch]), 0, None)), 0, 1)
        held = np.clip(_encoded(np.clip(_apply_global(
            linear, [keep, stretch]), 0, None)), 0, 1)

        def redness(a):
            px = a[12, 12]
            return float((px.max() - px.min()) / max(px.max(), 1e-6))

        before = redness(shown)
        # The per-channel stretch bleaches; the luminance one keeps
        # nearly all of what the star had -- "nearly", because a lifted
        # strong channel meets the top of the range and loses a sliver
        # there, which is the range's fault and not the stretch's.
        self.assertLess(redness(plain), before * 0.75)
        self.assertGreater(redness(held), before * 0.85)
        self.assertGreater(redness(held), redness(plain) * 1.2)
        # And both lift the star.
        self.assertGreater(float(held[12, 12].max()),
                           float(shown[12, 12].max()))

    def test_night_start_asks_for_the_colour(self):
        import tempfile as tf

        from PIL import Image

        from opencull_gui import nightstart

        rng = np.random.default_rng(3)
        field = np.full((240, 320, 3), 0.02, np.float32)
        field += rng.normal(0, 0.01, field.shape).astype(np.float32)
        with tf.TemporaryDirectory() as folder:
            path = Path(folder) / "sky.png"
            Image.fromarray(
                (np.clip(field, 0, 1) * 255).astype(np.uint8)).save(path)
            told = nightstart.night_start(path, shown=np.clip(field, 0, 1))
        names = [item["op"] for item in told["operations"]]
        if "tone.stretch" in names:
            self.assertIn("tone.preserve", names)
            self.assertLess(names.index("tone.preserve"),
                            names.index("tone.stretch"))


if __name__ == "__main__":
    unittest.main()
