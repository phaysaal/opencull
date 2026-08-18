"""Many frames of one sky, laid on each other."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import superimpose_kernel as sk

HEIGHT, WIDTH = 300, 400


def night(folder: Path, drifts: list[tuple[float, float]],
          noise: float = 0.045, streak_on: int | None = None) -> None:
    """A synthetic sky: fixed stars, a stated drift, honest noise."""
    rng = np.random.default_rng(5)
    xs = rng.uniform(30, WIDTH - 30, 80)
    ys = rng.uniform(30, HEIGHT - 30, 80)
    brightness = rng.uniform(0.3, 0.95, 80)
    grid_y, grid_x = np.mgrid[0:HEIGHT, 0:WIDTH]
    for index, (dx, dy) in enumerate(drifts):
        field = np.zeros((HEIGHT, WIDTH), np.float32) + 0.04
        for x, y, bright in zip(xs + dx, ys + dy, brightness):
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                field += bright * np.exp(
                    -(((grid_x - x) ** 2 + (grid_y - y) ** 2)
                      / (2 * 1.6 ** 2)))
        field = np.clip(field + np.random.default_rng(
            300 + index).normal(0, noise, (HEIGHT, WIDTH)), 0, 1)
        if index == streak_on:
            field[150:153, 40:360] = 0.95       # a satellite crosses
        Image.fromarray(
            (np.stack([field] * 3, -1) * 255 + 0.5).astype(np.uint8)
        ).save(folder / f"N{index:03d}.jpg", quality=98)


class SkyBoundTests(unittest.TestCase):
    """The sky's own clock, turned into a search radius."""

    def test_a_longer_gap_can_hide_a_longer_move(self):
        near = sk.drift_bound(10.0, 24.0, 6000, 23.5)
        far = sk.drift_bound(60.0, 24.0, 6000, 23.5)
        self.assertAlmostEqual(far / near, 6.0, places=3)

    def test_a_longer_lens_magnifies_the_same_turning(self):
        wide = sk.drift_bound(30.0, 14.0, 6000, 23.5)
        long = sk.drift_bound(30.0, 200.0, 6000, 23.5)
        self.assertGreater(long, wide * 10)

    def test_an_unknown_lens_states_no_bound(self):
        self.assertEqual(sk.drift_bound(30.0, 0.0, 6000, 23.5), 0.0)

    def test_the_rate_is_sidereal_not_solar(self):
        # 15.041 arcsec/second, not 15.000: the sky is not the sun.
        self.assertAlmostEqual(sk.SIDEREAL_ARCSEC, 15.041, places=3)


class StarFindingTests(unittest.TestCase):
    def test_the_stars_are_found_where_they_were_put(self):
        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder), [(0.0, 0.0)])
            grey = sk._grey(sk._frame(
                Path(folder) / "N000.jpg", demosaic=False))
            found = sk.stars_in(grey)
            self.assertGreater(len(found), 70)      # 80 were planted
            self.assertLessEqual(len(found), sk.STARS_WANTED)

    def test_an_empty_sky_yields_no_stars(self):
        flat = np.full((80, 80), 0.04, np.float32)
        self.assertEqual(len(sk.stars_in(flat)), 0)


class RegistrationTests(unittest.TestCase):
    DRIFTS = [(0.0, 0.0), (7.0, 3.0), (14.0, 6.0), (21.0, 9.0),
              (28.0, 12.0)]

    def test_a_known_drift_is_recovered_to_a_fraction_of_a_pixel(self):
        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder), self.DRIFTS)
            told = json.loads(sk.register(
                folder, "*.jpg", demosaic=False))
            for item, (dx, dy) in zip(told["frames"], self.DRIFTS):
                # The shift is the correction: what moves the frame
                # back onto the reference, so it is the drift negated.
                self.assertAlmostEqual(item["dx"], -dx, delta=0.5)
                self.assertAlmostEqual(item["dy"], -dy, delta=0.5)
                self.assertGreaterEqual(
                    item["agreed"], sk.LEAST_AGREEING)

    def test_one_frame_needs_no_registering(self):
        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder), [(0.0, 0.0)])
            told = json.loads(sk.register(
                folder, "*.jpg", demosaic=False))
            self.assertEqual(len(told["frames"]), 1)
            self.assertIn("nothing to register", told["note"])

    def test_the_vote_refuses_what_lies_outside_the_bound(self):
        # Stars twenty pixels apart, but the sky is told it cannot have
        # moved more than three: the true answer is out of reach and
        # the vote must not invent it.
        reference = np.array([[10.0, 10.0], [50.0, 60.0], [90.0, 20.0]],
                             np.float32)
        moving = reference + 20.0
        shift, agreed = sk.vote_shift(reference, moving, radius=3.0)
        self.assertLess(abs(shift[0] + 20.0), 40.0)
        self.assertLess(agreed, len(reference))


