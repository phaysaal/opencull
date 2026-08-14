"""Protect then reveal: the reasoning a development is arrived at by.

These hold the parts that decide what gets rendered and what gets paid
for -- the budget, what carries forward between rounds, what the panel
is shown -- without spending a model call to find out.
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

import treatment_kernel as treatment  # noqa: E402


def frame(path: Path, bright=(700, 400)) -> Path:
    """A dark frame with one clipped light in it: the shape this is for."""
    pixels = np.full((600, 900, 3), 8, dtype=np.uint8)
    pixels[430:520, 200:640] = 3                      # a silhouette
    ys, xs = np.ogrid[:600, :900]
    # Tight, the way a specular source is: the surround has to be
    # dark or 'how far it stands out' measures nothing.
    glow = np.exp(-(((xs - bright[0]) ** 2 + (ys - bright[1]) ** 2) / 1200.0))
    for channel in range(3):
        pixels[..., channel] = np.clip(
            pixels[..., channel] + glow * 400, 0, 255).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels).save(path, quality=95)
    return path


def veiled(path: Path, level: int = 45) -> Path:
    """The same light, but sitting in a bright haze -- the veiled frame."""
    pixels = np.full((600, 900, 3), level, dtype=np.uint8)
    ys, xs = np.ogrid[:600, :900]
    glow = np.exp(-(((xs - 700) ** 2 + (ys - 400) ** 2) / 1200.0))
    for channel in range(3):
        pixels[..., channel] = np.clip(
            pixels[..., channel] + glow * 400, 0, 255).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels).save(path, quality=95)
    return path


def critique(finished: bool, change: str = "lift the ghost") -> str:
    return json.dumps({"improved": "the silhouette reads", "regressed": "",
                       "next_change": change, "finished": finished,
                       "rationale": "because"})


class MeasuringTests(unittest.TestCase):
    """The numbers the model reasons with, which cost nothing."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_it_finds_the_light_and_how_far_it_stands_out(self):
        measured = treatment.measure_frame(frame(self.root / "a.jpg"))
        across, down = measured["brightest_at"]
        self.assertAlmostEqual(across, 700 / 899, places=1)
        self.assertAlmostEqual(down, 400 / 599, places=1)
        self.assertGreater(measured["subject_separation"], 50)

    def test_it_reports_what_is_clipped_and_what_is_already_black(self):
        measured = treatment.measure_frame(frame(self.root / "a.jpg"))
        self.assertGreater(measured["clipped_percent"], 0)
        self.assertGreater(measured["silhouette_percent"], 50)

    def test_the_evidence_carries_the_spectrum_and_the_photographers_words(self):
        evidence = json.loads(treatment.evidence_json(
            "A.ARW", str(frame(self.root / "a.jpg")), "infrared", 760,
            "A partial solar eclipse; the crescent is the sun."))
        self.assertEqual(evidence["spectrum"], "infrared")
        self.assertEqual(evidence["cutoff_nm"], 760.0)
        self.assertIn("crescent is the sun", evidence["about"])
        self.assertIn("subject_separation", evidence["baseline"])


class SeparationTests(unittest.TestCase):
    """What "stands out" has to mean, for the loop to optimise it.

    It was the difference in levels between the subject and its
    surround. That is dominated by how bright the frame is, so on one
    eclipse frame it scored the untouched render 126.5 and the best
    hand-graded version 110.4 -- a loop following it would have learned
    to leave the veil alone. It is a ratio now.
    """

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_dropping_the_whole_frame_does_not_count_as_separating_it(self):
        light = treatment.measure_frame(frame(self.root / "a.jpg"))
        pixels = np.asarray(Image.open(self.root / "a.jpg").convert("RGB"))
        Image.fromarray((pixels * 0.45).astype(np.uint8)).save(
            self.root / "dark.jpg", quality=95)
        dark = treatment.measure_frame(self.root / "dark.jpg")
        self.assertLess(dark["mean"], light["mean"] * 0.6)
        self.assertAlmostEqual(
            dark["subject_separation"], light["subject_separation"], delta=6)

    def test_a_veiled_frame_scores_below_a_clean_one(self):
        clean = treatment.measure_frame(frame(self.root / "a.jpg"))
        hazed = treatment.measure_frame(veiled(self.root / "veiled.jpg"))
        self.assertGreater(clean["subject_separation"],
                           hazed["subject_separation"] + 10)

    def test_both_levels_are_reported_so_nothing_is_hidden_by_the_ratio(self):
        measured = treatment.measure_frame(frame(self.root / "a.jpg"))
        self.assertGreater(measured["subject_level"],
                           measured["surround_level"])


