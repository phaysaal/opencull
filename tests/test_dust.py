"""Dust: found by the one thing it cannot do -- move."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from opencull_gui import dust

WIDTH, HEIGHT = 640, 480
DUST_AT = ((420, 130, 5), (150, 300, 7))


def dusty_shoot(folder: Path, frames: int = 8,
                with_dust: bool = True) -> None:
    """Frames with fixed dust shadows and one moving dark subject."""
    rng = np.random.default_rng(3)
    for index in range(frames):
        base = np.zeros((HEIGHT, WIDTH, 3), np.float32)
        for y in range(HEIGHT):
            base[y] = (0.55 + 0.1 * np.sin(index / 3), 0.62,
                       0.75 - 0.2 * y / HEIGHT)
        base += rng.normal(0, 0.01, base.shape)
        image = Image.fromarray(
            (np.clip(base, 0, 1) * 255).astype(np.uint8))
        draw = ImageDraw.Draw(image)
        if with_dust:
            for (x, y, r) in DUST_AT:
                shade = tuple(int(c * 0.55)
                              for c in image.getpixel((x, y)))
                draw.ellipse((x - r, y - r, x + r, y + r), fill=shade)
        moving = 60 + index * 60
        draw.ellipse((moving - 6, 74, moving + 6, 86), fill=(20, 20, 20))
        image.save(folder / f"F{index:02d}.jpg", quality=95)


class SurveyTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_fixed_dust_is_found_and_the_mover_is_not(self):
        dusty_shoot(self.folder)
        report = dust.survey(self.folder, "*.jpg", edge=640)
        self.assertEqual(len(report["spots"]), 2)
        found = sorted((round(s["x"] * WIDTH), round(s["y"] * HEIGHT))
                       for s in report["spots"])
        wanted = sorted((x, y) for x, y, _r in DUST_AT)
        for (fx, fy), (wx, wy) in zip(found, wanted):
            self.assertLess(abs(fx - wx), 4)
            self.assertLess(abs(fy - wy), 4)
        # The moving subject crossed y=80; nothing may be pinned there.
        for spot in report["spots"]:
            self.assertGreater(abs(spot["y"] * HEIGHT - 80), 20)

    def test_a_clean_sensor_is_a_finding_too(self):
        dusty_shoot(self.folder, with_dust=False)
        report = dust.survey(self.folder, "*.jpg", edge=640)
        self.assertEqual(report["spots"], [])
        self.assertEqual(report["frames_read"], 8)

    def test_too_few_frames_cannot_vote(self):
        dusty_shoot(self.folder, frames=2)
        report = dust.survey(self.folder, "*.jpg", edge=640)
        self.assertEqual(report["spots"], [])
        self.assertIn("at least", report.get("note", ""))


class MapTests(unittest.TestCase):
    def report(self, folder: Path) -> dict:
        return {"format": dust.DUST_FORMAT, "photos": str(folder),
                "pattern": "*.jpg", "frames_read": 8,
                "spots": [{"x": 0.5, "y": 0.25, "r": 0.01,
                           "depth": 0.3, "seen": 1.0}]}

    def test_the_map_lives_beside_the_photographs(self):
        with tempfile.TemporaryDirectory() as folder:
            kept = dust.save_map(self.report(Path(folder)))
            self.assertEqual(kept.parent.name, ".darkimiya")
            loaded = dust.load_map(folder)
            self.assertEqual(loaded["spots"][0]["x"], 0.5)

    def test_an_absent_or_broken_map_reads_as_none(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(dust.load_map(folder))
            path = dust.map_path(folder)
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(dust.load_map(folder))

    def test_the_operation_carries_only_what_the_renderer_needs(self):
        operation = dust.heal_operation(self.report(Path(".")))
        self.assertEqual(operation["op"], "heal.spots")
        self.assertTrue(operation["dust_map"])
        self.assertEqual(operation["value"]["spots"],
                         [{"x": 0.5, "y": 0.25, "r": 0.01}])
        self.assertIsNone(dust.heal_operation(
            {"format": dust.DUST_FORMAT, "spots": []}))

    def test_the_heal_comes_first_and_is_never_worn_twice(self):
        recipe = {"operations": [{"op": "tone.exposure", "value": 0.3}]}
        dressed = dust.underneath(recipe, self.report(Path(".")))
        self.assertEqual([item["op"] for item in dressed["operations"]],
                         ["heal.spots", "tone.exposure"])
        again = dust.underneath(dressed, self.report(Path(".")))
        self.assertEqual(again, dressed)
        self.assertEqual(dust.underneath(recipe, None), recipe)

    def test_the_map_never_leaves_inside_a_preset(self):
        from opencull_gui import presets

        worn = dust.underneath(
            {"operations": [{"op": "tone.exposure", "unit": "EV",
                             "mode": "delta", "value": 0.3,
                             "enabled": True}]},
            self.report(Path(".")))
        kept = presets.portable_operations(worn["operations"])
        self.assertEqual([item["op"] for item in kept],
                         ["tone.exposure"])


class KernelTests(unittest.TestCase):
    """The words the Kimiya program speaks."""

    def test_the_survey_speaks_json_for_the_act_to_write(self):
        import dust_kernel

        with tempfile.TemporaryDirectory() as folder:
            dusty_shoot(Path(folder))
            told = dust_kernel.survey_dust(folder, "*.jpg", 640)
            self.assertIsInstance(told, str)
            self.assertTrue(dust_kernel.map_valid(told))
            self.assertIn("dust spot", dust_kernel.map_note(told))
            parsed = json.loads(told)
            self.assertEqual(len(parsed["spots"]), 2)

    def test_a_map_that_could_not_vote_fails_the_check(self):
        import dust_kernel

        self.assertFalse(dust_kernel.map_valid("{}"))
        self.assertFalse(dust_kernel.map_valid("not json"))
        self.assertFalse(dust_kernel.map_valid(json.dumps({
            "format": dust.DUST_FORMAT, "frames_read": 2, "spots": []})))
        self.assertFalse(dust_kernel.map_valid(json.dumps({
            "format": dust.DUST_FORMAT, "frames_read": 8,
            "spots": [{"x": 2.0, "y": 0.5, "r": 0.01}]})))
        # A clean sensor on record is a valid finding.
        self.assertTrue(dust_kernel.map_valid(json.dumps({
            "format": dust.DUST_FORMAT, "frames_read": 8, "spots": []})))

    def test_map_home_prepares_the_house_folder(self):
        import dust_kernel

        with tempfile.TemporaryDirectory() as folder:
            where = Path(dust_kernel.map_home(folder))
            self.assertTrue(where.parent.is_dir())
            self.assertEqual(where.name, "dust-map.json")

    def test_the_program_is_in_the_catalogue(self):
        from opencull_gui.programs import BUILT_INS

        self.assertIn("dust_removal.kim",
                      [name for name, _purpose in BUILT_INS])


if __name__ == "__main__":
    unittest.main()
