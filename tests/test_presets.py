"""Presets: a stated look, and one the photographer kept.

These exercise the two halves against each other -- what ships, and what
gets saved -- rather than asserting on the text of the module. A preset
that compiles but renders nothing, or one that carries a crop onto a
frame it was never made for, would pass a shape check and damage a
photograph.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui import presets  # noqa: E402


def operation(op: str, value, **extra) -> dict:
    return {"op": op, "value": value, "unit": "percent", "mode": "delta",
            **extra}


class BuiltInTests(unittest.TestCase):
    """What ships has to be executable, and has to say something."""

    def test_every_built_in_compiles_to_operations(self):
        made = presets.built_in()
        self.assertEqual(len(made), len(presets.BUILT_IN))
        for item in made:
            with self.subTest(item["name"]):
                self.assertTrue(item["operations"], item["name"])
                self.assertTrue(item["intent"].strip())
                self.assertEqual(item["origin"], "built-in")
                self.assertTrue(item["id"].startswith("preset-"))

    def test_names_and_identifiers_are_distinct(self):
        made = presets.built_in()
        self.assertEqual(len({item["id"] for item in made}), len(made))
        self.assertEqual(len({item["name"] for item in made}), len(made))

    def test_an_instruction_the_compiler_cannot_read_is_a_failure_here(self):
        """Better a broken build than a preset that quietly does less."""
        with mock.patch.object(presets, "BUILT_IN", ({
            "slug": "nonsense", "name": "Nonsense", "intent": "x",
            "instructions": {"global_exposure": ["make it feel nostalgic"]},
        },)):
            with self.assertRaises(presets.PresetError):
                presets.built_in()

    def test_the_red_filter_darkens_blue_before_the_colour_is_removed(self):
        """A filter, not a tint: the order is the whole difference.

        Moving each colour family's brightness while there is still colour
        to move is what makes the grey come out different. After the
        desaturation those moves would land on a frame that has no blue
        left in it and do nothing at all.
        """
        found = next(item for item in presets.built_in()
                     if item["id"] == "preset-monochrome-red-filter")
        ops = found["operations"]
        blue = next(index for index, item in enumerate(ops)
                    if item["op"] == "color.hsl_range"
                    and item.get("channel") == "blue")
        grey = next(index for index, item in enumerate(ops)
                    if item["op"] == "color.saturation")
        self.assertLess(blue, grey)
        self.assertEqual(ops[grey]["value"], -100.0)


class InfraredPresetTests(unittest.TestCase):
    """The looks for a filter on the front of the lens.

    Infrared is where the preset idea earns itself: the treatment is the
    same for every frame behind a given filter, and no model needs to be
    asked what a 760nm cut-off does.
    """

    def preset(self, name: str) -> dict:
        return next(item for item in presets.built_in()
                    if item["id"] == f"preset-infrared-{name}")

    def ops(self, name: str) -> list[str]:
        return [item["op"] for item in self.preset(name)["operations"]]

    def test_every_infrared_preset_neutralises_before_anything_else(self):
        """The cast has to go first or every later move works on it."""
        for name in ("760-mono", "850-mono", "720-false-colour", "720-mono"):
            with self.subTest(name):
                self.assertEqual(self.ops(name)[0], "color.neutralize")

    def test_the_deep_filters_are_monochrome_because_the_light_is(self):
        """Past 760nm the three channels record the same thing."""
        for name in ("760-mono", "850-mono", "720-mono"):
            with self.subTest(name):
                grey = next(
                    item for item in self.preset(name)["operations"]
                    if item["op"] == "color.saturation")
                self.assertEqual(grey["value"], -100.0)

    def test_false_colour_swaps_the_channels_and_keeps_the_colour(self):
        operations = self.preset("720-false-colour")["operations"]
        swap = next(item for item in operations
                    if item["op"] == "color.channel_mixer")
        self.assertEqual(swap["value"],
                         [[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]])
        self.assertNotIn(
            -100.0, [item["value"] for item in operations
                     if item["op"] == "color.saturation"])

    def test_the_swap_happens_after_the_neutralising(self):
        """Swapping a cast only moves the cast to another channel."""
        ops = self.ops("720-false-colour")
        self.assertLess(ops.index("color.neutralize"),
                        ops.index("color.channel_mixer"))

    def test_the_deeper_filter_lifts_harder(self):
        """850nm reaches the sensor as far less light than 760nm does."""
        deep = {item["op"]: item["value"]
                for item in self.preset("850-mono")["operations"]}
        shallow = {item["op"]: item["value"]
                   for item in self.preset("760-mono")["operations"]}
        self.assertGreater(deep.get("tone.exposure", 0),
                           shallow.get("tone.exposure", 0))


class SavingTests(unittest.TestCase):
    """A look the photographer kept, and what travels with it."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve() / "Presets"
        self.addCleanup(self._temporary.cleanup)

    def test_saving_then_reading_gives_the_same_look_back(self):
        ops = [operation("tone.contrast", 12.0),
               operation("color.saturation", -8.0)]
        kept = presets.save("My grade", ops, root=self.root)
        found = presets.saved(self.root)
        self.assertEqual([item["id"] for item in found], [kept["id"]])
        self.assertEqual(found[0]["operations"], ops)
        self.assertEqual(found[0]["origin"], "saved")

    def test_a_crop_does_not_travel_to_another_photograph(self):
        """The one thing about a treatment that is only about its frame."""
        ops = [
            operation("geometry.crop_aspect", [16, 9], unit="ratio"),
            operation("geometry.rotation", 1.4, unit="degree"),
            operation("tone.contrast", 10.0),
        ]
        kept = presets.save("Wide look", ops, root=self.root)
        self.assertEqual([item["op"] for item in kept["operations"]],
                         ["tone.contrast"])

    def test_a_version_that_is_only_a_crop_cannot_become_a_preset(self):
        with self.assertRaises(presets.PresetError):
            presets.save("Just a crop", [
                operation("geometry.crop_aspect", [1, 1], unit="ratio")],
                root=self.root)

    def test_a_preset_needs_a_name(self):
        with self.assertRaises(presets.PresetError):
            presets.save("   ", [operation("tone.contrast", 4.0)],
                         root=self.root)

    def test_saving_the_same_look_twice_does_not_make_two(self):
        ops = [operation("tone.contrast", 12.0)]
        first = presets.save("Grade", ops, root=self.root)
        second = presets.save("Grade", list(ops), root=self.root)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(presets.saved(self.root)), 1)

    def test_the_photographers_own_come_before_the_shipped_ones(self):
        presets.save("Mine", [operation("tone.contrast", 3.0)],
                     root=self.root)
        listed = presets.presets(self.root)
        self.assertEqual(listed[0]["origin"], "saved")
        self.assertEqual(
            {item["origin"] for item in listed[1:]}, {"built-in"})

    def test_a_damaged_file_is_skipped_rather_than_fatal(self):
        presets.save("Good", [operation("tone.contrast", 3.0)],
                     root=self.root)
        (self.root / "broken.json").write_text("{ not json", encoding="utf-8")
        (self.root / "empty.json").write_text(
            json.dumps({"format": presets.PRESET_FORMAT, "operations": []}),
            encoding="utf-8")
        self.assertEqual([item["name"] for item in presets.saved(self.root)],
                         ["Good"])

    def test_only_the_photographers_own_can_be_forgotten(self):
        kept = presets.save("Mine", [operation("tone.contrast", 3.0)],
                            root=self.root)
        self.assertFalse(presets.forget("preset-monochrome", root=self.root))
        self.assertTrue(presets.forget(kept["id"], root=self.root))
        self.assertEqual(presets.saved(self.root), [])
        self.assertTrue(any(item["id"] == "preset-monochrome"
                            for item in presets.presets(self.root)))

    def test_where_it_was_kept_from_is_recorded(self):
        kept = presets.save(
            "From a frame", [operation("tone.contrast", 3.0)],
            origin_note={"photo": "DSCF9240.RAF", "treatment": "personal"},
            root=self.root)
        self.assertEqual(kept["kept_from"]["photo"], "DSCF9240.RAF")

    def test_a_named_folder_is_where_they_go(self):
        with mock.patch.dict(
            "os.environ", {presets.PRESETS_ENVIRONMENT: str(self.root)}
        ):
            self.assertEqual(presets.presets_dir(), self.root)