class BudgetTests(unittest.TestCase):
    """A photographer does not give one frame infinite time."""

    def round(self, number: int, finished: bool, sections: str = "{}") -> str:
        return json.dumps({"round": number, "sections": sections,
                           "critique": json.loads(critique(finished))})

    def test_the_first_round_always_runs(self):
        self.assertTrue(treatment.keep_going([], 3))

    def test_a_finished_critique_stops_it_early(self):
        self.assertFalse(treatment.keep_going([self.round(1, True)], 3))

    def test_an_unfinished_one_buys_another_round(self):
        self.assertTrue(treatment.keep_going([self.round(1, False)], 3))

    def test_the_budget_stops_it_when_the_critique_will_not(self):
        spent = [self.round(n, False) for n in (1, 2, 3)]
        self.assertFalse(treatment.keep_going(spent, 3))

    def test_a_budget_of_one_buys_exactly_one(self):
        self.assertTrue(treatment.keep_going([], 1))
        self.assertFalse(treatment.keep_going([self.round(1, False)], 1))

    def test_a_round_with_no_critique_does_not_stop_the_loop(self):
        """A round whose recipe would not compile has nothing to say."""
        failed = json.dumps({"round": 1, "sections": "{}", "critique": None})
        self.assertTrue(treatment.keep_going([failed], 3))


class CarriedForwardTests(unittest.TestCase):
    """What one round tells the next. Without this it is three guesses."""

    def test_the_critique_reaches_the_next_recipe(self):
        record = json.dumps({
            "round": 1, "sections": "{}", "unsupported": "",
            "critique": json.loads(critique(False, "lift the left crescent"))})
        carried = treatment.latest_critique([record])
        self.assertIn("lift the left crescent", carried)
        self.assertIn("Improved", carried)

    def test_what_would_not_compile_is_carried_too(self):
        record = json.dumps({
            "round": 1, "sections": "{}",
            "unsupported": "These instructions did not compile: Use AgX",
            "critique": json.loads(critique(False))})
        self.assertIn("did not compile", treatment.latest_critique([record]))

    def test_the_first_round_is_told_nothing_and_asks_for_nothing(self):
        self.assertEqual(treatment.latest_critique([]), "")
        self.assertEqual(treatment.latest_sections([]), "")


class RecipeTests(unittest.TestCase):
    """Turning an answer into something that renders, or saying why not."""

    def test_sections_are_read_out_of_the_answer(self):
        answer = json.dumps({
            "global_exposure": ["Exposure -1.80", "Contrast +26"],
            "layers_and_masks": ["Radial gradient on the sun, inverted: "
                                 "exposure +0.60"]})
        sections = json.loads(treatment.recipe_sections(answer))
        self.assertEqual(sections["global_exposure"][0], "Exposure -1.80")
        self.assertIn("layers_and_masks", sections)

    def test_a_section_nobody_recognises_is_dropped(self):
        answer = json.dumps({"global_exposure": ["Contrast +10"],
                             "vibes": ["make it feel nostalgic"]})
        self.assertNotIn("vibes", json.loads(treatment.recipe_sections(answer)))

    def test_one_string_where_a_list_was_wanted_is_met_halfway(self):
        """The round is already paid for."""
        answer = json.dumps({"global_exposure": "Contrast +10"})
        sections = json.loads(treatment.recipe_sections(answer))
        self.assertEqual(sections["global_exposure"], ["Contrast +10"])

    def test_an_answer_that_is_not_json_costs_the_round_and_not_the_run(self):
        self.assertEqual(treatment.recipe_sections("sorry, I cannot"), "{}")

    def test_a_recipe_that_does_nothing_is_not_usable(self):
        compiled = treatment.compiled_treatment(
            "A.ARW", {"title": "t", "intent": "i"},
            json.dumps({"global_exposure": ["be lovely"]}))
        self.assertFalse(treatment.treatment_usable(compiled))

    def test_a_recipe_that_does_something_is(self):
        compiled = treatment.compiled_treatment(
            "A.ARW", {"title": "t", "intent": "i"},
            json.dumps({"global_exposure": ["Contrast +12"]}))
        self.assertTrue(treatment.treatment_usable(compiled))
        self.assertTrue(json.loads(compiled)["operations"])

    def test_what_did_not_compile_is_named_for_the_next_round(self):
        compiled = treatment.compiled_treatment(
            "A.ARW", {"title": "t", "intent": "i"}, json.dumps({
                "global_exposure": ["Contrast +12", "Use the AgX transform"]}))
        self.assertIn("AgX", treatment.unsupported_note(compiled))


