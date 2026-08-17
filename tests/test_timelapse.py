"""The handheld eclipse sequence, pinned for a timelapse.

The photographer's algorithm with its two agreed amendments: a robust
limb fit instead of extreme-point bookkeeping, and margins taken off
centres smoothed over time. The crop invariants are the tests' core:
every frame contains its box, every box is the same even size, and the
sun sits at the same offset in all of them.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        # Spans differ 200 vs 90; scale-normalisation folds them into
        # one scale rather than clamping. The crop still contains every
        # subject, and the offset is pinned in NORMALISED pixels.
        plan = json.loads(timelapse.crop_plan(json.dumps(survey)))
        self.assertNotIn("error", plan)
        for box in plan["boxes"]:
            self.assertGreaterEqual(box["left"], 0)
            self.assertLessEqual(box["left"] + plan["width"],
                                 survey["frames"][0]["width"] * box["scale"] + 1)
            self.assertEqual(
                box["left"],
                int(round(survey["frames"][
                    [b["name"] for b in plan["boxes"]].index(box["name"])
                    ]["box"][0] * box["scale"] - plan["margins"]["left"])))

    def test_a_zoomed_run_is_normalised_not_excluded(self):
        """A zoom is not a failed frame. Several frames agreeing on a
        new scale are folded in, each carrying the factor that maps its
        own sun to the sequence's; a lone wild fit is still excluded."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Eight normal frames, then a run of five zoomed to half the
            # sun's size -- a real lens change, not a spike.
            for index in range(8):
                crescent(root / f"frame-{index:03d}.jpg",
                         centre=(430 + index, 300), radius=90)
            for index in range(8, 13):
                crescent(root / f"frame-{index:03d}.jpg",
                         centre=(300, 250), radius=45)
            plan = json.loads(timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg")))
            self.assertEqual(plan["excluded"], [])
            scales = {b["name"]: b["scale"] for b in plan["boxes"]}
            # The zoomed frames carry ~2x scale; the normal ones ~1.
            self.assertAlmostEqual(scales["frame-010.jpg"], 2.0, delta=0.15)
            self.assertAlmostEqual(scales["frame-003.jpg"], 1.0, delta=0.1)

    def test_a_lone_wild_fit_is_still_excluded_as_a_failed_find(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(8):
                crescent(root / f"frame-{index:03d}.jpg",
                         centre=(430, 300), radius=90)
            # One frame, alone, at a wildly different size: a botched fit.
            crescent(root / "frame-050.jpg", centre=(430, 300), radius=20)
            told = json.loads(timelapse.survey(str(root), "frame-*.jpg"))
            names = [item["name"] for item in told["excluded"]]
            self.assertIn("frame-050.jpg", names)

    def test_a_short_zoom_burst_of_two_is_admitted_as_a_run(self):
        """A strong zoom lasting only two frames cannot swing a
        window-5 median onto its scale, so each frame reads as a lone
        spike. What saves it is that the two agree with each OTHER: a
        suspect flanked by a suspect of nearly its own extent is a
        zoom, not a botched fit, and is kept and normalised."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Normals on both sides so the median stays put; a two-frame
            # burst at double the sun's radius in the middle.
            for index in list(range(5)) + list(range(7, 12)):
                crescent(root / f"frame-{index:03d}.jpg",
                         centre=(430, 300), radius=90)
            crescent(root / "frame-005.jpg", centre=(400, 300), radius=180)
            crescent(root / "frame-006.jpg", centre=(400, 300), radius=180)
            told = json.loads(timelapse.survey(str(root), "frame-*.jpg"))
            excluded = {item["name"] for item in told["excluded"]}
            self.assertNotIn("frame-005.jpg", excluded)
            self.assertNotIn("frame-006.jpg", excluded)

    def test_two_adjacent_spikes_that_disagree_are_not_a_run(self):
        """Adjacency alone is not a zoom. Two neighbouring wild fits
        that land at DIFFERENT wrong sizes agree with the run around
        them on nothing -- not the smoothed track, not each other -- so
        both are still excluded as failed finds."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in list(range(5)) + list(range(7, 12)):
                crescent(root / f"frame-{index:03d}.jpg",
                         centre=(430, 300), radius=90)
            crescent(root / "frame-005.jpg", centre=(400, 300), radius=150)
            crescent(root / "frame-006.jpg", centre=(400, 300), radius=200)
            told = json.loads(timelapse.survey(str(root), "frame-*.jpg"))
            excluded = {item["name"] for item in told["excluded"]}
            self.assertIn("frame-005.jpg", excluded)
            self.assertIn("frame-006.jpg", excluded)

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


class ProgressMarkerTests(unittest.TestCase):
    """The long run narrates itself, so the queue can draw a bar."""

    def test_survey_and_render_print_progress_across_the_whole(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, centre in enumerate([(430, 300), (470, 300),
                                            (450, 320)]):
                crescent(root / f"frame-{index:03d}.jpg", centre=centre)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                plan = timelapse.crop_plan(
                    timelapse.survey(str(root), "frame-*.jpg"))
                timelapse.render_sequence(str(root), plan, str(root / "out"))
            markers = [line for line in out.getvalue().splitlines()
                       if line.startswith("TIMELAPSE_PROGRESS")]
            percents = [int(line.split()[1]) for line in markers]
            self.assertTrue(markers)
            # Aligning sits in the first band, rendering in the second, and
            # the percentages only advance -- one bar across both phases.
            self.assertTrue(any("aligned" in m for m in markers))
            self.assertTrue(any("rendered" in m for m in markers))
            self.assertEqual(percents, sorted(percents))
            self.assertLessEqual(max(percents), 90)

    def test_the_queue_reads_the_marker_into_a_fraction(self):
        from opencull_gui.jobs import JobManager

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "run.log"
            log.write_text(
                "some noise\n"
                "TIMELAPSE_PROGRESS 45 aligning frame 130 of 289\n"
                "TIMELAPSE_PROGRESS 70 rendering frame 40 of 260\n")
            # The output from an EARLIER run is still on disk -- the
            # timelapse writes to the same folder every time -- and must
            # not read as this run being done.
            (root / "report.json").write_text("{}")
            job = {"kind": "kimiya_program", "log": str(log),
                   "status": "running",
                   "output": str(root / "report.json"),
                   "checkpoint": str(root / "cp.json")}
            progress = JobManager._progress(job)
            self.assertAlmostEqual(progress["fraction"], 0.70)
            self.assertIn("rendering", progress["stage"])

    def test_a_rerun_ignores_the_previous_runs_markers(self):
        # The supervisor appends run after run to one log. The moment a
        # rerun starts, the tail still ends with last run's markers; only
        # what follows this run's own opening line counts, so a fresh run
        # opens at nothing rather than at 92%.
        from opencull_gui.jobs import JobManager

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "run.log"
            log.write_text(
                "starting supervised command\n"
                "TIMELAPSE_PROGRESS 92 encoding the video\n"
                "COMMITTED\n"
                "starting supervised command\n")
            job = {"kind": "kimiya_program", "log": str(log),
                   "status": "running",
                   "output": str(root / "report.json"),
                   "checkpoint": str(root / "cp.json")}
            progress = JobManager._progress(job)
            self.assertNotEqual(progress.get("fraction"), 0.92)
            # And once THIS run speaks, its own markers count.
            with log.open("a") as handle:
                handle.write("TIMELAPSE_PROGRESS 18 aligning frame 5 of 300\n")
            self.assertAlmostEqual(
                JobManager._progress(job)["fraction"], 0.18)

    def test_a_queued_rerun_claims_no_progress_at_all(self):
        from opencull_gui.jobs import JobManager

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "run.log"
            log.write_text(
                "starting supervised command\n"
                "TIMELAPSE_PROGRESS 92 encoding the video\n")
            job = {"kind": "kimiya_program", "log": str(log),
                   "status": "queued",
                   "output": str(root / "report.json"),
                   "checkpoint": str(root / "cp.json")}
            progress = JobManager._progress(job)
            self.assertEqual(progress["fraction"], 0.0)
            self.assertEqual(progress["stage"], "")


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

    def test_ultimate_quality_develops_each_frame_once(self):
        """With demosaic on, every planned frame is developed from the
        raw with the look applied in float -- and the crop stage must not
        apply the operations a second time."""
        with mock.patch.dict(
                "os.environ", {"DARKIMIYA_TIMELAPSE_WORKERS": "1"}):
            self._ultimate_quality_develops_each_frame_once()

    def _ultimate_quality_develops_each_frame_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, centre in enumerate([(430, 300), (470, 300)]):
                crescent(root / f"frame-{index:03d}.jpg", centre=centre)
            plan = timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg"))
            ops = [{"op": "tone.exposure", "value": -1.0, "unit": "EV",
                    "mode": "delta"}]
            recipe = root / "shift.recipe.json"
            recipe.write_text(json.dumps({
                "format": "darkimiya-portable-recipe-v1",
                "recipe": {"operations": ops}}))
            asked = []

            def fake_developed(path, operations):
                asked.append((Path(path).name, len(operations or [])))
                return Image.new("RGB", crescent_size(), (200, 200, 200))

            def crescent_size():
                with Image.open(root / "frame-000.jpg") as opened:
                    return opened.size

            with mock.patch.object(
                    timelapse, "_developed", side_effect=fake_developed):
                report = json.loads(timelapse.render_sequence(
                    str(root), plan, str(root / "out"),
                    recipe=str(recipe), demosaic=True))
            self.assertEqual(len(asked), 2)          # one develop per frame
            self.assertEqual(asked[0][1], 1)         # the look went in
            self.assertEqual(report["base"], "demosaic")
            # The fake returned flat grey WITH the look "already applied";
            # if the crop stage applied it again, the frame would darken.
            first = np.asarray(Image.open(
                Path(report["directory"]) / "0001.jpg").convert("L"))
            self.assertGreater(float(first.mean()), 180)

    def test_without_the_switch_nothing_is_demosaiced(self):
        with mock.patch.dict(
                "os.environ", {"DARKIMIYA_TIMELAPSE_WORKERS": "1"}), \
                tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crescent(root / "frame-000.jpg")
            crescent(root / "frame-001.jpg", centre=(460, 310))
            plan = timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg"))
            with mock.patch.object(timelapse, "_developed") as developed:
                report = json.loads(timelapse.render_sequence(
                    str(root), plan, str(root / "out")))
            developed.assert_not_called()
            self.assertEqual(report["base"], "embedded rendering")

    def test_parallel_and_serial_render_the_same_film(self):
        """The pool is a speed choice, never a picture choice."""
        import hashlib

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, centre in enumerate([(430, 300), (470, 300),
                                            (450, 320)]):
                crescent(root / f"frame-{index:03d}.jpg", centre=centre)
            plan = timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg"))
            with mock.patch.dict(
                    "os.environ", {"DARKIMIYA_TIMELAPSE_WORKERS": "1"}):
                timelapse.render_sequence(str(root), plan,
                                          str(root / "serial"))
            with mock.patch.dict(
                    "os.environ", {"DARKIMIYA_TIMELAPSE_WORKERS": "3"}):
                timelapse.render_sequence(str(root), plan,
                                          str(root / "pooled"))

            def digest(folder: Path) -> list[tuple[str, str]]:
                return [(p.name,
                         hashlib.sha256(p.read_bytes()).hexdigest())
                        for p in sorted(folder.glob("*.jpg"))]

            self.assertEqual(digest(root / "serial"),
                             digest(root / "pooled"))

    def test_a_previous_runs_tail_is_cleared_before_rendering(self):
        """The frames folder is reused run after run. A shorter run must
        not leave the last film's tail beyond its own -- stale frames
        that read as "the look was not applied" and that ffmpeg's
        numbered pattern would splice into the end of the video."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crescent(root / "frame-000.jpg")
            crescent(root / "frame-001.jpg", centre=(460, 310))
            out = root / "out"
            out.mkdir()
            # The previous, longer run's leftovers.
            for number in (1, 2, 3, 9):
                (out / f"{number:04d}.jpg").write_bytes(b"stale")
            (out / "anchors").mkdir()
            plan = timelapse.crop_plan(
                timelapse.survey(str(root), "frame-*.jpg"))
            report = json.loads(timelapse.render_sequence(
                str(root), plan, str(out)))
            numbered = sorted(p.name for p in out.glob("*.jpg"))
            self.assertEqual(numbered, ["0001.jpg", "0002.jpg"])
            self.assertEqual(len(report["frames"]), 2)

    def test_the_model_proofs_live_beside_the_frames_not_among_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crescent(root / "frame-000.jpg")
            proof = timelapse.keyframe_proof(
                str(root), "frame-000.jpg", str(root / "frames"))
            self.assertEqual(Path(proof).parent.name, "anchors")
            self.assertEqual(Path(proof).parent.parent.name, "frames")

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


class AssembleVideoTests(unittest.TestCase):
    """The last step the button promises: the frames become a film."""

    def _rendered(self, root: Path) -> str:
        for index, centre in enumerate([(430, 300), (470, 300), (450, 320)]):
            crescent(root / f"frame-{index:03d}.jpg", centre=centre)
        plan = timelapse.crop_plan(
            timelapse.survey(str(root), "frame-*.jpg"))
        # The house layout: frames in a `frames/` folder, film beside it.
        return timelapse.render_sequence(
            str(root), plan, str(root / "Timelapse" / "frames"))

    def test_the_film_lands_beside_the_frames_not_among_them(self):
        self.assertTrue(timelapse._video_target(
            Path("/a/Timelapse/frames")).as_posix().endswith(
            "/a/Timelapse/timelapse.mp4"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_the_video_is_encoded_and_its_path_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            film = json.loads(timelapse.assemble_video(self._rendered(root)))
            made = film.get("video")
            self.assertTrue(made, film.get("video_note"))
            self.assertTrue(Path(made).is_file())
            self.assertGreater(Path(made).stat().st_size, 0)
            # Beside the frames, in the timelapse folder itself.
            self.assertEqual(Path(made).parent.name, "Timelapse")

    def test_a_missing_ffmpeg_keeps_the_frames_and_the_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = self._rendered(root)
            with mock.patch("timelapse_kernel.shutil.which",
                            return_value=None):
                film = json.loads(timelapse.assemble_video(report))
            # No film, but the run is not failed: the frames are on disk and
            # the assemble line is there for when ffmpeg is installed.
            self.assertIsNone(film["video"])
            self.assertIn("ffmpeg", film["video_note"])
            self.assertIn("ffmpeg", film["assemble"])
            self.assertTrue(timelapse.sequence_complete(
                report, timelapse.crop_plan(
                    timelapse.survey(str(root), "frame-*.jpg"))))

    def test_no_frames_makes_no_film_and_does_not_raise(self):
        film = json.loads(timelapse.assemble_video(json.dumps(
            {"directory": "/nowhere/frames", "frames": []})))
        self.assertIsNone(film["video"])
        self.assertIn("no frames", film["video_note"])

    def test_the_asked_speed_reaches_the_encoder_and_the_note(self):
        # Kimiya hands a num over as a float; it must land in the ffmpeg
        # line and the note as the framerate, bounded to the playable.
        told = timelapse._assemble_command(
            Path("/f"), Path("/f/t.mp4"), timelapse._fps_of(6.0))
        self.assertIn("-framerate 6", told)
        self.assertEqual(timelapse._fps_of(0), 12)      # unset -> default
        self.assertEqual(timelapse._fps_of("nope"), 12)
        self.assertEqual(timelapse._fps_of(500), 60)    # bounded
        self.assertEqual(timelapse._fps_of(-3), 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            film = json.loads(timelapse.assemble_video(
                self._rendered(root), fps=6.0))
            if film.get("video"):                       # ffmpeg installed
                self.assertIn("6 fps", film["video_note"])
                self.assertIn("-framerate 6", film["assemble"])


class TrackTests(unittest.TestCase):
    """Mark it once, track it free -- and never trust a lost match.

    The moving subject is a textured patch on a textured ground, so
    correlation has something honest to grip; a blank square on black
    matches itself everywhere.
    """

    def sequence(self, root: Path, positions, size=(640, 420),
                 missing=()):
        rng = np.random.default_rng(7)
        ground = rng.integers(20, 70, size=(size[1], size[0]),
                              dtype=np.uint8)
        stamp = rng.integers(120, 250, size=(48, 48), dtype=np.uint8)
        for index, position in enumerate(positions):
            frame = ground.copy()
            if index not in missing:
                x, y = position
                frame[y:y + 48, x:x + 48] = stamp
            Image.fromarray(frame).convert("RGB").save(
                root / f"frame-{index:03d}.jpg", quality=95)
        first_x, first_y = positions[0]
        return f"{first_x},{first_y},{first_x + 48},{first_y + 48}"

    def test_a_selection_file_confines_the_run_to_the_kept_frames(self):
        """The cull already said which frames matter. Given a selection
        file, every measurer's listing holds only those names -- the
        whole folder is not the default aim of a culled project."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100), (120, 100), (140, 104),
                                 (160, 110)])
            chosen = root / "selection.json"
            chosen.write_text(json.dumps(
                {"names": ["frame-001.jpg", "frame-003.jpg"]}))
            listed = timelapse.list_frames(
                str(root), "frame-*.jpg", only=str(chosen))
            self.assertEqual([item["name"] for item in listed],
                             ["frame-001.jpg", "frame-003.jpg"])
            # And the box tracker sees the same confined world.
            told = json.loads(timelapse.track_survey(
                str(root), "frame-*.jpg", "120,100,168,148",
                only=str(chosen)))
            self.assertEqual(len(told["frames"]), 2)

    def test_an_empty_only_means_the_whole_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100), (130, 100)])
            listed = timelapse.list_frames(str(root), "frame-*.jpg", only="")
            self.assertEqual(len(listed), 2)

    def test_listing_frames_never_decodes_a_preview(self):
        """Listing is a read of names and clocks, not of pictures. An
        earlier version decoded every frame's embedded rendering just to
        spell the date -- half a minute before any work began."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100), (130, 100)])
            with mock.patch.object(
                    timelapse, "_preview",
                    side_effect=AssertionError("decoded a preview")):
                listed = timelapse.list_frames(str(root), "frame-*.jpg")
            self.assertEqual(len(listed), 2)
            self.assertTrue(all(item["taken"] for item in listed))

    def test_tracking_starts_at_the_marked_frame_not_the_first(self):
        """The first shots of a sequence often are not the subject yet.
        The box belongs to the frame it was drawn on: everything before
        is set aside by name, and the template is cut where the mark
        was made rather than from background."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Frames 0-1 have no subject at all; it appears at frame 2.
            positions = [(0, 0), (0, 0), (200, 150), (240, 160), (270, 180)]
            self.sequence(root, positions, missing={0, 1})
            told = json.loads(timelapse.track_survey(
                str(root), "frame-*.jpg", "200,150,248,198",
                subject_frame="frame-002.jpg"))
            excluded = {item["name"]: item["why"]
                        for item in told["excluded"]}
            self.assertIn("frame-000.jpg", excluded)
            self.assertIn("frame-001.jpg", excluded)
            self.assertIn("before the marked frame", excluded["frame-000.jpg"])
            self.assertEqual(told["kept"], 3)
            kept = [f for f in told["frames"] if "excluded" not in f]
            self.assertEqual(kept[0]["name"], "frame-002.jpg")
            self.assertAlmostEqual(kept[-1]["box"][0], 270, delta=3)

    def test_an_unknown_marked_frame_refuses_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100), (130, 100)])
            with self.assertRaisesRegex(ValueError, "not among the frames"):
                timelapse.track_survey(
                    str(root), "frame-*.jpg", "100,100,148,148",
                    subject_frame="frame-099.jpg")

    def test_the_tracker_follows_the_subject_within_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            positions = [(100, 100), (130, 96), (170, 118), (210, 140)]
            seed = self.sequence(root, positions)
            told = json.loads(timelapse.track_survey(
                str(root), "frame-*.jpg", seed))
            self.assertEqual(told["kept"], 4)
            for frame, (x, y) in zip(told["frames"], positions):
                self.assertAlmostEqual(frame["box"][0], x, delta=3)
                self.assertAlmostEqual(frame["box"][1], y, delta=3)

    def test_a_vanished_subject_is_set_aside_and_reacquired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            positions = [(100, 100), (130, 100), (150, 104), (165, 110)]
            seed = self.sequence(root, positions, missing={2})
            told = json.loads(timelapse.track_survey(
                str(root), "frame-*.jpg", seed))
            lost = {item["name"]: item["why"] for item in told["excluded"]}
            self.assertIn("frame-002.jpg", lost)
            self.assertIn("lost the subject", lost["frame-002.jpg"])
            found = {f["name"]: f for f in told["frames"]
                     if "excluded" not in f}
            self.assertIn("frame-003.jpg", found)
            self.assertAlmostEqual(
                found["frame-003.jpg"]["box"][0], 165, delta=4)

    def test_the_template_is_cut_once_and_never_updated(self):
        """A template that follows its matches drifts onto whatever it
        matched. The seed box must survive to the last frame."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            positions = [(80 + 12 * i, 90 + 6 * i) for i in range(10)]
            seed = self.sequence(root, positions)
            told = json.loads(timelapse.track_survey(
                str(root), "frame-*.jpg", seed))
            last = [f for f in told["frames"] if "excluded" not in f][-1]
            self.assertAlmostEqual(last["box"][0], positions[-1][0],
                                   delta=3)

    def test_a_degenerate_seed_box_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100)])
            with self.assertRaises(ValueError):
                timelapse.track_survey(str(root), "frame-*.jpg",
                                       "50,50,50,90")

    def test_the_tracked_survey_feeds_the_same_plan_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            positions = [(100, 100), (140, 110), (180, 120)]
            seed = self.sequence(root, positions)
            plan = json.loads(timelapse.crop_plan(timelapse.track_survey(
                str(root), "frame-*.jpg", seed)))
            self.assertNotIn("error", plan)
            self.assertEqual(plan["width"] % 2, 0)
            for box in plan["boxes"]:
                self.assertGreaterEqual(box["left"], 0)


class ShiftNameTests(unittest.TestCase):
    """The colour shift, named the way a person names it.

    A JSON path is for a custom look; a preset answers to its id, its
    id without the "preset-" prefix, or its human name, case blind.
    An unknown name refuses with the working names listed, because a
    shift silently skipped is a timelapse quietly wrong.
    """

    def test_a_presets_short_id_is_enough(self):
        told = timelapse._shift_operations("infrared-720-false-colour")
        self.assertTrue(told)
        self.assertIn("color.neutralize", [item["op"] for item in told])

    def test_the_human_name_works_case_blind(self):
        told = timelapse._shift_operations("infrared · 720nm false colour")
        self.assertTrue(told)

    def test_a_relative_path_resolves_beside_the_photos(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "my-look.recipe.json").write_text(json.dumps({
                "recipe": {"operations": [
                    {"op": "tone.exposure", "value": -0.5, "unit": "EV",
                     "mode": "delta"}]}}))
            told = timelapse._shift_operations(
                "my-look.recipe.json", photos=str(root))
            self.assertEqual(told[0]["op"], "tone.exposure")

    def test_an_unknown_name_refuses_and_lists_what_would_work(self):
        with self.assertRaises(ValueError) as caught:
            timelapse._shift_operations("ir-false-colour")
        said = str(caught.exception)
        self.assertIn("infrared-720-false-colour", said)

    def test_masks_are_still_skipped_whichever_spelling(self):
        told = timelapse._shift_operations("infrared-760-blue-violet")
        self.assertTrue(told)
        self.assertFalse(
            [item for item in told
             if str(item["op"]).startswith("mask.")])


class AnchoredTests(unittest.TestCase):
    """A model anchors sparsely; the tracker carries between.

    The cost shape is the point: a handful of vision calls stabilizes
    hundreds of frames, and every model answer is validated, scaled and
    -- where unusable -- recorded with its reason instead of trusted.
    """

    def sequence(self, root: Path, positions, size=(640, 420)):
        rng = np.random.default_rng(11)
        ground = rng.integers(20, 70, size=(size[1], size[0]),
                              dtype=np.uint8)
        stamp = rng.integers(120, 250, size=(48, 48), dtype=np.uint8)
        for index, (x, y) in enumerate(positions):
            frame = ground.copy()
            frame[y:y + 48, x:x + 48] = stamp
            Image.fromarray(frame).convert("RGB").save(
                root / f"frame-{index:03d}.jpg", quality=95)

    def test_keyframes_are_first_last_and_every_nth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100 + 6 * i, 100) for i in range(9)])
            chosen = timelapse.keyframes(
                str(root), "frame-*.jpg", every=4)
            self.assertEqual(chosen, ["frame-000.jpg", "frame-004.jpg",
                                      "frame-008.jpg"])

    def test_an_anchor_is_scaled_from_the_proofs_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100)])
            proof = timelapse.keyframe_proof(
                str(root), "frame-000.jpg", str(root / "anchors"),
                edge=320)   # proof at half size: scale = 2
            held = json.loads(timelapse.collect_anchor(
                "[]", str(root), "frame-000.jpg", proof,
                {"x0": 50, "y0": 50, "x1": 74, "y1": 74,
                 "visible": True, "what": "the stamp"}))
            self.assertEqual(held[0]["box"], [100.0, 100.0, 148.0, 148.0])

    def test_a_useless_answer_records_why_instead_of_pretending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100)])
            proof = timelapse.keyframe_proof(
                str(root), "frame-000.jpg", str(root / "anchors"))
            held = json.loads(timelapse.collect_anchor(
                "[]", str(root), "frame-000.jpg", proof,
                {"x0": 90, "y0": 90, "x1": 20, "y1": 120,
                 "visible": True, "what": "?"}))
            self.assertIn("unusable box", held[0]["skipped"])
            hidden = json.loads(timelapse.collect_anchor(
                "[]", str(root), "frame-000.jpg", proof,
                {"x0": 0, "y0": 0, "x1": 0, "y1": 0,
                 "visible": False, "what": "clouds"}))
            self.assertIn("not visible", hidden[0]["skipped"])
            self.assertFalse(timelapse.anchors_usable(
                json.dumps(hidden)))

    def test_the_named_path_narrates_its_anchoring_and_its_tracking(self):
        """The named subject's long first phase -- model calls, then the
        carry between anchors -- printed nothing, so its run showed no
        progress at all until rendering began. Each anchor now ticks the
        anchor band, and the survey ticks the tracking band above it."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            positions = [(80 + 10 * i, 90 + 4 * i) for i in range(9)]
            self.sequence(root, positions)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                chosen = timelapse.keyframes(str(root), "frame-*.jpg", 8)
                anchors = "[]"
                for index in (0, 8):
                    x, y = positions[index]
                    proof = timelapse.keyframe_proof(
                        str(root), f"frame-{index:03d}.jpg",
                        str(root / "anchors"))
                    anchors = timelapse.collect_anchor(
                        anchors, str(root), f"frame-{index:03d}.jpg", proof,
                        {"x0": x, "y0": y, "x1": x + 48, "y1": y + 48,
                         "visible": True, "what": "the stamp"},
                        chosen)
                timelapse.anchored_survey(str(root), "frame-*.jpg", anchors)
            markers = [line for line in out.getvalue().splitlines()
                       if line.startswith("TIMELAPSE_PROGRESS")]
            self.assertTrue(any("keyframes chosen" in m for m in markers))
            self.assertTrue(any("anchored keyframe 1 of 2" in m
                                for m in markers))
            self.assertTrue(any("tracking frame" in m for m in markers))
            percents = [int(m.split()[1]) for m in markers]
            self.assertEqual(percents, sorted(percents))
            self.assertLessEqual(max(percents), 45)

    def test_the_box_path_narrates_its_tracking(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100), (130, 100), (150, 104)])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                timelapse.track_survey(
                    str(root), "frame-*.jpg", "100,100,148,148")
            markers = [line for line in out.getvalue().splitlines()
                       if line.startswith("TIMELAPSE_PROGRESS")]
            self.assertTrue(any("tracking frame 3 of 3" in m
                                for m in markers))

    def test_the_tracker_carries_between_anchors_and_resets_on_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            positions = [(80 + 10 * i, 90 + 4 * i) for i in range(9)]
            self.sequence(root, positions)
            anchors = "[]"
            for index in (0, 8):
                x, y = positions[index]
                proof = timelapse.keyframe_proof(
                    str(root), f"frame-{index:03d}.jpg",
                    str(root / "anchors"))
                anchors = timelapse.collect_anchor(
                    anchors, str(root), f"frame-{index:03d}.jpg", proof,
                    {"x0": x, "y0": y, "x1": x + 48, "y1": y + 48,
                     "visible": True, "what": "the stamp"})
            told = json.loads(timelapse.anchored_survey(
                str(root), "frame-*.jpg", anchors))
            self.assertEqual(told["kept"], 9)
            for frame, (x, y) in zip(told["frames"], positions):
                self.assertAlmostEqual(frame["box"][0], x, delta=3)
            self.assertTrue(told["frames"][0].get("anchored"))
            self.assertTrue(told["frames"][8].get("anchored"))

    def test_frames_before_the_first_anchor_are_named_not_guessed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            positions = [(100, 100), (110, 104), (120, 108)]
            self.sequence(root, positions)
            x, y = positions[1]
            proof = timelapse.keyframe_proof(
                str(root), "frame-001.jpg", str(root / "anchors"))
            anchors = timelapse.collect_anchor(
                "[]", str(root), "frame-001.jpg", proof,
                {"x0": x, "y0": y, "x1": x + 48, "y1": y + 48,
                 "visible": True, "what": "the stamp"})
            told = json.loads(timelapse.anchored_survey(
                str(root), "frame-*.jpg", anchors))
            lost = {item["name"]: item["why"] for item in told["excluded"]}
            self.assertIn("frame-000.jpg", lost)
            self.assertIn("before the first usable anchor",
                          lost["frame-000.jpg"])

    def test_the_prompt_names_the_subject_and_the_proofs_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sequence(root, [(100, 100)])
            proof = timelapse.keyframe_proof(
                str(root), "frame-000.jpg", str(root / "anchors"),
                edge=320)
            built = timelapse.anchor_prompt("the red kite", proof)
            self.assertIn("the red kite", built)
            self.assertIn("320x", built)


if __name__ == "__main__":
    unittest.main()