class RecipeTests(unittest.TestCase):
    """A preset, as the thing the renderer is handed."""

    def test_a_preset_becomes_a_recipe_for_this_photograph(self):
        found = next(item for item in presets.built_in()
                     if item["id"] == "preset-monochrome")
        recipe = presets.recipe_for(found, "A.RAF", "raw")
        self.assertEqual(recipe["source_photo"], "A.RAF")
        self.assertEqual(recipe["source_kind"], "raw")
        self.assertEqual(recipe["operations"], found["operations"])
        self.assertEqual(recipe["guardrails"], [])

    def test_the_recipe_cannot_be_edited_through_the_preset(self):
        found = next(item for item in presets.built_in()
                     if item["id"] == "preset-monochrome")
        recipe = presets.recipe_for(found, "A.RAF", "raw")
        recipe["operations"][0]["value"] = 999
        self.assertNotEqual(found["operations"][0]["value"], 999)


if __name__ == "__main__":
    unittest.main()


class FilmSpiritTests(unittest.TestCase):
    """The spirit collection: known characters, honest names."""

    NAMES = ("Vivid Slide", "Gentle Slide", "Muted Chrome",
             "Negative Portrait", "Crisp Negative",
             "Nostalgic Negative", "Cinema Flat", "Silver Film")

    def test_every_spirit_ships_compiled_and_labelled(self):
        made = {item["name"]: item for item in presets.built_in()}
        for name in self.NAMES:
            self.assertIn(name, made)
            look = made[name]
            # Every one says plainly what it is: an approximation in
            # the spirit of a named simulation, not the thing itself.
            self.assertIn("spirit of Fujifilm", look["intent"])
            self.assertIn("approximation", look["intent"])
            self.assertTrue(look["operations"])
            for op in look["operations"]:
                self.assertTrue(op.get("op"))

    def test_the_spirits_move_in_their_simulations_direction(self):
        made = {item["name"]: item for item in presets.built_in()}

        def value(name: str, op: str) -> float:
            return next(
                (float(item["value"])
                 for item in made[name]["operations"]
                 if item["op"] == op), 0.0)

        # The slide saturates, the cinema stock desaturates, the
        # negative relaxes its contrast, the silver drops its colour.
        self.assertGreater(value("Vivid Slide", "color.saturation"), 20)
        self.assertLess(value("Cinema Flat", "color.saturation"), -15)
        self.assertLess(value("Negative Portrait", "tone.contrast"), 0)
        self.assertEqual(value("Silver Film", "color.saturation"), -100)