class AnswerShapeTests(unittest.TestCase):
    """A model's answer, read without trusting its shape."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_the_recipe_is_read_out_of_the_answers_field(self):
        answer = {"title": "t", "intent": "i",
                  "recipe": json.dumps({"global_exposure": ["Contrast +12"]})}
        sections = json.loads(treatment.recipe_sections(answer))
        self.assertEqual(sections["global_exposure"], ["Contrast +12"])

    def test_a_recipe_that_arrived_already_parsed_is_read(self):
        """Three rounds of a live run came back empty on this.

        The field may hold the JSON text the schema asks for, or the
        object itself where the runtime parsed it first. Stringifying a
        mapping and hoping it is JSON gets you repr, which is not.
        """
        answer = {"title": "t", "intent": "i",
                  "recipe": {"global_exposure": ["Exposure -1.80"]}}
        sections = json.loads(treatment.recipe_sections(answer))
        self.assertEqual(sections["global_exposure"], ["Exposure -1.80"])

    def test_a_round_that_produced_nothing_keeps_what_the_model_said(self):
        """The round most worth reading afterwards is the empty one."""
        where = treatment.treatment_directory(
            str(self.root), "A.ARW", "20260814T000004Z")
        compiled = treatment.compiled_treatment("A.ARW", {}, "{}")
        treatment.round_record(where, [], compiled, "{}",
                               answer={"recipe": "something unreadable"})
        kept = json.loads((Path(where) / "round-1.json").read_text())
        self.assertIn("unreadable", kept["answer"])

    def test_a_round_that_worked_does_not_keep_the_raw_answer(self):
        where = treatment.treatment_directory(
            str(self.root), "A.ARW", "20260814T000005Z")
        sections = json.dumps({"global_exposure": ["Contrast +12"]})
        compiled = treatment.compiled_treatment("A.ARW", {}, sections)
        treatment.round_record(where, [], compiled, sections,
                               answer={"recipe": sections})
        kept = json.loads((Path(where) / "round-1.json").read_text())
        self.assertEqual(kept["answer"], "")

    def test_an_answer_that_ignored_the_field_is_still_read(self):
        answer = {"global_exposure": ["Contrast +12"]}
        self.assertIn("global_exposure",
                      json.loads(treatment.recipe_sections(answer)))

    def test_a_missing_field_is_a_soft_fact_not_a_crash(self):
        self.assertEqual(treatment.field({"title": "t"}, "intent"), "")
        self.assertEqual(treatment.field({}, "title", "Treatment"), "Treatment")

    def test_reasoning_is_written_before_it_is_judged(self):
        """A model answered, the answer failed a check, and the run died
        without keeping the thing that had just been paid for."""
        where = treatment.treatment_directory(
            str(self.root), "A.ARW", "20260814T000000Z")
        kept = treatment.keep_reasoning(
            where, "diagnosis",
            {"subject": "an eclipse", "irrecoverable": "the clipped core"})
        self.assertTrue(kept)
        written = Path(where) / "diagnosis.json"
        self.assertIn("clipped core", written.read_text())

    def test_an_empty_answer_is_kept_and_reported_as_empty(self):
        where = treatment.treatment_directory(
            str(self.root), "A.ARW", "20260814T000001Z")
        self.assertFalse(treatment.keep_reasoning(where, "diagnosis", {}))
        self.assertTrue((Path(where) / "diagnosis.json").is_file())

    def test_an_answer_nothing_can_read_keeps_what_arrived(self):
        """An empty file and a guess is how the first live run was spent."""
        where = treatment.treatment_directory(
            str(self.root), "A.ARW", "20260814T000002Z")
        self.assertFalse(treatment.keep_reasoning(where, "diagnosis", 42))
        kept = json.loads((Path(where) / "diagnosis.json").read_text())
        self.assertEqual(kept["type"], "int")
        self.assertIn("42", kept["unreadable"])

    def test_an_answer_that_arrived_as_json_text_is_read(self):
        answer = json.dumps({"subject": "an eclipse",
                             "irrecoverable": "the clipped core"})
        self.assertEqual(treatment.field(answer, "subject"), "an eclipse")
        where = treatment.treatment_directory(
            str(self.root), "A.ARW", "20260814T000003Z")
        self.assertTrue(treatment.keep_reasoning(where, "diagnosis", answer))


class DecisionTests(unittest.TestCase):
    """A procedure that branches, not four questions in a fixed order.

    The first shape asked about masks however the photograph answered.
    A frame needing none paid for the reasoning anyway; a frame needing
    three got one paragraph describing all of them.
    """

    def plan(self, count, **globals_):
        return {"title": "t", "strategy": "protect the core",
                "mask_count": count,
                "global_adjustments": globals_ or {"exposure": -1.8}}

    def test_a_photograph_needing_no_mask_asks_for_none(self):
        self.assertEqual(treatment.mask_count(self.plan(0)), 0)

    def test_a_count_nobody_could_mean_is_brought_into_range(self):
        self.assertEqual(treatment.mask_count(self.plan(9)), 3)
        self.assertEqual(treatment.mask_count(self.plan(-2)), 0)
        self.assertEqual(treatment.mask_count({"mask_count": "two"}), 0)

    def test_the_whole_frame_moves_become_operations(self):
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", self.plan(0, exposure=-1.8, contrast=26, denoise=90), []))
        self.assertEqual(
            [item["op"] for item in recipe["operations"]],
            ["tone.exposure", "tone.contrast", "detail.denoise_luminance"])
        self.assertEqual(recipe["operations"][0]["value"], -1.8)

    def test_a_move_nobody_offered_is_dropped(self):
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", self.plan(0, exposure=-1.0, nostalgia=7), []))
        self.assertEqual(len(recipe["operations"]), 1)

    def test_a_number_out_of_range_is_bounded_not_refused(self):
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", self.plan(0, exposure=-40), []))
        self.assertEqual(recipe["operations"][0]["value"], -5.0)

    def test_a_placed_mask_carries_its_centre_and_radius(self):
        mask = {"region": "everything but the sun", "shape": "radial",
                "centre_x": 44, "centre_y": 49, "radius": 18,
                "inverted": True, "feather": 90,
                "adjustments": {"exposure": 0.6}}
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", self.plan(1), [mask]))
        placed = recipe["operations"][-1]
        self.assertEqual(placed["op"], "mask.radial")
        self.assertIn("at 44%, 49%", placed["value"]["anchor"])
        self.assertIn("radius 18%", placed["value"]["anchor"])
        self.assertIn("inverted", placed["value"]["anchor"])

    def test_a_mask_with_nothing_inside_it_is_not_added(self):
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", self.plan(1),
            [{"shape": "radial", "adjustments": {}}]))
        self.assertNotIn("mask.radial",
                         [item["op"] for item in recipe["operations"]])

    def test_a_mask_carries_every_move_the_whole_frame_can(self):
        """The engine runs the global operation and blends it through the mask.

        A hand-picked subset here once dropped shadows, blacks, whites
        and dehaze -- the moves for a silhouette. Measured on one frame:
        a bottom gradient asking exposure +0.8 with shadows +35 rendered
        byte-identical to exposure alone, and two treatments spent three
        rounds each asking again.
        """
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", self.plan(1),
            [{"shape": "linear", "anchor": "bottom",
              "adjustments": {"exposure": 0.4, "shadows": 35, "blacks": 20,
                              "dehaze": 10}}]))
        effects = recipe["operations"][-1]["value"]["effects"]
        self.assertEqual(
            sorted(item["op"] for item in effects),
            ["detail.dehaze", "tone.black", "tone.exposure", "tone.shadow"])

    def test_a_move_nobody_offered_is_still_dropped_from_a_mask(self):
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", self.plan(1),
            [{"shape": "linear", "anchor": "bottom",
              "adjustments": {"exposure": 0.4, "nostalgia": 7}}]))
        effects = recipe["operations"][-1]["value"]["effects"]
        self.assertEqual([item["op"] for item in effects], ["tone.exposure"])

    def test_a_shape_nobody_recognises_becomes_a_radial(self):
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", self.plan(1),
            [{"shape": "hexagon", "adjustments": {"exposure": 0.3}}]))
        self.assertEqual(recipe["operations"][-1]["op"], "mask.radial")

    def test_the_assembled_recipe_always_renders_what_it_says(self):
        """Numbers cannot half-compile; that was the point of the change."""
        recipe = treatment.assemble_recipe(
            "A.ARW", self.plan(0, exposure=-1.8), [])
        self.assertTrue(treatment.treatment_usable(recipe))
        self.assertEqual(json.loads(recipe)["coverage"]["unsupported"], 0)

    def test_the_masks_are_asked_for_one_at_a_time_and_kept(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        where = treatment.treatment_directory(
            str(root), "A.ARW", "20260814T000020Z")
        masks = []
        for region in ("the sun", "the skyline"):
            self.assertTrue(treatment.keep_mask(
                where, masks, {"region": region, "shape": "radial"}))
            masks.append({"region": region})
        self.assertTrue((Path(where) / "mask-1.json").is_file())
        self.assertTrue((Path(where) / "mask-2.json").is_file())


class AnchorTests(unittest.TestCase):
    """A round is a change to the renderer's own untouched frame.

    The first live run measured its rounds against a darktable export.
    The two engines sit two stops apart at rest, so every critique read
    that gap as damage and asked for more darkening; three rounds went
    the wrong way and the panel refused all of them.
    """

    def setUp(self):
        self.asked = []
        self.root = Path(tempfile.mkdtemp())
        stand_in = self.root / "rendered.jpg"
        stand_in.write_bytes(b"\xff\xd8rendered")

        def _render(photos, photo, recipe_text, maximum):
            self.asked.append(json.loads(recipe_text))
            return str(stand_in)

        self.original = treatment._render
        treatment._render = _render
        self.addCleanup(lambda: setattr(treatment, "_render", self.original))

    def test_the_starting_frame_has_nothing_done_to_it(self):
        treatment.starting_frame("/photos", "A.ARW", str(self.root), 1100)
        self.assertEqual(self.asked[0]["operations"], [])
        self.assertEqual(self.asked[0]["source_photo"], "A.ARW")

    def test_it_is_kept_on_disk_as_the_honest_before(self):
        where = treatment.starting_frame("/photos", "A.ARW", str(self.root), 1100)
        self.assertEqual(Path(where).name, "start.jpg")
        self.assertTrue(Path(where).is_file())

    def test_it_does_not_take_a_round_number_from_the_budget(self):
        treatment.starting_frame("/photos", "A.ARW", str(self.root), 1100)
        first = treatment.render_round(
            "/photos", "A.ARW", "{}", str(self.root), [], 1100)
        self.assertEqual(Path(first).name, "round-1.jpg")


class RepeatedRoundTests(unittest.TestCase):
    """A round that changed nothing is the end of the argument."""

    def round(self, number, measurements, finished=False):
        return json.dumps({
            "round": number, "measurements": measurements,
            "critique": {"finished": finished, "next_change": "lift it"},
        })

    def test_two_identical_renders_end_the_loop(self):
        same = {"mean": 10.78, "subject_separation": 22.0}
        self.assertFalse(treatment.keep_going(
            [self.round(1, same), self.round(2, dict(same))], 3))

    def test_a_round_that_moved_keeps_the_budget_open(self):
        self.assertTrue(treatment.keep_going(
            [self.round(1, {"mean": 10.33}), self.round(2, {"mean": 10.78})], 3))

    def test_a_round_that_never_rendered_does_not_count_as_a_repeat(self):
        self.assertTrue(treatment.keep_going(
            [self.round(1, {}), self.round(2, {})], 3))


class CarriedMovesTests(unittest.TestCase):
    """A round revises the last one; it does not start from nothing.

    Live, the critique said "leave the whole-frame treatment unchanged"
    and the plan came back with no whole-frame moves at all. Read as a
    fresh recipe that is -1.5 EV thrown away: mean went 10.3 back up to
    59.4, twice in one run.
    """

    def round(self, **moves):
        plan = {"mask_count": 0, "global_adjustments": moves}
        return json.dumps({
            "round": 1,
            "recipe": json.loads(treatment.assemble_recipe("A.ARW", plan, [])),
        })

    def test_what_was_applied_is_what_carries(self):
        held = json.loads(treatment.standing_moves(
            [self.round(exposure=-1.5, contrast=35)]))
        self.assertEqual(held, {"exposure": -1.5, "contrast": 35.0})

    def test_a_move_carries_at_the_value_the_renderer_took_not_the_one_asked(self):
        held = json.loads(treatment.standing_moves([self.round(exposure=-40)]))
        self.assertEqual(held["exposure"], -5.0)

    def test_saying_nothing_keeps_the_frame_where_it_was(self):
        standing = treatment.standing_moves([self.round(exposure=-1.5)])
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", {"mask_count": 0, "global_adjustments": {}}, [], standing))
        self.assertEqual(recipe["operations"][0]["value"], -1.5)

    def test_a_move_that_is_named_replaces_the_one_standing(self):
        standing = treatment.standing_moves([self.round(exposure=-1.5)])
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", {"global_adjustments": {"exposure": -1.8}}, [], standing))
        self.assertEqual(recipe["operations"][0]["value"], -1.8)

    def test_zero_is_how_a_move_is_taken_away(self):
        standing = treatment.standing_moves(
            [self.round(exposure=-1.5, contrast=35)])
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", {"global_adjustments": {"contrast": 0}}, [], standing))
        self.assertEqual([item["op"] for item in recipe["operations"]],
                         ["tone.exposure"])

    def test_a_mask_is_not_mistaken_for_a_whole_frame_move(self):
        """Its source line is the region it is around, not 'name number'."""
        plan = {"mask_count": 1, "global_adjustments": {"exposure": -1.5}}
        made = json.dumps({"round": 1, "recipe": json.loads(
            treatment.assemble_recipe("A.ARW", plan, [
                {"region": "the rooftop and tree line", "shape": "linear",
                 "anchor": "bottom", "adjustments": {"exposure": 0.3}}]))})
        self.assertEqual(json.loads(treatment.standing_moves([made])),
                         {"exposure": -1.5})

    def test_nothing_stands_before_the_first_round(self):
        self.assertEqual(treatment.standing_moves([]), "{}")

    def test_a_round_that_never_rendered_is_skipped(self):
        empty = json.dumps({"round": 2, "recipe": {"operations": []}})
        held = json.loads(treatment.standing_moves(
            [self.round(exposure=-1.5), empty]))
        self.assertEqual(held, {"exposure": -1.5})

    def test_the_plan_is_told_what_it_is_revising(self):
        built = treatment.plan_prompt(
            json.dumps({"baseline": {}}), {"next_change": "lift the rooftop"},
            json.dumps({"exposure": -1.5}))
        self.assertIn("CURRENTLY IN FORCE", built)
        self.assertIn("-1.5", built)
        self.assertIn("as 0", built)

    def test_the_first_plan_is_not_told_it_is_revising_anything(self):
        built = treatment.plan_prompt(json.dumps({"baseline": {}}))
        self.assertNotIn("CURRENTLY IN FORCE", built)


class LostAnswerTests(unittest.TestCase):
    """An answer that never arrived costs its mask, not the treatment.

    The runtime discards a generation that is missing any field of its
    schema. A linear mask has no centre and no radius, the model left
    them out, every retry was thrown away -- and the check on the mask
    took the whole run down with it: plan paid for, frame rendered,
    nothing kept.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_a_mask_that_never_arrived_is_not_kept(self):
        self.assertFalse(treatment.keep_mask(str(self.root), [], None))

    def test_what_did_arrive_is_written_down_anyway(self):
        treatment.keep_mask(str(self.root), [], None)
        kept = json.loads((self.root / "mask-1.json").read_text())
        self.assertIn("unreadable", kept)

    def test_the_masks_that_did_arrive_still_make_a_recipe(self):
        good = {"region": "the rooftop", "shape": "linear",
                "anchor": "bottom", "adjustments": {"exposure": 0.4}}
        recipe = json.loads(treatment.assemble_recipe(
            "A.ARW", {"mask_count": 2, "global_adjustments": {"exposure": -1.5}},
            [good]))
        self.assertEqual([item["op"] for item in recipe["operations"]],
                         ["tone.exposure", "mask.linear"])

    def test_the_mask_question_says_every_field_is_required(self):
        built = treatment.mask_prompt(
            json.dumps({"baseline": {}}), {"strategy": "s"}, 0, 1, [])
        self.assertIn("EVERY FIELD PRESENT", built)


