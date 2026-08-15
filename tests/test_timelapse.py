"""The handheld eclipse sequence, pinned for a timelapse.

The photographer's algorithm with its two agreed amendments: a robust
limb fit instead of extreme-point bookkeeping, and margins taken off
centres smoothed over time. The crop invariants are the tests' core:
every frame contains its box, every box is the same even size, and the
sun sits at the same offset in all of them.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import timelapse_kernel as timelapse  # noqa: E402


def crescent(path: Path, centre=(450, 300), radius=90, bite=0.55,
             size=(900, 600), glow=28.0) -> None:
    """A partial eclipse: bright disc, moon bitten, veiled a little."""
    ys, xs = np.mgrid[0:size[1], 0:size[0]].astype(np.float64)
    sun = np.hypot(xs - centre[0], ys - centre[1]) <= radius
    moon = np.hypot(xs - (centre[0] + radius * bite),
                    ys - (centre[1] - radius * bite)) <= radius
    disc = sun & ~moon
    frame = np.full(size[::-1], 6.0)
    frame += glow * np.exp(-(np.hypot(xs - centre[0], ys - centre[1])
                             / (radius * 2.2)) ** 2)
    frame[disc] = 250.0
    pixels = np.clip(frame, 0, 255).astype(np.uint8)
    Image.fromarray(pixels).convert("RGB").save(path, quality=95)


class LimbFitTests(unittest.TestCase):
    """The sun found through the bite, exactly."""

    def grey(self, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "frame.jpg"
            crescent(path, **kwargs)
            return np.asarray(Image.open(path).convert("L"),
                              dtype=np.float32)

    def test_the_fit_lands_on_the_solar_disc_not_the_crescent_blob(self):
        fitted = timelapse.fit_limb(self.grey())
        self.assertIsNotNone(fitted)
        self.assertAlmostEqual(fitted["cx"], 450, delta=4)
        self.assertAlmostEqual(fitted["cy"], 300, delta=4)
        self.assertAlmostEqual(fitted["r"], 90, delta=4)

    def test_a_deeper_bite_does_not_move_the_centre(self):
        shallow = timelapse.fit_limb(self.grey(bite=0.35))
        deep = timelapse.fit_limb(self.grey(bite=0.8))
        self.assertAlmostEqual(shallow["cx"], deep["cx"], delta=5)
        self.assertAlmostEqual(shallow["r"], deep["r"], delta=5)

    def test_a_thin_crescent_binds_to_the_sun_not_the_moon(self):
        """The two arcs are near-equal in length and radius on a thin
        crescent -- the moon matches the sun's size, that is what an
        eclipse is -- and inlier count alone elected the moon on the
        real series: the crescent wandered by the bite's direction.
        Containment cannot confuse them."""
        for bite in (0.9, 1.05):
            fitted = timelapse.fit_limb(self.grey(bite=bite))
            self.assertIsNotNone(fitted, f"bite {bite}")
            self.assertAlmostEqual(fitted["cx"], 450, delta=6)
            self.assertAlmostEqual(fitted["cy"], 300, delta=6)
            self.assertAlmostEqual(fitted["r"], 90, delta=6)

    def test_a_frame_with_no_sun_says_so(self):
        dark = np.full((300, 400), 5.0, dtype=np.float32)
        self.assertIsNone(timelapse.fit_limb(dark))

    def test_the_fit_is_deterministic(self):
        grey = self.grey()
        first = timelapse.fit_limb(grey)
        second = timelapse.fit_limb(grey)
        self.assertEqual(first, second)


class PlanTests(unittest.TestCase):
    """The largest common crop, and its invariants."""

    def sequence(self, root: Path, centres, radius=90, size=(900, 600)):
        for index, centre in enumerate(centres):
            crescent(root / f"frame-{index:03d}.jpg", centre=centre,
                     radius=radius, size=size)
        return timelapse.survey(str(root), "frame-*.jpg")

    def test_every_crop_is_identical_even_sized_and_contained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            centres = [(430, 300), (470, 290), (450, 320), (445, 305)]
            plan = json.loads(timelapse.crop_plan(
                self.sequence(root, centres)))
            self.assertNotIn("error", plan)
            self.assertEqual(plan["width"] % 2, 0)
            self.assertEqual(plan["height"] % 2, 0)
            for box in plan["boxes"]:
                self.assertGreaterEqual(box["left"], 0)
                self.assertGreaterEqual(box["top"], 0)
                self.assertLessEqual(box["left"] + plan["width"], 900)
                self.assertLessEqual(box["top"] + plan["height"], 600)

    def test_the_sun_sits_at_the_same_offset_in_every_crop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            centres = [(430, 300), (470, 290), (450, 320)]
            told = json.loads(self.sequence(root, centres))
            plan = json.loads(timelapse.crop_plan(json.dumps(told)))
            offsets = set()
            fitted = {f["name"]: f for f in told["frames"]}
            for box in plan["boxes"]:
                frame = fitted[box["name"]]
                offsets.add((round(frame["box"][0] - box["left"]),
                             round(frame["box"][1] - box["top"])))
            # One offset for the whole sequence, within a pixel.
            self.assertLessEqual(len(offsets), 2)

    def test_the_right_margin_carries_all_three_terms(self):
        """The step-11 fix: the crop must never cut into the subject."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = json.loads(timelapse.crop_plan(self.sequence(
                root, [(200, 300), (700, 300)])))
            self.assertNotIn("error", plan)
            self.assertGreaterEqual(
                plan["width"],
                plan["margins"]["left"] + plan["span"]["width"]
                + plan["margins"]["right"] - 2)

    def test_a_varying_subject_extent_is_contained_by_construction(self):
        """The refactor's own reason: the circle version measured the
        right margin to each frame's own edge, so a frame whose subject
        was smaller than the sequence's largest could see the crop slip
        past the frame boundary and get clamped -- moving the subject
        off its pinned offset. Boxes size the margins against the
        largest span, so every crop contains every subject unclamped."""
        survey = {"format": timelapse.FORMAT, "photos": "/p",
                  "pattern": "*", "kept": 2, "excluded": [],
                  "frames": [
                      {"name": "big.jpg", "taken": "1", "width": 900,
                       "height": 600, "box": [300.0, 200.0, 500.0, 400.0]},
                      {"name": "small.jpg", "taken": "2", "width": 900,
                       "height": 600, "box": [700.0, 200.0, 790.0, 290.0]},
                  ]}
        # Spans differ 200 vs 90 -- more than the scale screen allows,
        # so feed the plan directly: crop_plan trusts its survey.
        plan = json.loads(timelapse.crop_plan(json.dumps(survey)))
        self.assertNotIn("error", plan)
        for box, frame in zip(plan["boxes"], survey["frames"]):
            self.assertGreaterEqual(box["left"], 0)
            self.assertLessEqual(box["left"] + plan["width"],
                                 frame["width"])
            # And the subject's pinned offset survived: no clamp moved it.
            self.assertEqual(
                box["left"],
                int(round(frame["box"][0] - plan["margins"]["left"])))

    def test_a_different_lens_is_set_aside_by_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, centre in enumerate([(430, 300), (460, 310)]):
                crescent(root / f"frame-{index:03d}.jpg", centre=centre)
            crescent(root / "frame-999.jpg", radius=30)   # telephoto odd one
            plan = json.loads(timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg")))
            names = [item["name"] for item in plan["excluded"]]
            self.assertEqual(names, ["frame-999.jpg"])

    def test_a_sun_touching_the_edge_is_excluded_before_it_can_lie(self):
        """A clipped disc does not fail loudly -- it fits a wrong circle
        with a straight face (measured: a sun at x=60 fitted to x=113).
        So the border test runs on the bright mask, before any fit is
        believed, and the frame is set aside by name."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crescent(root / "frame-000.jpg", centre=(450, 300))
            crescent(root / "frame-001.jpg", centre=(60, 300))
            told = json.loads(timelapse.survey(str(root), "frame-*.jpg"))
            names = {item["name"]: item["why"] for item in told["excluded"]}
            self.assertIn("frame-001.jpg", names)
            self.assertIn("touches the frame edge", names["frame-001.jpg"])
            plan = json.loads(timelapse.crop_plan(json.dumps(told)))
            self.assertNotIn("error", plan)

    def test_negative_margins_still_refuse_the_plan_outright(self):
        """The belt behind the braces: a survey that somehow carries a
        fit past the frame edge is refused, not clamped."""
        survey = {"format": timelapse.FORMAT, "photos": "/p",
                  "pattern": "*", "kept": 1, "excluded": [],
                  "frames": [{"name": "a.jpg", "taken": "t",
                              "width": 900, "height": 600,
                              "box": [-40.0, 210.0, 140.0, 390.0]}]}
        plan = json.loads(timelapse.crop_plan(json.dumps(survey)))
        self.assertIn("error", plan)
        self.assertIn("a.jpg", plan["error"])
        self.assertFalse(timelapse.plan_valid(json.dumps(plan)))


class SequenceTests(unittest.TestCase):
    """From plan to numbered frames, colour-shifted the house way."""

    def test_frames_land_numbered_sized_and_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, centre in enumerate([(430, 300), (470, 300)]):
                crescent(root / f"frame-{index:03d}.jpg", centre=centre)
            plan = timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg"))
            report = timelapse.render_sequence(
                str(root), plan, str(root / "out"))
            told = json.loads(report)
            self.assertEqual([item["frame"] for item in told["frames"]],
                             ["0001.jpg", "0002.jpg"])
            self.assertTrue(timelapse.sequence_complete(report, plan))
            self.assertIn("ffmpeg", told["assemble"])

    def test_the_colour_shift_is_applied_and_masks_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crescent(root / "frame-000.jpg")
            crescent(root / "frame-001.jpg", centre=(460, 310))
            recipe = root / "shift.recipe.json"
            recipe.write_text(json.dumps({
                "format": "darkimiya-portable-recipe-v1",
                "recipe": {"operations": [
                    {"op": "tone.exposure", "value": -1.0, "unit": "EV",
                     "mode": "delta"},
                    {"op": "mask.radial", "unit": "mask", "mode": "absolute",
                     "value": {"anchor": "on the sun", "effects": []}},
                ]}}))
            plan = timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg"))
            plain = json.loads(timelapse.render_sequence(
                str(root), plan, str(root / "plain")))
            shifted = json.loads(timelapse.render_sequence(
                str(root), plan, str(root / "shifted"),
                recipe=str(recipe)))
            before = np.asarray(Image.open(
                Path(plain["directory"]) / "0001.jpg").convert("L"))
            after = np.asarray(Image.open(
                Path(shifted["directory"]) / "0001.jpg").convert("L"))
            self.assertLess(float(after.mean()), float(before.mean()) - 5)

    def test_a_missing_frame_fails_completeness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crescent(root / "frame-000.jpg")
            crescent(root / "frame-001.jpg", centre=(460, 310))
            plan = timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg"))
            report = timelapse.render_sequence(
                str(root), plan, str(root / "out"))
            (root / "out" / "0002.jpg").unlink()
            self.assertFalse(timelapse.sequence_complete(report, plan))


if __name__ == "__main__":
    unittest.main()