class StackingTests(unittest.TestCase):
    DRIFTS = [(index * 5.0, index * 2.0) for index in range(9)]

    def sky_noise(self, path) -> float:
        held = np.asarray(
            Image.open(path).convert("L"), float) / 255.0
        return float(np.std(held[5:40, 5:40]))    # a corner of sky

    def test_averaging_buys_the_square_root_of_the_count(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            night(root, self.DRIFTS)
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = json.loads(sk.superimpose(
                placed, "average", output=str(root / "out")))
            single = self.sky_noise(root / "N000.jpg")
            stacked = self.sky_noise(told["proof"])
            # Nine frames: theory says three times cleaner.
            self.assertGreater(single / stacked, 2.2)

    def test_clipping_leaves_the_satellite_out(self):
        ruined = 4
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            night(root, self.DRIFTS, streak_on=ruined)
            placed = sk.register(folder, "*.jpg", demosaic=False)
            plain = json.loads(sk.superimpose(
                placed, "average", output=str(root / "plain")))
            clipped = json.loads(sk.superimpose(
                placed, "clipped", output=str(root / "clipped")))
            # The streak was painted in the ruined frame's own pixels,
            # and registration then moved that frame -- so the stack
            # carries it wherever the frame's own correction put it.
            shift = json.loads(placed)["frames"][ruined]
            row = int(round(151 + shift["dy"]))
            column = int(round(200 + shift["dx"]))

            def at_streak(told):
                held = np.asarray(
                    Image.open(told["proof"]).convert("L"), float) / 255.0
                return float(held[row, column])
            self.assertGreater(at_streak(plain), 0.12)   # it is there
            self.assertLess(at_streak(clipped), at_streak(plain) / 2)

    def test_trails_keep_the_brightest_thing_that_ever_crossed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            night(root, self.DRIFTS, streak_on=4)
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = json.loads(sk.superimpose(
                placed, "trails", output=str(root / "out")))
            held = np.asarray(
                Image.open(told["proof"]).convert("L"), float) / 255.0
            # Trails move nothing, so the streak is where it was drawn.
            self.assertGreater(held[151, 200], 0.9)   # the streak stands
            self.assertEqual(told["mode"], "trails")

    def test_the_stack_is_written_as_a_developable_tiff(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            night(root, self.DRIFTS[:4])
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = json.loads(sk.superimpose(
                placed, "clipped", output=str(root / "out")))
            stack = Path(told["stack"])
            self.assertTrue(stack.is_file())
            with Image.open(stack) as opened:
                self.assertEqual(opened.size, (WIDTH, HEIGHT))
            self.assertTrue(Path(told["proof"]).is_file())

    def test_an_unknown_mode_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder), self.DRIFTS[:3])
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = json.loads(sk.superimpose(placed, "cheese"))
            self.assertIn("cheese", told["error"])
            self.assertFalse(sk.stack_valid(json.dumps(told)))


class ProgramTests(unittest.TestCase):
    def test_a_finished_stack_passes_its_check_and_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            night(root, [(0.0, 0.0), (5.0, 2.0), (10.0, 4.0)])
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = sk.superimpose(placed, "clipped",
                                  output=str(root / "out"))
            self.assertTrue(sk.stack_valid(told))
            self.assertIn("registered", sk.stack_note(told))
            self.assertTrue(sk.stack_home(told).endswith(".json"))

    def test_nothing_to_stack_fails_the_check_politely(self):
        told = json.dumps({"format": sk.FORMAT, "photos": ".",
                           "frames": []})
        self.assertFalse(sk.stack_valid(sk.superimpose(told, "trails")))
        self.assertIn("no frames", sk.stack_note(
            sk.superimpose(told, "trails")))

    def test_the_program_is_in_the_catalogue(self):
        from opencull_gui.programs import BUILT_INS

        self.assertIn("superimpose.kim",
                      [name for name, _purpose in BUILT_INS])


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:                          # pragma: no cover
            raise unittest.SkipTest("PySide6 is not installed") from None
        cls.application = QApplication.instance() or QApplication([])

    def test_the_dialog_asks_the_program_for_what_was_chosen(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder), [(0.0, 0.0), (4.0, 1.0)])
            dialog = SuperimposeDialog(folder, selection=["N000.jpg"])
            self.addCleanup(dialog.deleteLater)
            dialog.clipped.setChecked(True)
            dialog.focal.setValue(24.0)
            dialog.where.setText(str(Path(folder) / "out"))
            request = dialog.run_request()
            self.assertEqual(request["program"], "superimpose.kim")
            told = request["parameters"]
            self.assertEqual(told["mode"], "clipped")
            self.assertEqual(told["focal_mm"], "24.0")
            # The marked frames ride in a file, not in a parameter.
            self.assertTrue(told["only"].endswith("selection.json"))
            self.assertEqual(
                json.loads(Path(told["only"]).read_text())["names"],
                ["N000.jpg"])

    def test_the_three_pictures_are_the_three_modes(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.mode(), "trails")
            dialog.clipped.setChecked(True)
            self.assertEqual(dialog.mode(), "clipped")
            dialog.average.setChecked(True)
            self.assertEqual(dialog.mode(), "average")
            self.assertEqual(sorted(sk.MODES),
                             sorted(("trails", "clipped", "average")))


if __name__ == "__main__":
    unittest.main()