class WhatThePanelIsAskedTests(unittest.TestCase):
    """One photographic question, not five clauses joined by semicolons.

    The claim used to include where the measurements sit and how the
    round was chosen. Those are facts about the report file, true by
    inspection; a panel asked to certify them refused the conjunction
    and there was no telling which clause it disliked. They are checked
    now, and what is put to the panel is the judgement.
    """

    def report(self, chosen, separations=(17.3, 17.2, 16.7)):
        rounds = [{
            "round": n + 1, "sections": "protect the core",
            "render": f"round-{n + 1}.jpg",
            "recipe": {"operations": [{"op": "tone.exposure", "value": -1.2}]},
            "measurements": {"subject_separation": value},
            "critique": {"finished": False},
        } for n, value in enumerate(separations)]
        return json.dumps({
            "format": "darkimiya-treatment-v1", "photo": "A.ARW",
            "evidence": {"baseline": {}}, "reasoning": "protect the core",
            "rounds": rounds, "chosen_round": chosen})

    def test_offering_the_best_measuring_round_is_checked_not_judged(self):
        self.assertTrue(treatment.treatment_valid(self.report(1)))

    def test_offering_a_worse_round_fails_the_check(self):
        self.assertFalse(treatment.treatment_valid(self.report(3)))

    def test_a_round_with_no_measurements_fails_the_check(self):
        self.assertFalse(treatment.treatment_valid(
            self.report(1, separations=(17.3, None))))

    def test_the_claim_is_one_question_about_the_photograph(self):
        policy = treatment.treatment_policy(json.dumps({"about": ""}))
        self.assertIn("beyond recovery", policy)
        self.assertNotIn("measurements are reported", policy)
        self.assertLessEqual(policy.count(";"), 1)


