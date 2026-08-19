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


def spun(px, py, degrees, cx, cy):
    """Star positions turned about a point, for a synthetic sky."""
    angle = np.radians(degrees)
    cos, sin = np.cos(angle), np.sin(angle)
    dx, dy = px - cx, py - cy
    return cx + dx * cos - dy * sin, cy + dx * sin + dy * cos


def turning_night(folder: Path, turns: list[float],
                  pole: tuple[float, float] = (120.0, 480.0),
                  step: float = 240.0, stamped: bool = True) -> None:
    """A sky that turns about a pole, with the clock to prove it."""
    rng = np.random.default_rng(5)
    xs = rng.uniform(40, WIDTH - 40, 80)
    ys = rng.uniform(40, HEIGHT - 40, 80)
    brightness = rng.uniform(0.35, 0.95, 80)
    grid_y, grid_x = np.mgrid[0:HEIGHT, 0:WIDTH]
    for index, turn in enumerate(turns):
        px, py = spun(xs, ys, turn, *pole)
        field = np.zeros((HEIGHT, WIDTH), np.float32) + 0.04
        for x, y, bright in zip(px, py, brightness):
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                field += bright * np.exp(
                    -(((grid_x - x) ** 2 + (grid_y - y) ** 2)
                      / (2 * 1.6 ** 2)))
        field = np.clip(field + np.random.default_rng(
            600 + index).normal(0, 0.04, (HEIGHT, WIDTH)), 0, 1)
        image = Image.fromarray(
            (np.stack([field] * 3, -1) * 255 + 0.5).astype(np.uint8))
        exif = Image.Exif()
        if stamped:
            seconds = int(index * step)
            exif[36867] = (f"2026:08:18 22:{10 + seconds // 60:02d}:"
                           f"{seconds % 60:02d}")
            exif[306] = exif[36867]
        image.save(folder / f"S{index:03d}.jpg", quality=98, exif=exif)


def handheld_night(folder: Path,
                   held: list[tuple[float, float, float]]) -> None:
    """A sky a hand held: rolled and moved, arbitrarily."""
    rng = np.random.default_rng(5)
    xs = rng.uniform(40, WIDTH - 40, 80)
    ys = rng.uniform(40, HEIGHT - 40, 80)
    brightness = rng.uniform(0.35, 0.95, 80)
    grid_y, grid_x = np.mgrid[0:HEIGHT, 0:WIDTH]
    for index, (degrees, dx, dy) in enumerate(held):
        px, py = spun(xs, ys, degrees, WIDTH / 2, HEIGHT / 2)
        field = np.zeros((HEIGHT, WIDTH), np.float32) + 0.04
        for x, y, bright in zip(px + dx, py + dy, brightness):
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                field += bright * np.exp(
                    -(((grid_x - x) ** 2 + (grid_y - y) ** 2)
                      / (2 * 1.6 ** 2)))
        field = np.clip(field + np.random.default_rng(
            700 + index).normal(0, 0.04, (HEIGHT, WIDTH)), 0, 1)
        Image.fromarray(
            (np.stack([field] * 3, -1) * 255 + 0.5).astype(np.uint8)
        ).save(folder / f"H{index:03d}.jpg", quality=98)


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
            night(Path(folder).resolve(), [(0.0, 0.0)])
            grey = sk._grey(sk._frame(
                Path(folder).resolve() / "N000.jpg", demosaic=False))
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
            night(Path(folder).resolve(), self.DRIFTS)
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
            night(Path(folder).resolve(), [(0.0, 0.0)])
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
            root = Path(folder).resolve()
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
            root = Path(folder).resolve()
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
            root = Path(folder).resolve()
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
            root = Path(folder).resolve()
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
            night(Path(folder).resolve(), self.DRIFTS[:3])
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = json.loads(sk.superimpose(placed, "cheese"))
            self.assertIn("cheese", told["error"])
            self.assertFalse(sk.stack_valid(json.dumps(told)))


