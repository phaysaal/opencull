import json
import tempfile
import unittest
from pathlib import Path

from recipe_compiler import (
    CHANNEL_SWAPS,
    CORPUS_FORMAT,
    IR_FORMAT,
    PRESET_STYLE,
    RecipeCompileError,
    compile_checkpoint,
    compile_recipe,
)


class RecipeCompilerTests(unittest.TestCase):
    def sample(self):
        return {
            "global_exposure": [
                "Exposure +0.3", "Contrast +10", "Brightness -5"],
            "white_balance_and_color": [
                "Kelvin +400 (warmer)", "Tint -5 (magenta)"],
            "composition": ["Crop to 3:2 aspect ratio", "Rotate -0.5 degrees"],
            "layers_and_masks": [
                "Brush Mask (face, 20% opacity) to brighten subject"],
            "evaluation_order": ["Check histogram for clipping"],
        }

    def test_compiles_typed_bounded_operations_without_guessing_masks(self):
        result = compile_recipe(
            "A.RAF", "standard", "Natural", "Clean rendering",
            self.sample(), "Preserve skin.", "raw")
        self.assertEqual(result["format"], IR_FORMAT)
        by_op = {item["op"]: item for item in result["operations"]}
        self.assertEqual(by_op["tone.exposure"]["unit"], "EV")
        self.assertEqual(by_op["color.temperature"]["mode"], "delta")
        self.assertEqual(by_op["geometry.crop_aspect"]["value"], [3, 2])
        self.assertEqual(result["coverage"]["unsupported"], 1)
        self.assertIn("Check histogram for clipping", result["guardrails"])

    def test_absolute_and_delta_kelvin_are_distinct(self):
        delta = compile_recipe(
            "A.RAF", "standard", "A", "B",
            {"white_balance_and_color": ["Kelvin +300"]}, "", "raw")
        absolute = compile_recipe(
            "A.RAF", "standard", "A", "B",
            {"white_balance_and_color": ["Kelvin 5200"]}, "", "raw")
        self.assertEqual(delta["operations"][0]["mode"], "delta")
        self.assertEqual(absolute["operations"][0]["mode"], "absolute")

    def test_rejects_out_of_range_values(self):
        with self.assertRaisesRegex(RecipeCompileError, "outside"):
            compile_recipe(
                "A.RAF", "standard", "A", "B",
                {"global_exposure": ["Exposure +20"]}, "", "raw")

    def test_levels_point_is_not_compiled_as_hdr_percentage(self):
        result = compile_recipe(
            "A.RAF", "standard", "A", "B",
            {"hdr_levels_curves": ["White Point 245", "Black Point 8"]},
            "", "raw")
        self.assertEqual(
            [item["op"] for item in result["operations"]],
            ["levels.white_input", "levels.black_input"],
        )

    def test_compiles_three_styles_per_checkpoint_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "checkpoint.json"
            entry = {"photo": "A.JPG", "guardrails": "Preserve subject."}
            for style in ("standard", "signature", "creative"):
                entry[f"{style}_title"] = style
                entry[f"{style}_intent"] = "intent"
                entry[f"{style}_recipe"] = json.dumps(self.sample())
            path.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
            corpus = compile_checkpoint(path)
            self.assertEqual(corpus["format"], CORPUS_FORMAT)
            self.assertEqual(corpus["recipe_count"], 3)
            self.assertEqual(len(corpus["source_checkpoint_sha256"]), 64)

    def test_personal_style_compiles_for_raw_and_jpeg_sources(self):
        for source_kind, photo in (("raw", "A.RAF"), ("jpeg", "A.JPG")):
            with self.subTest(source_kind=source_kind):
                result = compile_recipe(
                    photo, "personal", "Personal finish",
                    "Apply the learned style carefully.", self.sample(),
                    "Preserve the subject.", source_kind)
                self.assertEqual(result["style"], "personal")
                self.assertEqual(result["source_kind"], source_kind)

    def test_ranges_and_named_colour_channels_compile_from_model_prose(self):
        result = compile_recipe(
            "A.RAF", "personal", "Personal", "Intent", {
                "global_exposure": [
                    "Set Contrast about +12 to +22 and Saturation about +5 to +12."],
                "white_balance_and_color": [
                    "Cool the global white balance approximately -200 to -600 Kelvin."],
                "color_editor": [
                    "Increase blue/cyan saturation about 5-12 and lower luminance about 0-8."],
            }, "", "raw")
        operations = result["operations"]
        by_op = {item["op"]: item for item in operations
                 if item["op"] != "color.hsl_range"}
        self.assertEqual(by_op["tone.contrast"]["value"], 17)
        self.assertEqual(by_op["color.saturation"]["value"], 8.5)
        self.assertEqual(by_op["color.temperature"]["value"], -400)
        hsl = [item for item in operations if item["op"] == "color.hsl_range"]
        self.assertEqual(
            [(item["channel"], item["component"], item["value"]) for item in hsl],
            [("blue/teal", "saturation", 8.5),
             ("blue/teal", "lightness", -4)],
        )

    def test_checkpoint_includes_personal_recipe_when_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "checkpoint.json"
            entry = {"photo": "A.JPG", "guardrails": "Preserve subject."}
            for style in ("standard", "signature", "creative", "personal"):
                entry[f"{style}_title"] = style
                entry[f"{style}_intent"] = "intent"
                entry[f"{style}_recipe"] = json.dumps(self.sample())
            path.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
            corpus = compile_checkpoint(path)
            self.assertEqual(corpus["recipe_count"], 4)
            self.assertEqual(
                [item["style"] for item in corpus["recipes"]],
                ["standard", "signature", "creative", "personal"],
            )