class UnculledFolderTests(unittest.TestCase):
    """Developing one frame must not require having culled its folder.

    The renderer wanted the folder's cull report, so a folder of 300
    raws that nobody had culled could not be treated at all -- and
    culling 300 frames to develop one is not a thing to make somebody
    pay for.
    """

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        (self.root / "DSCF1221.RAF").write_bytes(b"not really a raw")
        self.layout = {"Reports": self.root / ".darkimiya" / "Reports"}
        self.layout["Reports"].mkdir(parents=True)

    def test_a_folder_with_no_cull_still_names_its_photograph(self):
        roster = treatment._roster(self.root, self.layout, "DSCF1221.RAF")
        self.assertEqual(roster.photo_names, ("DSCF1221.RAF",))

    def test_the_roster_is_never_written_to_the_photographers_folder(self):
        treatment._roster(self.root, self.layout, "DSCF1221.RAF")
        self.assertEqual(list(self.layout["Reports"].glob("*")), [])

    def test_a_photograph_that_is_not_there_is_refused(self):
        with self.assertRaises(ValueError):
            treatment._roster(self.root, self.layout, "MISSING.RAF")

    def test_a_real_cull_is_preferred_where_there_is_one(self):
        report = self.layout["Reports"] / "shoot-results.json"
        report.write_text(json.dumps({
            "format": "opencull-report-v2",
            "clusters": [{"cluster_id": "c1", "photos": ["DSCF1221.RAF"]}],
            "keep": [{"cluster_id": "c1", "photos": ["DSCF1221.RAF"]}]}))
        roster = treatment._roster(self.root, self.layout, "DSCF1221.RAF")
        self.assertEqual(roster.path, report)