class PortableMaskTests(unittest.TestCase):
    """A preset carries the masks that describe a scene, not a bitmap."""

    def shaped_mask(self, shape: str, **value) -> dict:
        return {"op": f"mask.{shape}", "unit": "mask", "mode": "absolute",
               "enabled": True, "value": {
                   "anchor": f"{shape} gradient", "opacity": 1.0,
                   "feather": 1.0, **value,
                   "effects": [{"op": "tone.exposure", "value": 0.3,
                               "unit": "EV", "mode": "delta"}]}}

    def test_luma_radial_linear_and_color_masks_travel(self):
        operations = [
            self.shaped_mask("luma"), self.shaped_mask("radial"),
            self.shaped_mask("linear"), self.shaped_mask("color"),
        ]
        kept = {item["op"] for item in
               presets.portable_operations(operations)}
        self.assertEqual(
            kept, {"mask.luma", "mask.radial", "mask.linear",
                  "mask.color"})

    def test_a_painted_mask_does_not_travel(self):
        operations = [
            self.shaped_mask("luma"),
            self.shaped_mask("brush", map="BASE64BITMAPDATA=="),
        ]
        kept = presets.portable_operations(operations)
        self.assertEqual([item["op"] for item in kept], ["mask.luma"])
        # The bitmap itself never even risks leaving with it.
        self.assertNotIn("BASE64BITMAPDATA==", json.dumps(kept))

    def test_a_kept_preset_never_carries_a_painted_mask(self):
        operations = [
            {"op": "tone.exposure", "value": 0.2, "unit": "EV",
             "mode": "delta", "enabled": True},
            self.shaped_mask("brush", map="STROKES"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            kept = presets.save("Brushed", operations,
                                root=Path(folder))
        ops = {item["op"] for item in kept["operations"]}
        self.assertIn("tone.exposure", ops)
        self.assertNotIn("mask.brush", ops)