if __name__ == "__main__":
    unittest.main()


class LevelsScaleTests(unittest.TestCase):
    """One sentence from a live run, read three ways wrong."""

    SENTENCE = ("Set Levels black input around 4, white input 98, "
                "midpoint 0.98, with output black near 1 and white 99.")

    def compile(self, sentence: str) -> dict:
        result = compile_recipe(
            "A.RAF", "signature", "", "",
            json.dumps({"hdr_levels_curves": [sentence]}), "", "raw")
        return {op["op"].split(".")[1]: op["value"]
                for op in result["operations"]}

    def test_a_white_point_in_percent_is_not_a_level(self):
        # Read as level 98 of 255 this brightened a frame eight-fold and
        # blew it to white. Ninety-eight per cent of full scale is what
        # the sentence means and is very nearly no change at all.
        self.assertAlmostEqual(
            self.compile(self.SENTENCE)["white_input"], 249.9, places=1)

    def test_an_output_point_is_not_an_input_point(self):
        # "output black near 1 and white 99" is two output points; the
        # word governs both, though it sits beside only the first.
        values = self.compile(self.SENTENCE)
        self.assertNotIn(99.0, values.values())
        self.assertAlmostEqual(values["black_input"], 4.0, places=1)

    def test_a_point_written_after_its_name_is_still_read(self):
        # "white input 98" and "input white 98" are the same instruction.
        self.assertEqual(
            self.compile("Levels: input white 240, input black 12."),
            self.compile("Levels: white input 240, black input 12."))

    def test_a_level_above_the_percent_range_is_taken_as_a_level(self):
        self.assertEqual(self.compile("Set white point 250.")["white_input"],
                         250.0)

    def test_an_explicit_percentage_is_honoured_on_either_point(self):
        values = self.compile("Levels input black 5%, input white 96%.")
        self.assertAlmostEqual(values["black_input"], 12.75, places=2)
        self.assertAlmostEqual(values["white_input"], 244.8, places=1)


class InfraredGrammarTests(unittest.TestCase):
    """The two instructions infrared presets are written with."""

    def compile(self, sections):
        return compile_recipe("A.ARW", PRESET_STYLE, "IR", "for infrared",
                              sections, "", "raw")

    def test_neutralise_asks_for_the_channels_to_agree(self):
        recipe = self.compile({"white_balance_and_color": ["Neutralise"]})
        self.assertEqual(
            [(item["op"], item["value"]) for item in recipe["operations"]],
            [("color.neutralize", 100.0)])

    def test_the_american_spelling_reads_the_same(self):
        recipe = self.compile({"white_balance_and_color": ["Neutralize 60"]})
        self.assertEqual(recipe["operations"][0]["value"], 60.0)

    def test_it_does_not_swallow_an_ordinary_temperature(self):
        recipe = self.compile(
            {"white_balance_and_color": ["Temperature +400 kelvin"]})
        self.assertEqual(recipe["operations"][0]["op"], "color.temperature")

    def test_swapping_red_and_blue_compiles_to_the_matrix(self):
        recipe = self.compile({"color_editor": ["Swap red and blue channels"]})
        self.assertEqual(len(recipe["operations"]), 1)
        self.assertEqual(recipe["operations"][0]["op"], "color.channel_mixer")
        self.assertEqual(recipe["operations"][0]["value"],
                         [[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]])

    def test_the_other_two_swaps_are_understood(self):
        for words, expected in (
            ("Swap red and green", "red-green"),
            ("Swap green and blue", "green-blue"),
        ):
            with self.subTest(words):
                recipe = self.compile({"color_editor": [words]})
                self.assertEqual(recipe["operations"][0]["value"],
                                 CHANNEL_SWAPS[expected])

    def test_an_ordinary_colour_instruction_is_not_read_as_a_swap(self):
        recipe = self.compile({"color_editor": ["Blue saturation +10"]})
        self.assertEqual(recipe["operations"][0]["op"], "color.hsl_range")