class GateTests(unittest.TestCase):
    """Is an edit necessary at all -- asked before anything is spent.

    The procedure diagnosed, decided, separated and then edited. There
    was no way to answer "this frame is already right": an empty recipe
    counted as a failed round, and "finished" could only be said by a
    critique, which only runs after a render. So the earliest the loop
    could conclude nothing was needed was after it had already edited
    once. On a Fujifilm frame that arrived well exposed it edited three
    times and finished with a posterised sky.
    """

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_a_frame_already_right_is_left_alone(self):
        self.assertFalse(treatment.edit_wanted(
            {"needs_edit": False, "verdict": "already right"}, []))

    def test_a_frame_that_wants_work_goes_on(self):
        self.assertTrue(treatment.edit_wanted({"needs_edit": True}, []))

    def test_an_answer_that_never_arrived_means_yes(self):
        """A lost generation must not read as a decision to do nothing."""
        self.assertTrue(treatment.edit_wanted(None, []))
        self.assertTrue(treatment.edit_wanted({}, []))

    def test_the_word_no_is_read_as_no(self):
        self.assertFalse(treatment.edit_wanted({"needs_edit": "false"}, []))
        self.assertTrue(treatment.edit_wanted({"needs_edit": "yes"}, []))

    def test_only_the_first_round_is_gated(self):
        """After a render the question is answered better by the critique."""
        already = [json.dumps({"round": 1, "measurements": {"mean": 10}})]
        self.assertTrue(treatment.edit_wanted({"needs_edit": False}, already))

    def test_leaving_it_alone_is_a_round_with_the_untouched_frame_in_it(self):
        kept = json.loads(treatment.left_alone_record(
            str(self.root), [], {"needs_edit": False, "verdict": "well judged"},
            "/where/start.jpg", json.dumps({"subject_separation": 37.1})))
        self.assertEqual(kept["render"], "/where/start.jpg")
        self.assertEqual(kept["recipe"]["operations"], [])
        self.assertEqual(kept["measurements"]["subject_separation"], 37.1)
        self.assertTrue((self.root / "round-1.json").is_file())

    def test_leaving_it_alone_ends_the_loop(self):
        kept = treatment.left_alone_record(
            str(self.root), [], {"verdict": "well judged"},
            "/where/start.jpg", "{}")
        self.assertFalse(treatment.keep_going([kept], 3))

    def test_the_reason_it_was_left_alone_is_kept(self):
        kept = json.loads(treatment.left_alone_record(
            str(self.root), [], {"verdict": "the veil is the subject"},
            "/where/start.jpg", "{}"))
        self.assertEqual(kept["critique"]["rationale"],
                         "the veil is the subject")

    def test_a_treatment_that_only_left_it_alone_still_checks_out(self):
        kept = treatment.left_alone_record(
            str(self.root), [], {"verdict": "already right"},
            "/where/start.jpg", json.dumps({"subject_separation": 37.1}))
        report = treatment.treatment_report(
            str(self.root), "DSCF1221.RAF",
            json.dumps({"baseline": {"subject_separation": 37.1}}), [kept], 1)
        self.assertTrue(treatment.treatment_valid(report))

    def test_the_question_is_put_before_the_masks_and_the_numbers(self):
        built = treatment.plan_prompt(json.dumps({"baseline": {}}))
        self.assertIn("IS AN EDIT NECESSARY AT ALL?", built)
        self.assertLess(built.index("IS AN EDIT NECESSARY"),
                        built.index("SEPARATE"))

    def test_the_question_warns_against_answering_no_out_of_caution(self):
        built = treatment.plan_prompt(json.dumps({"baseline": {}}))
        self.assertIn("not answer no out of caution", built)

    def test_the_panel_is_told_leaving_it_alone_is_an_outcome(self):
        policy = treatment.treatment_policy(json.dumps({"about": ""}))
        self.assertIn("left it alone", policy)