class RotationTests(unittest.TestCase):
    """The sky's turn read off the clock, and a hand's voted on."""

    TURNS = [0.0, 1.003, 2.005, 3.008, 4.011, 5.014]

    def test_the_clock_gives_the_angle_without_searching_for_it(self):
        self.assertAlmostEqual(sk.sky_rotation(86164.0905), 360.0,
                               places=6)
        self.assertAlmostEqual(sk.sky_rotation(600), 2.507, places=3)

    def test_a_turning_sky_is_registered_by_its_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            turning_night(Path(folder).resolve(), self.TURNS)
            told = json.loads(sk.register(
                folder, "*.jpg", demosaic=False, focal_mm=24.0))
            for index, item in enumerate(told["frames"]):
                self.assertAlmostEqual(
                    abs(item["turn"]), self.TURNS[index], delta=0.1)
                self.assertGreaterEqual(
                    item["agreed"], sk.LEAST_AGREEING)

    def test_rotation_keeps_the_stars_as_points(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            turning_night(root, self.TURNS)
            placed = json.loads(sk.register(
                folder, "*.jpg", demosaic=False, focal_mm=24.0))
            flattened = {**placed, "frames": [
                {**item, "turn": 0.0} for item in placed["frames"]]}

            def peak(told):
                held = np.asarray(Image.open(
                    told["proof"]).convert("L"), float) / 255.0
                return float(held.max())
            turned = json.loads(sk.superimpose(
                json.dumps(placed), "average", output=str(root / "a")))
            flat = json.loads(sk.superimpose(
                json.dumps(flattened), "average", output=str(root / "b")))
            # Ignoring the turn smears every star into an arc.
            self.assertGreater(peak(turned), peak(flat) * 1.3)

    def test_a_hand_roll_is_voted_on_by_star_pairs(self):
        held = [(0.0, 0, 0), (7.0, 60, -40), (-11.0, -80, 55),
                (16.0, 120, 90)]
        with tempfile.TemporaryDirectory() as folder:
            handheld_night(Path(folder).resolve(), held)
            told = json.loads(sk.register(
                folder, "*.jpg", demosaic=False, handheld=True))
            for item, (degrees, _dx, _dy) in zip(told["frames"], held):
                self.assertAlmostEqual(
                    abs(item["turn"]), abs(degrees), delta=0.2)
                self.assertGreaterEqual(
                    item["agreed"], sk.LEAST_AGREEING)

    def test_the_turn_vote_folds_at_half_a_circle(self):
        # A pair of stars has no head and no tail, so an answer and
        # its opposite are the same evidence; the caller scores both.
        points = np.random.default_rng(3).uniform(
            20, 380, (40, 2)).astype(np.float32)
        rolled = sk.turned(points, 12.0, (200.0, 150.0))
        angle, agreed = sk.vote_rotation(points, rolled)
        self.assertAlmostEqual(angle % 180.0, (-12.0) % 180.0, delta=0.3)
        self.assertGreater(agreed, 100)

    def test_a_wrong_lens_widens_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as folder:
            turning_night(Path(folder).resolve(), self.TURNS)
            # 200mm on a sky shot at 24: the bound is far too tight,
            # and without the widening every frame would come back
            # unregistered.
            told = json.loads(sk.register(
                folder, "*.jpg", demosaic=False, focal_mm=200.0))
            agreed = [item["agreed"] for item in told["frames"][1:]]
            self.assertTrue(all(
                count >= sk.LEAST_AGREEING for count in agreed))


class WarpTests(unittest.TestCase):
    def test_a_turn_and_a_fraction_land_where_they_should(self):
        frame = np.zeros((60, 80, 3), np.float32)
        frame[30, 40] = 1.0                      # one lit pixel, centred
        moved = sk._warped(frame, 0.0, (3.0, 2.0))
        self.assertAlmostEqual(float(moved[32, 43, 0]), 1.0, places=3)

    def test_half_a_pixel_is_shared_between_two(self):
        frame = np.zeros((60, 80, 3), np.float32)
        frame[30, 40] = 1.0
        moved = sk._warped(frame, 0.0, (0.5, 0.0))
        self.assertAlmostEqual(float(moved[30, 40, 0]), 0.5, places=3)
        self.assertAlmostEqual(float(moved[30, 41, 0]), 0.5, places=3)

    def test_what_falls_outside_is_black_not_wrapped(self):
        frame = np.ones((40, 40, 3), np.float32)
        moved = sk._warped(frame, 0.0, (10.0, 0.0))
        self.assertEqual(float(moved[20, 2, 0]), 0.0)
        self.assertAlmostEqual(float(moved[20, 20, 0]), 1.0, places=3)


class ModelAnchorTests(unittest.TestCase):
    """What a model is asked, and what is done with the answer."""

    def test_the_prompt_asks_for_recognition_not_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder).resolve(), [(0.0, 0.0)])
            asked = sk.group_prompt(str(Path(folder).resolve() / "N000.jpg"))
            self.assertIn("recognise", asked)
            self.assertIn(f"{WIDTH}x{HEIGHT}", asked)
            # A guess is worse than nothing, and it says so.
            self.assertIn("visible=false", asked)

    def test_two_frames_seeing_one_group_give_a_coarse_shift(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            night(root, [(0.0, 0.0), (0.0, 0.0)])
            first = str(root / "N000.jpg")
            second = str(root / "N001.jpg")
            hints = sk.collect_group("", folder, "N000.jpg", first, {
                "group": "Orion's Belt", "x0": 100, "y0": 100,
                "x1": 140, "y1": 140, "visible": True})
            hints = sk.collect_group(hints, folder, "N001.jpg", second, {
                "group": "orions belt", "x0": 150, "y0": 130,
                "x1": 190, "y1": 170, "visible": True})
            told = json.loads(hints)
            self.assertTrue(sk.hints_usable(hints))
            self.assertAlmostEqual(
                told["shifts"]["N001.jpg"][0], -50.0, delta=1.0)
            self.assertAlmostEqual(
                told["shifts"]["N001.jpg"][1], -30.0, delta=1.0)

    def test_a_frame_recognising_nothing_says_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder).resolve(), [(0.0, 0.0)])
            hints = sk.collect_group(
                "", folder, "N000.jpg", str(Path(folder).resolve() / "N000.jpg"),
                {"group": "", "x0": 0, "y0": 0, "x1": 0, "y1": 0,
                 "visible": False})
            self.assertFalse(sk.hints_usable(hints))
            self.assertIn("No star pattern", sk.hints_note(hints))

    def test_different_groups_are_not_compared_with_each_other(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            night(root, [(0.0, 0.0), (0.0, 0.0)])
            hints = sk.collect_group(
                "", folder, "N000.jpg", str(root / "N000.jpg"),
                {"group": "Orion", "x0": 100, "y0": 100, "x1": 140,
                 "y1": 140, "visible": True})
            hints = sk.collect_group(
                hints, folder, "N001.jpg", str(root / "N001.jpg"),
                {"group": "Cassiopeia", "x0": 300, "y0": 40, "x1": 340,
                 "y1": 80, "visible": True})
            told = json.loads(hints)
            # Two different patterns are two anchors, not one shift.
            self.assertEqual(told["shifts"], {})
            self.assertEqual(len(told["seen"]), 2)


class CalibrationTests(unittest.TestCase):
    """Darks come off, flats divide out, and the order is not a taste."""

    def calibration(self, folder: Path, name: str, level, count: int = 3):
        home = folder / name
        home.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            field = np.asarray(level, np.float32)
            if field.ndim == 0:
                field = np.full((HEIGHT, WIDTH, 3), float(level),
                                np.float32)
            noisy = np.clip(field + np.random.default_rng(
                900 + index).normal(0, 0.002, field.shape), 0, 1)
            Image.fromarray(
                (noisy * 255 + 0.5).astype(np.uint8)
            ).save(home / f"{name}{index}.jpg", quality=99)
        return str(home)

    def vignetted(self):
        """A flat that is bright in the middle and dark at the corners."""
        grid_y, grid_x = np.mgrid[0:HEIGHT, 0:WIDTH]
        away = np.sqrt(((grid_x - WIDTH / 2) / (WIDTH / 2)) ** 2
                       + ((grid_y - HEIGHT / 2) / (HEIGHT / 2)) ** 2)
        shape = np.clip(0.9 - 0.35 * away, 0.2, 1.0).astype(np.float32)
        return np.stack([shape] * 3, -1)

    def test_a_flat_takes_the_vignetting_out(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            lit = root / "lights"
            lit.mkdir()
            shape = self.vignetted()
            # An even sky seen through a vignetting lens.
            for index in range(3):
                seen = np.clip(0.5 * shape + np.random.default_rng(
                    950 + index).normal(0, 0.004, shape.shape), 0, 1)
                Image.fromarray(
                    (seen * 255 + 0.5).astype(np.uint8)
                ).save(lit / f"L{index}.jpg", quality=99)
            flats = self.calibration(root, "flats", shape)
            placed = json.dumps({
                "format": sk.FORMAT, "photos": str(lit),
                "pattern": "*.jpg", "demosaic": False,
                "frames": [{"name": f"L{i}.jpg", "dx": 0.0, "dy": 0.0,
                            "turn": 0.0} for i in range(3)]})

            def corner_against_middle(told):
                held = np.asarray(Image.open(
                    told["proof"]).convert("L"), float) / 255.0
                return (float(held[20:40, 20:40].mean())
                        / max(float(held[140:160, 190:210].mean()), 1e-6))
            plain = json.loads(sk.superimpose(
                placed, "average", output=str(root / "plain")))
            flattened = json.loads(sk.superimpose(
                placed, "average", output=str(root / "flat"),
                flats=flats))
            self.assertTrue(flattened["flat_divided"])
            # Corners were far darker; after the flat they match.
            self.assertLess(corner_against_middle(plain), 0.75)
            self.assertGreater(corner_against_middle(flattened), 0.9)

    def test_a_flat_corrects_shape_and_not_colour(self):
        # A flat shot on a warm panel must not cool every frame it
        # touches, so it is normalised per channel.
        warm = np.zeros((HEIGHT, WIDTH, 3), np.float32)
        warm[..., 0], warm[..., 1], warm[..., 2] = 0.8, 0.6, 0.4
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            flats = self.calibration(root, "flats", warm)
            made = sk._master_flat(flats, "*.jpg", False)
            for channel in range(3):
                self.assertAlmostEqual(
                    float(np.median(made[..., channel])), 1.0, places=2)

    def test_a_dark_is_subtracted_and_a_bias_before_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            lit = root / "lights"
            lit.mkdir()
            for index in range(3):
                seen = np.full((HEIGHT, WIDTH, 3), 0.30, np.float32)
                Image.fromarray(
                    (seen * 255 + 0.5).astype(np.uint8)
                ).save(lit / f"L{index}.jpg", quality=99)
            darks = self.calibration(root, "darks", 0.08)
            placed = json.dumps({
                "format": sk.FORMAT, "photos": str(lit),
                "pattern": "*.jpg", "demosaic": False,
                "frames": [{"name": f"L{i}.jpg", "dx": 0.0, "dy": 0.0,
                            "turn": 0.0} for i in range(3)]})
            told = json.loads(sk.superimpose(
                placed, "average", output=str(root / "out"), darks=darks))
            self.assertTrue(told["dark_subtracted"])
            held = np.asarray(Image.open(
                told["proof"]).convert("L"), float) / 255.0
            self.assertAlmostEqual(float(held.mean()), 0.22, delta=0.02)

    def test_one_calibration_frame_is_not_a_master(self):
        with tempfile.TemporaryDirectory() as folder:
            alone = self.calibration(Path(folder).resolve(), "darks", 0.08, count=1)
            self.assertIsNone(sk._median_of(alone, "*.jpg", False))


class CloudScreenTests(unittest.TestCase):
    """What the stars already said about the sky they stood in."""

    def placed(self, counts):
        return json.dumps({
            "format": sk.FORMAT, "photos": ".", "pattern": "*.jpg",
            "frames": [{"name": f"F{index}.jpg", "stars": count,
                        "dx": 0.0, "dy": 0.0, "turn": 0.0}
                       for index, count in enumerate(counts)]})

    def test_three_bands_not_two(self):
        told = json.loads(sk.screen_frames(
            self.placed([80, 78, 20, 48, 82])))
        verdicts = [item["verdict"] for item in told["frames"]]
        self.assertEqual(verdicts,
                         ["clear", "clear", "clouded", "doubtful",
                          "clear"])
        self.assertFalse(told["frames"][2]["keep"])
        self.assertTrue(told["frames"][3]["keep"])   # doubtful is kept

    def test_the_doubtful_are_named_for_a_second_opinion(self):
        screened = sk.screen_frames(self.placed([80, 78, 20, 48, 82]))
        self.assertEqual(sk.doubtful(screened), ["F3.jpg"])
        self.assertIn("set aside", sk.screen_note(screened))

    def test_a_clear_night_sets_nothing_aside(self):
        screened = sk.screen_frames(self.placed([80, 78, 82, 79]))
        self.assertEqual(sk.doubtful(screened), [])
        self.assertIn("none set aside", sk.screen_note(screened))

    def test_a_frame_set_aside_is_not_in_the_stack(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            night(root, [(0.0, 0.0)] * 4)
            placed = json.loads(sk.register(
                folder, "*.jpg", demosaic=False))
            placed["frames"][1]["keep"] = False
            told = json.loads(sk.superimpose(
                json.dumps(placed), "average", output=str(root / "out")))
            self.assertEqual(told["frames_used"], 3)
            self.assertEqual(told["set_aside"], ["N001.jpg"])

    def test_a_sky_nobody_could_use_refuses_rather_than_pretends(self):
        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder).resolve(), [(0.0, 0.0)] * 3)
            placed = json.loads(sk.register(
                folder, "*.jpg", demosaic=False))
            for item in placed["frames"]:
                item["keep"] = False
            told = json.loads(sk.superimpose(
                json.dumps(placed), "average"))
            self.assertIn("set aside", told["error"])

    def test_the_screen_reads_star_counts_the_registration_took(self):
        with tempfile.TemporaryDirectory() as folder:
            night(Path(folder).resolve(), [(0.0, 0.0), (4.0, 1.0)])
            placed = json.loads(sk.register(
                folder, "*.jpg", demosaic=False))
            for item in placed["frames"]:
                self.assertGreater(item["stars"], 60)
                self.assertGreater(item["sky"], 0.0)

    def test_the_question_asked_is_about_use_not_beauty(self):
        asked = sk.cloud_prompt("x.jpg")
        self.assertIn("usable", asked)
        self.assertIn("poisons an average", asked)
        # Thin cloud that leaves stars visible is explicitly usable.
        self.assertIn("plainly visible", asked)

    def test_a_second_opinion_overrides_the_threshold(self):
        screened = sk.screen_frames(self.placed([80, 78, 20, 48, 82]))
        told = json.loads(sk.judge_frame(
            screened, "F3.jpg", {"usable": False, "why": "hazed over"}))
        doubted = told["frames"][3]
        self.assertFalse(doubted["keep"])
        self.assertEqual(doubted["verdict"], "clouded by eye")
        self.assertIn("hazed", doubted["why"])
        kept = json.loads(sk.judge_frame(
            screened, "F3.jpg", {"usable": True, "why": "thin cloud"}))
        self.assertTrue(kept["frames"][3]["keep"])
        self.assertEqual(kept["frames"][3]["verdict"], "kept by eye")


class DrizzleTests(unittest.TestCase):
    """Drops on a finer grid: what it buys, and what it costs."""

    def dithered(self, folder: Path, count: int = 16,
                 sigma: float = 0.75):
        """An UNDERSAMPLED sky, shifted by fractions of a pixel."""
        rng = np.random.default_rng(9)
        xs = rng.uniform(25, WIDTH - 25, 40)
        ys = rng.uniform(25, HEIGHT - 25, 40)
        bright = rng.uniform(0.5, 0.9, 40)
        grid_y, grid_x = np.mgrid[0:HEIGHT, 0:WIDTH]
        offsets = [(round(rng.uniform(-3, 3), 3),
                    round(rng.uniform(-3, 3), 3)) for _ in range(count)]
        for index, (dx, dy) in enumerate(offsets):
            field = np.zeros((HEIGHT, WIDTH), np.float32) + 0.03
            for x, y, level in zip(xs + dx, ys + dy, bright):
                field += level * np.exp(
                    -(((grid_x - x) ** 2 + (grid_y - y) ** 2)
                      / (2 * sigma ** 2)))
            field = np.clip(field + np.random.default_rng(
                1200 + index).normal(0, 0.02, (HEIGHT, WIDTH)), 0, 1)
            Image.fromarray(
                (np.stack([field] * 3, -1) * 255 + 0.5).astype(np.uint8)
            ).save(folder / f"D{index:03d}.jpg", quality=99)
        return offsets

    def star_sigma(self, path, scale: float) -> float:
        """Intensity-weighted width of the stars, in INPUT pixels."""
        held = np.asarray(
            Image.open(path).convert("L"), float) / 255.0
        base = float(np.median(held))
        reach = int(round(4 * scale))
        widths = []
        for x, y in sk.stars_in(held.astype(np.float32), wanted=30):
            xi, yi = int(round(x)), int(round(y))
            if not (reach < xi < held.shape[1] - reach
                    and reach < yi < held.shape[0] - reach):
                continue
            patch = np.clip(
                held[yi - reach:yi + reach + 1,
                     xi - reach:xi + reach + 1] - base, 0, None)
            if patch.sum() <= 0:
                continue
            grid_y, grid_x = np.mgrid[-reach:reach + 1, -reach:reach + 1]
            share = patch / patch.sum()
            mx = float((grid_x * share).sum())
            my = float((grid_y * share).sum())
            spread = float(((((grid_x - mx) ** 2 + (grid_y - my) ** 2))
                            * share).sum() / 2)
            widths.append(np.sqrt(spread))
        return float(np.median(widths)) / scale

    def test_the_grid_really_is_finer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.dithered(root, count=6)
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = json.loads(sk.superimpose(
                placed, "drizzle", output=str(root / "out"), scale=2.0))
            with Image.open(told["stack"]) as opened:
                self.assertEqual(opened.size, (WIDTH * 2, HEIGHT * 2))
            self.assertEqual(told["drizzle"]["scale"], 2.0)

    def test_a_sharp_average_is_not_beaten_by_drizzle_on_a_few_frames(self):
        """The claim this test used to make, corrected by measurement.

        It once asserted that drizzle came back with narrower stars
        than an average upsampled after the fact -- and it did, by
        about 6%, for as long as the averaging path aligned its frames
        BILINEARLY. That was drizzle beating a handicap, not drizzle
        beating interpolation. With the frames aligned properly the
        result reverses: on the user's own ten frames the average came
        back at 2.44 px against drizzle's 2.65, and on this synthetic
        the same way round.

        Which is what drizzle's own theory says. It reconstructs from
        many well-dithered samples, and a finer grid filled by ten
        frames hears from too few drops per output pixel. It is worth
        reaching for when there are dozens of frames, not a handful.
        """
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.dithered(root, count=20)
            placed = sk.register(folder, "*.jpg", demosaic=False)
            plain = json.loads(sk.superimpose(
                placed, "average", output=str(root / "avg")))
            drizzled = json.loads(sk.superimpose(
                placed, "drizzle", output=str(root / "dz"),
                scale=2.0, pixfrac=0.8))
            upsampled = root / "up.jpg"
            with Image.open(plain["proof"]) as opened:
                opened.resize((WIDTH * 2, HEIGHT * 2),
                              Image.Resampling.BICUBIC).save(
                    upsampled, quality=99)
            averaged = self.star_sigma(upsampled, 2.0)
            drizzle = self.star_sigma(drizzled["proof"], 2.0)
            self.assertLessEqual(averaged, drizzle)

    def test_the_stack_is_sharper_than_a_bilinear_one_would_be(self):
        """What the averaging path's resampler is actually worth.

        The frames are dithered by fractions of a pixel, so every one
        of them is resampled on the way into the stack. Aligning them
        bilinearly softens each frame before it is added; this is the
        guard that says the good resampler is still in place.
        """
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.dithered(root, count=12)
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = json.loads(sk.superimpose(
                placed, "average", output=str(root / "avg")))
            sharp = self.star_sigma(told["proof"], 1.0)
            # The same frames, aligned the old blurry way.
            frames = json.loads(placed)["frames"]
            blunt_stack = None
            for item in frames:
                held = np.asarray(Image.open(
                    root / item["name"]).convert("RGB"), np.float32) / 255.0
                moved = sk._warped(
                    held, float(item.get("turn", 0.0)),
                    (float(item["dx"]), float(item["dy"])))
                blunt_stack = moved if blunt_stack is None \
                    else blunt_stack + moved
            blunt_path = root / "blunt.jpg"
            Image.fromarray(
                (np.clip(blunt_stack / len(frames), 0, 1) * 255
                 ).astype(np.uint8)).save(blunt_path, quality=99)
            blunt = self.star_sigma(blunt_path, 1.0)
            self.assertLess(sharp, blunt)

    def test_the_finer_grid_is_filled_by_enough_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.dithered(root, count=16)
            placed = sk.register(folder, "*.jpg", demosaic=False)
            told = json.loads(sk.superimpose(
                placed, "drizzle", output=str(root / "out"),
                scale=2.0, pixfrac=0.8))
            self.assertLess(told["drizzle"]["unfilled"], 0.01)

    def test_a_drop_cannot_be_larger_than_a_pixel_or_smaller_than_a_speck(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.dithered(root, count=4)
            placed = sk.register(folder, "*.jpg", demosaic=False)
            wide = json.loads(sk.superimpose(
                placed, "drizzle", output=str(root / "a"),
                scale=9.0, pixfrac=8.0))
            self.assertEqual(wide["drizzle"]["pixfrac"], 1.0)
            self.assertEqual(wide["drizzle"]["scale"], 4.0)

    def test_drizzle_honours_the_calibration_and_the_screen(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.dithered(root, count=6)
            placed = json.loads(sk.register(
                folder, "*.jpg", demosaic=False))
            placed["frames"][1]["keep"] = False
            told = json.loads(sk.superimpose(
                json.dumps(placed), "drizzle",
                output=str(root / "out"), scale=2.0))
            self.assertEqual(told["frames_used"], 5)
            self.assertEqual(told["set_aside"], ["D001.jpg"])

    def test_a_drop_lands_where_the_transform_says(self):
        # One lit pixel, no turn, a shift of exactly one input pixel:
        # at twice the grid it must land two output pixels along.
        frame = np.zeros((20, 20, 3), np.float32)
        frame[10, 10] = 1.0
        values = np.zeros((40, 40, 3), np.float64)
        weights = np.zeros((40, 40), np.float64)
        sk._drizzle_frame(frame, values, weights, 0.0, (1.0, 0.0),
                          2.0, 1.0)
        lit = values[..., 0] / np.maximum(weights, 1e-9)
        found = np.argwhere(lit > 0.4)
        self.assertTrue(len(found))
        # centre of the drop: (10 + 1 + 0.5) * 2 = 23
        self.assertAlmostEqual(float(found[:, 1].mean()), 22.5, delta=1.0)
        self.assertAlmostEqual(float(found[:, 0].mean()), 20.5, delta=1.0)


class ProgramTests(unittest.TestCase):
    def test_a_finished_stack_passes_its_check_and_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
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

    def test_all_three_programs_are_in_the_catalogue(self):
        from opencull_gui.programs import BUILT_INS

        listed = [name for name, _purpose in BUILT_INS]
        for name in ("superimpose.kim", "handheld_stack.kim",
                     "clear_stack.kim"):
            self.assertIn(name, listed)


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
            night(Path(folder).resolve(), [(0.0, 0.0), (4.0, 1.0)])
            dialog = SuperimposeDialog(folder, selection=["N000.jpg"])
            self.addCleanup(dialog.deleteLater)
            dialog.clipped.setChecked(True)
            dialog.focal.setValue(24.0)
            dialog.where.setText(str(Path(folder).resolve() / "out"))
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

    def test_the_two_questions_do_not_fight_over_one_slot(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            dialog.clipped.setChecked(True)
            dialog.handheld.setChecked(True)
            # Choosing how it was held must not un-choose the picture.
            self.assertEqual(dialog.mode(), "clipped")
            self.assertEqual(dialog.run_request()["program"],
                             "handheld_stack.kim")
            dialog.tripod.setChecked(True)
            self.assertEqual(dialog.mode(), "clipped")
            self.assertEqual(dialog.run_request()["program"],
                             "superimpose.kim")

    def test_trails_need_no_holding_and_say_so(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            dialog.trails.setChecked(True)
            self.assertFalse(dialog.tripod.isEnabled())
            self.assertFalse(dialog.handheld.isEnabled())
            dialog.clipped.setChecked(True)
            self.assertTrue(dialog.tripod.isEnabled())

    def test_a_handheld_run_carries_keyframes_not_a_focal_length(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            dialog.clipped.setChecked(True)
            dialog.handheld.setChecked(True)
            told = dialog.run_request()["parameters"]
            self.assertIn("every", told)
            self.assertIn("proofs_dir", told)
            self.assertNotIn("focal_mm", told)

    def test_asking_about_doubtful_frames_picks_its_own_program(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            dialog.clipped.setChecked(True)
            dialog.tripod.setChecked(True)
            dialog.judge.setChecked(True)
            self.assertEqual(dialog.run_request()["program"],
                             "clear_stack.kim")
            dialog.judge.setChecked(False)
            self.assertEqual(dialog.run_request()["program"],
                             "superimpose.kim")

    def test_trails_cannot_be_poisoned_so_are_not_offered_the_ask(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            dialog.trails.setChecked(True)
            self.assertFalse(dialog.judge.isEnabled())
            dialog.clipped.setChecked(True)
            self.assertTrue(dialog.judge.isEnabled())

    def test_the_calibration_folders_reach_the_program(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            dialog.darks.setText("/darks")
            dialog.flats.setText("/flats")
            dialog.bias.setText("/bias")
            told = dialog.run_request()["parameters"]
            self.assertEqual(told["darks"], "/darks")
            self.assertEqual(told["flats"], "/flats")
            self.assertEqual(told["bias"], "/bias")

    def test_every_picture_offered_is_a_mode_the_kernel_has(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.mode(), "trails")
            for choice, name in ((dialog.clipped, "clipped"),
                                 (dialog.drizzle, "drizzle"),
                                 (dialog.average, "average")):
                choice.setChecked(True)
                self.assertEqual(dialog.mode(), name)
                self.assertIn(name, sk.MODES)
            self.assertEqual(len(sk.MODES), 4)

    def test_the_drizzle_numbers_are_idle_until_drizzling(self):
        from opencull_qt.superimpose import SuperimposeDialog

        with tempfile.TemporaryDirectory() as folder:
            dialog = SuperimposeDialog(folder)
            self.addCleanup(dialog.deleteLater)
            dialog.clipped.setChecked(True)
            self.assertFalse(dialog.scale.isEnabled())
            dialog.drizzle.setChecked(True)
            self.assertTrue(dialog.scale.isEnabled())
            self.assertTrue(dialog.pixfrac.isEnabled())
            told = dialog.run_request()["parameters"]
            self.assertEqual(told["scale"], "2.0")
            self.assertEqual(told["pixfrac"], "0.8")


class ResamplingTests(unittest.TestCase):
    """A frame moved between its pixels should not lose its stars."""

    def starry(self, seed=4, count=60):
        rng = np.random.default_rng(seed)
        field = np.zeros((HEIGHT, WIDTH, 3), np.float32) + 0.03
        grid_y, grid_x = np.mgrid[0:HEIGHT, 0:WIDTH]
        for _ in range(count):
            x = rng.uniform(20, WIDTH - 20)
            y = rng.uniform(20, HEIGHT - 20)
            bright = rng.uniform(0.2, 0.8)
            field += (bright * np.exp(
                -((grid_x - x) ** 2 + (grid_y - y) ** 2) / 3.2)
            )[..., None].astype(np.float32)
        return np.clip(field, 0, 1)

    def peak_kept(self, moved, held):
        """What became of the brightest star's peak."""
        inner = (slice(12, -12), slice(12, -12))
        return (float(moved[inner].max()) / float(held[inner].max()))

    def test_lanczos_keeps_a_star_where_bilinear_softens_it(self):
        held = self.starry()
        shift = (0.37, 0.29)
        soft = sk._warped(held, 0.0, shift)
        sharp = sk._lanczos_shift(held, shift)
        self.assertLess(self.peak_kept(soft, held), 0.92)
        self.assertGreater(self.peak_kept(sharp, held), 0.97)

    def round_trip_error(self, move):
        """Half a pixel there and half a pixel back, against the original."""
        held = self.starry()
        inner = (slice(20, -20), slice(20, -20))
        back = move(move(held, (0.5, 0.5)), (-0.5, -0.5))
        return float(np.abs(back[inner] - held[inner]).mean())

    def test_half_a_pixel_there_and_back_comes_back(self):
        soft = self.round_trip_error(lambda a, s: sk._warped(a, 0.0, s))
        sharp = self.round_trip_error(sk._lanczos_shift)
        self.assertLess(sharp, soft)

    def test_a_whole_pixel_move_is_not_a_resample(self):
        held = self.starry()
        moved = sk._lanczos_shift(held, (3.0, -2.0))
        plain = sk._shifted(held, (3.0, -2.0))
        inner = (slice(12, -12), slice(12, -12))
        self.assertTrue(np.allclose(moved[inner], plain[inner], atol=1e-5))

    def test_what_falls_outside_is_black_not_dim(self):
        """A rim that collected half a kernel would darken the edge."""
        held = self.starry()
        moved = sk._lanczos_shift(held, (6.4, 0.0))
        # The left edge came from outside the frame entirely.
        self.assertTrue(np.all(moved[:, :5] == 0.0))
        # And what remains is not dimmed at the seam.
        self.assertGreater(float(moved[:, 20:40].mean()),
                           float(held[:, 14:34].mean()) * 0.9)

    def test_a_turn_is_resampled_too(self):
        held = self.starry()
        turned = sk._lanczos_warp(held, 3.0, (0.0, 0.0))
        soft = sk._warped(held, 3.0, (0.0, 0.0))
        inner = (slice(30, -30), slice(30, -30))
        self.assertGreater(float(turned[inner].max()),
                           float(soft[inner].max()))
        # And a turn of nothing changes nothing.
        still = sk._lanczos_warp(held, 0.0, (0.0, 0.0))
        self.assertTrue(np.allclose(still[inner], held[inner], atol=1e-4))

    def test_the_kernel_does_not_ring_a_star_into_a_dark_halo(self):
        held = self.starry(count=8)
        moved = sk._lanczos_shift(held, (0.5, 0.5))
        inner = (slice(12, -12), slice(12, -12))
        # Nothing may dip meaningfully below the sky it sits on.
        self.assertGreater(float(moved[inner].min()), 0.02)


class FrameWeightTests(unittest.TestCase):
    """Frames are not all worth the same, and the noise says so."""

    def weights(self, noises):
        return sk._frame_weights([{"noise": n} for n in noises])

    def test_equal_frames_weigh_equally(self):
        self.assertTrue(np.allclose(self.weights([0.01] * 4), 1.0))

    def test_a_grainier_frame_counts_by_the_square(self):
        weights = self.weights([0.01, 0.01, 0.02])
        # Twice the noise is a quarter the weight, not half.
        self.assertAlmostEqual(float(weights[2] / weights[0]), 0.25, places=3)

    def test_no_frame_may_run_away_with_the_stack(self):
        weights = self.weights([0.01, 0.01, 0.01, 1e-9])
        self.assertLessEqual(float(weights.max() / weights[0]), 4.001)

    def test_frames_that_said_nothing_weigh_the_same(self):
        self.assertTrue(np.allclose(self.weights([0.0] * 4), 1.0))
        # One silent frame and the whole set falls back to plain.
        self.assertTrue(np.allclose(self.weights([0.01, 0.0, 0.01]), 1.0))

    def test_weighting_is_quieter_than_averaging_when_frames_differ(self):
        rng = np.random.default_rng(7)
        noises = np.array([0.008, 0.010, 0.014, 0.030])
        weights = self.weights(list(noises))
        plain, weighted = [], []
        for _ in range(1500):
            seen = 0.5 + rng.normal(0, noises)
            plain.append(seen.mean())
            weighted.append(float((seen * weights).sum() / weights.sum()))
        self.assertLess(float(np.std(weighted)),
                        float(np.std(plain)) * 0.85)

    def test_registering_writes_down_how_grainy_each_frame_was(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            turning_night(root, [0.0, 0.0, 0.0])
            told = json.loads(sk.register(folder, "*.jpg", demosaic=False))
        for item in told["frames"]:
            self.assertIn("noise", item)
            self.assertGreater(float(item["noise"]), 0.0)



if __name__ == "__main__":
    unittest.main()
