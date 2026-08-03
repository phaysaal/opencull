import json
import tempfile
import unittest
from pathlib import Path

from recipe_compiler import (
    CORPUS_FORMAT,
    IR_FORMAT,
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
            path = Path(temporary) / "checkpoint.json"
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
            path = Path(temporary) / "checkpoint.json"
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