class ContactSheetTests(unittest.TestCase):
    """Every round on one sheet, so the argument can be looked at."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        frame(self.root / "start.jpg")
        self.records = []
        for n in (1, 2):
            render = frame(self.root / f"round-{n}.jpg", bright=(700, 380 + n))
            self.records.append(json.dumps({
                "round": n, "render": str(render),
                "measurements": {"mean": 10.0 + n}}))

    def test_it_puts_the_start_and_every_round_on_one_sheet(self):
        where = treatment.contact_sheet(str(self.root), self.records, 1)
        self.assertEqual(Path(where).name, "contact-sheet.jpg")
        with Image.open(where) as sheet:
            self.assertGreater(sheet.height, 600)

    def test_a_round_whose_render_is_gone_is_left_out_not_fatal(self):
        missing = json.dumps({"round": 3, "render": str(self.root / "no.jpg")})
        self.assertTrue(treatment.contact_sheet(
            str(self.root), [*self.records, missing], 1))

    def test_nothing_to_compare_makes_no_sheet(self):
        empty = Path(tempfile.mkdtemp())
        self.assertEqual(treatment.contact_sheet(str(empty), [], 0), "")


class BoundaryTests(unittest.TestCase):
    """Nothing crossing from the program assumes what shape it is in.

    Three live runs died one call site at a time on this: a value
    arrives as a mapping, as the JSON text of one, or as an object,
    depending on the runtime and on whether it came fresh or from a
    memo. Each death had already paid for the answer it threw away.
    """

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_data_reads_all_three_shapes(self):
        self.assertEqual(treatment.data({"a": 1}), {"a": 1})
        self.assertEqual(treatment.data('{"a": 1}'), {"a": 1})
        self.assertIsNone(treatment.data("not json at all"))

    def test_a_round_records_a_critique_that_arrived_as_a_mapping(self):
        """The shape the third live run died on."""
        where = treatment.treatment_directory(
            str(self.root), "A.ARW", "20260814T000010Z")
        compiled = treatment.compiled_treatment(
            "A.ARW", {}, json.dumps({"global_exposure": ["Contrast +12"]}))
        record = json.loads(treatment.round_record(
            where, [], compiled, "{}", "r.jpg",
            {"subject_separation": 70},
            {"improved": "x", "finished": True, "next_change": ""}))
        self.assertEqual(record["measurements"]["subject_separation"], 70)
        self.assertTrue(record["critique"]["finished"])

    def test_the_budget_reads_records_whatever_shape_they_are(self):
        as_mapping = {"round": 1, "critique": {"finished": True}}
        self.assertFalse(treatment.keep_going([as_mapping], 3))
        self.assertFalse(treatment.keep_going([json.dumps(as_mapping)], 3))

    def test_a_recipe_that_arrived_as_a_mapping_still_renders_and_validates(self):
        compiled = treatment.compiled_treatment(
            "A.ARW", {}, {"global_exposure": ["Contrast +12"]})
        self.assertTrue(treatment.treatment_usable(compiled))

    def test_prompts_survive_a_previous_answer_that_is_a_mapping(self):
        """A prompt that concatenates its inputs has to survive all three."""
        evidence = json.dumps({"about": "an eclipse", "spectrum": "visible",
                               "baseline": {"brightest_at": [0.4, 0.5]}})
        plan = {"protect": ["the clipped core"], "reveal": ["the skyline"],
                "mask_count": 1}
        for built in (
            treatment.plan_prompt(evidence, plan),
            treatment.mask_prompt(evidence, plan, 0, 1, []),
            treatment.critique_prompt(evidence, plan,
                                      json.dumps({"mean": 30}), 1, 3),
        ):
            self.assertIn("the clipped core", built)


class KeepingTheWorkingTests(unittest.TestCase):
    """Every round on disk, and the choice between them."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def directory(self) -> str:
        return treatment.treatment_directory(
            str(self.root), "DSC00703.ARW", "20260814T000000Z")

    def test_a_round_is_written_before_the_next_one_starts(self):
        where = self.directory()
        compiled = treatment.compiled_treatment(
            "A.ARW", {"title": "t", "intent": "i"},
            json.dumps({"global_exposure": ["Contrast +12"]}))
        treatment.round_record(where, [], compiled, "{}", "render.jpg",
                               json.dumps({"subject_separation": 70}),
                               critique(False))
        written = Path(where) / "round-1.json"
        self.assertTrue(written.is_file())
        self.assertEqual(json.loads(written.read_text())["round"], 1)

    def test_rounds_number_themselves_in_order(self):
        where = self.directory()
        compiled = treatment.compiled_treatment(
            "A.ARW", {"title": "t", "intent": "i"},
            json.dumps({"global_exposure": ["Contrast +12"]}))
        records = []
        for _ in range(3):
            records.append(treatment.round_record(
                where, records, compiled, "{}", "r.jpg",
                json.dumps({"subject_separation": 70}), critique(False)))
        self.assertEqual([json.loads(item)["round"] for item in records],
                         [1, 2, 3])

    def test_the_round_offered_is_the_one_that_measured_best(self):
        """Not merely the last one tried."""
        rounds = [
            json.dumps({"round": 1, "measurements": {"subject_separation": 70}}),
            json.dumps({"round": 2, "measurements": {"subject_separation": 92}}),
            json.dumps({"round": 3, "measurements": {"subject_separation": 81}}),
        ]
        self.assertEqual(treatment.best_round(rounds), 2)

    def test_a_tie_goes_to_the_round_that_heard_more_criticism(self):
        rounds = [
            json.dumps({"round": 1, "measurements": {"subject_separation": 88}}),
            json.dumps({"round": 2, "measurements": {"subject_separation": 88}}),
        ]
        self.assertEqual(treatment.best_round(rounds), 2)