class GreyMixGrammarTests(unittest.TestCase):
    """Writing a channel mix in the same words the presets are written in."""

    def compile(self, sections):
        return compile_recipe("A.ARW", PRESET_STYLE, "IR", "for infrared",
                              sections, "", "raw")

    def test_a_monochrome_mix_normalises_to_one(self):
        recipe = self.compile(
            {"color_editor": ["Monochrome mix: red 50, green 40, blue 10"]})
        matrix = recipe["operations"][0]["value"]
        self.assertEqual(recipe["operations"][0]["op"], "color.channel_mixer")
        self.assertEqual(matrix[0], [0.5, 0.4, 0.1])
        self.assertEqual(matrix[0], matrix[1])
        self.assertEqual(matrix[1], matrix[2])

    def test_weights_that_do_not_add_to_a_hundred_still_normalise(self):
        recipe = self.compile(
            {"color_editor": ["Grey mix red 2, green 1, blue 1"]})
        self.assertEqual(recipe["operations"][0]["value"][0], [0.5, 0.25, 0.25])

    def test_it_does_not_swallow_an_ordinary_colour_instruction(self):
        recipe = self.compile({"color_editor": ["Red saturation +10"]})
        self.assertEqual(recipe["operations"][0]["op"], "color.hsl_range")

    def test_a_mix_missing_a_channel_is_not_guessed_at(self):
        recipe = self.compile({"color_editor": ["Monochrome mix: red 60"]})
        self.assertNotEqual(
            [item["op"] for item in recipe["operations"]],
            ["color.channel_mixer"])


class UnparseablePointTests(LevelsScaleTests):
    """A point the prose never numbered must change nothing."""

    def test_a_numberless_white_point_reads_as_neutral(self):
        # The live sentence from the Quiet Cyan Atmosphere suggestion:
        # "Levels" named, no number anywhere. Compiled as white input 0
        # it divided a night sky by nothing and previewed as a sheet
        # of white. The reading that changes the photograph least is
        # 255 -- exactly no change at all.
        values = self.compile(
            "Set exposure, HDR, Levels, and the contrast-restoring "
            "Luma curve; keep the white and black relationship gentle.")
        self.assertGreaterEqual(values.get("white_input", 255.0), 254.0)

    def test_the_hdr_quartet_is_sliders_not_levels_points(self):
        # The exact sentence behind Quiet Cyan Atmosphere: Capture
        # One's HDR quartet with signed ranges. "White 0 to +5" is a
        # slider nudge, and read as an absolute white input of zero it
        # divided a night sky by nothing.
        values = self.compile(
            "HDR Highlight -10 to -20, Shadow +8 to +16, "
            "White 0 to +5, Black -5 to -12.")
        self.assertNotIn("white_input", values)
        self.assertIn("white", values)      # tone.white, a delta
        self.assertIn("highlight", values)
        self.assertAlmostEqual(values["white"], 2.5, places=1)


class DegenerateWhiteInputTests(unittest.TestCase):
    """The engine refuses to divide a frame by nothing."""

    def test_a_zero_white_input_is_neutral_not_ruin(self):
        import numpy as np

        from development_engine import _apply_global

        held = np.full((24, 24, 3), 0.2, np.float32)
        out = _apply_global(held, [{
            "op": "levels.white_input", "unit": "level-8bit",
            "mode": "absolute", "value": 0.0, "enabled": True}])
        self.assertTrue(np.allclose(out, held, atol=1e-4))