class WarrantTests(unittest.TestCase):
    """What the panel is shown, and what it is asked."""

    def report(self, rounds=2, renders=True) -> str:
        made = [json.dumps({
            "round": n, "sections": "protect the core, reveal the skyline",
            "recipe": {"operations": [{"op": "tone.exposure", "value": -1.8}]},
            "unsupported": "",
            "render": f"round-{n}.jpg" if renders else "",
            "measurements": {"subject_separation": 70 + n, "clipped_percent": 0.2},
            "critique": json.loads(critique(n == rounds)),
        }) for n in range(1, rounds + 1)]
        return treatment.treatment_report(
            "/where", "A.ARW",
            json.dumps({"photo": "A.ARW", "about": "an eclipse",
                        "baseline": {"subject_separation": 60}}),
            made, treatment.best_round(made))

    def test_a_finished_treatment_passes_its_own_checks(self):
        self.assertTrue(treatment.treatment_valid(self.report()))

    def test_a_treatment_that_never_rendered_does_not(self):
        self.assertFalse(treatment.treatment_valid(self.report(renders=False)))

    def test_the_panel_is_shown_what_the_treatment_said_it_would_do(self):
        """It was shown three empty strings, and refused -- correctly."""
        warrant = treatment.treatment_evidence(self.report())
        self.assertIn("protect the core, reveal the skyline", warrant)

    def test_the_panel_is_shown_the_plan_and_the_numbers_not_the_picture(self):
        warrant = treatment.treatment_evidence(self.report())
        self.assertIn("subject_separation", warrant)
        self.assertIn("protect the core", warrant)
        self.assertNotIn(".jpg", warrant)

    def test_every_round_is_in_the_warrant_including_the_ones_not_chosen(self):
        warrant = json.loads(
            treatment.treatment_evidence(self.report(rounds=3)).split("\n", 1)[1])
        self.assertEqual(len(warrant["each_round"]), 3)
        self.assertEqual(warrant["rounds_spent"], 3)

    def test_the_policy_says_it_must_not_chase_the_irrecoverable(self):
        policy = treatment.treatment_policy(
            json.dumps({"about": "a partial solar eclipse"}))
        self.assertIn("beyond recovery", policy)
        self.assertIn("solar eclipse", policy)

    def test_an_abstention_keeps_the_evidence(self):
        refusal = treatment.treatment_abstention("the numbers")
        self.assertIn("still on disk", refusal)
        self.assertIn("the numbers", refusal)


if __name__ == "__main__":
    unittest.main()
