"""Per-camera default looks: assigned once, worn under every render."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from opencull_gui import cameralooks

LOOK_OPS = [
    {"op": "color.channel_mixer", "unit": "matrix", "mode": "absolute",
     "value": [[0.95, 0.05, 0.0], [0.0, 1.0, 0.0], [0.0, 0.05, 0.95]],
     "enabled": True},
    {"op": "color.warp", "unit": "warp", "mode": "absolute",
     "value": {"size": 2, "lattice": "AAAA"}, "enabled": True},
]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_a_look_round_trips_by_camera_name(self):
        cameralooks.assign("FUJIFILM X-T5", LOOK_OPS, note="fitted",
                           root=self.root)
        found = cameralooks.look_for("FUJIFILM X-T5", root=self.root)
        self.assertEqual(found["camera"], "FUJIFILM X-T5")
        self.assertEqual(len(found["operations"]), 2)

    def test_the_name_is_forgiving_about_case_and_spacing(self):
        cameralooks.assign("FUJIFILM X-T5", LOOK_OPS, root=self.root)
        self.assertIsNotNone(
            cameralooks.look_for("fujifilm  x-t5", root=self.root))

    def test_every_operation_is_marked_as_the_cameras(self):
        cameralooks.assign("X-T5", LOOK_OPS, root=self.root)
        found = cameralooks.look_for("X-T5", root=self.root)
        self.assertTrue(all(item["camera_look"]
                            for item in found["operations"]))

    def test_an_undressed_camera_answers_none(self):
        self.assertIsNone(cameralooks.look_for("X-H2", root=self.root))
        self.assertIsNone(cameralooks.look_for(None, root=self.root))
        self.assertIsNone(cameralooks.look_for("", root=self.root))

    def test_assigning_again_replaces_whole(self):
        cameralooks.assign("X-T5", LOOK_OPS, root=self.root)
        cameralooks.assign("X-T5", LOOK_OPS[:1], root=self.root)
        found = cameralooks.look_for("X-T5", root=self.root)
        self.assertEqual(len(found["operations"]), 1)

    def test_forget_takes_the_look_away(self):
        cameralooks.assign("X-T5", LOOK_OPS, root=self.root)
        self.assertTrue(cameralooks.forget("X-T5", root=self.root))
        self.assertIsNone(cameralooks.look_for("X-T5", root=self.root))
        self.assertFalse(cameralooks.forget("X-T5", root=self.root))

    def test_cameras_lists_who_wears_what(self):
        cameralooks.assign("X-T5", LOOK_OPS, note="chart", root=self.root)
        cameralooks.assign("X100V", LOOK_OPS, root=self.root)
        told = cameralooks.cameras(root=self.root)
        self.assertEqual(sorted(item["camera"] for item in told),
                         ["X-T5", "X100V"])

    def test_a_look_needs_a_name_and_operations(self):
        with self.assertRaises(cameralooks.CameraLookError):
            cameralooks.assign("  ", LOOK_OPS, root=self.root)
        with self.assertRaises(cameralooks.CameraLookError):
            cameralooks.assign("X-T5", [], root=self.root)


class DressingTests(unittest.TestCase):
    def look(self):
        marked = [dict(item, camera_look=True) for item in LOOK_OPS]
        return {"format": cameralooks.LOOK_FORMAT, "camera": "X-T5",
                "operations": marked}

    def recipe(self):
        return {"style": "standard",
                "operations": [{"op": "tone.exposure", "value": 0.3}]}

    def test_the_look_lies_under_the_recipes_own_operations(self):
        dressed = cameralooks.underneath(self.recipe(), self.look())
        ops = [item["op"] for item in dressed["operations"]]
        self.assertEqual(ops, ["color.channel_mixer", "color.warp",
                               "tone.exposure"])

    def test_a_look_is_never_worn_twice(self):
        once = cameralooks.underneath(self.recipe(), self.look())
        twice = cameralooks.underneath(once, self.look())
        self.assertEqual(twice, once)

    def test_no_look_changes_nothing(self):
        recipe = self.recipe()
        self.assertEqual(cameralooks.underneath(recipe, None), recipe)

    def test_the_original_recipe_is_not_mutated(self):
        recipe = self.recipe()
        cameralooks.underneath(recipe, self.look())
        self.assertEqual(len(recipe["operations"]), 1)


class CameraModelTests(unittest.TestCase):
    def test_the_model_reads_from_a_jpegs_exif(self):
        from opencull_gui.scenes import camera_model

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "frame.jpg"
            exif = Image.Exif()
            exif[0x0110] = "FUJIFILM X-T5"
            Image.new("RGB", (32, 24), (90, 80, 70)).save(path, exif=exif)
            self.assertEqual(camera_model(path), "FUJIFILM X-T5")

    def test_a_frame_without_a_camera_answers_none(self):
        from opencull_gui.scenes import camera_model

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plain.jpg"
            Image.new("RGB", (32, 24), (90, 80, 70)).save(path)
            self.assertIsNone(camera_model(path))
            self.assertIsNone(camera_model(Path(folder) / "absent.jpg"))


class PortabilityTests(unittest.TestCase):
    def test_a_cameras_look_never_leaves_inside_a_preset(self):
        from opencull_gui import presets

        worn = [dict(LOOK_OPS[0], camera_look=True),
                {"op": "tone.exposure", "unit": "EV", "mode": "delta",
                 "value": 0.4, "enabled": True}]
        kept = presets.portable_operations(worn)
        self.assertEqual([item["op"] for item in kept], ["tone.exposure"])


class WornUnderEveryRenderTests(unittest.TestCase):
    """The workspace lays the look under whatever the recipe is."""

    def setUp(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:                          # pragma: no cover
            self.skipTest("PySide6 is not installed")
        self.application = QApplication.instance() or QApplication([])
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self._held = os.environ.get(cameralooks.LOOKS_ENVIRONMENT)
        os.environ[cameralooks.LOOKS_ENVIRONMENT] = str(
            self.root / "camera-looks")
        self.addCleanup(self._restore)
        photos = self.root / "photos"
        photos.mkdir(parents=True)
        exif = Image.Exif()
        exif[0x0110] = "FUJIFILM X-T5"
        for index, name in enumerate(("A.JPG", "B.JPG")):
            Image.new("RGB", (160, 120), (70 + index * 20, 100, 90)).save(
                photos / name, exif=exif)
        report = self.root / "shoot-results.json"
        report.write_text(json.dumps({
            "format": "opencull-report-v2", "manifest_sha256": "x",
            "clusters": [{"cluster_id": "group-0001",
                          "photos": ["A.JPG", "B.JPG"]}],
            "keep": [{"cluster_id": "group-0001", "photos": ["A.JPG"],
                      "rationale": "sharpest", "confidence": 0.8,
                      "warning": "", "fallback": False,
                      "photographic_assessment": []}],
            "warnings": [], "adaptive_clustering": {"enabled": False},
            "notice": "read only",
        }), encoding="utf-8")
        from opencull_gui.report import load_report
        from opencull_qt.develop import workspace_for

        self.workspace = workspace_for(
            load_report(report), photos, decoders=set())

    def _restore(self):
        if self._held is None:
            os.environ.pop(cameralooks.LOOKS_ENVIRONMENT, None)
        else:
            os.environ[cameralooks.LOOKS_ENVIRONMENT] = self._held

    def test_the_baseline_wears_the_cameras_look(self):
        cameralooks.assign("FUJIFILM X-T5", LOOK_OPS)
        recipe = self.workspace.compiled_recipe(
            "A.JPG", "calibrated", "darktable")
        ops = [item["op"] for item in recipe["operations"]]
        self.assertEqual(ops[:2], ["color.channel_mixer", "color.warp"])
        self.assertTrue(recipe["operations"][0]["camera_look"])

    def test_a_preset_wears_it_underneath_its_own_moves(self):
        cameralooks.assign("FUJIFILM X-T5", LOOK_OPS)
        preset = self.workspace.presets()[0]
        recipe = self.workspace.compiled_recipe(
            "A.JPG", preset["id"], "darktable")
        ops = [item["op"] for item in recipe["operations"]]
        self.assertEqual(ops[:2], ["color.channel_mixer", "color.warp"])
        self.assertGreater(len(ops), 2)     # the preset's own moves follow

    def test_the_dust_map_is_healed_first_under_even_the_look(self):
        from opencull_gui import dust

        cameralooks.assign("FUJIFILM X-T5", LOOK_OPS)
        dust.save_map({
            "format": dust.DUST_FORMAT,
            "photos": str(self.root / "photos"), "pattern": "*.JPG",
            "frames_read": 8,
            "spots": [{"x": 0.5, "y": 0.25, "r": 0.01,
                       "depth": 0.3, "seen": 1.0}]})
        recipe = self.workspace.compiled_recipe(
            "A.JPG", "calibrated", "darktable")
        ops = [item["op"] for item in recipe["operations"]]
        self.assertEqual(ops, ["heal.spots", "color.channel_mixer",
                               "color.warp"])
        self.assertTrue(recipe["operations"][0]["dust_map"])

    def test_an_undressed_camera_renders_exactly_as_before(self):
        recipe = self.workspace.compiled_recipe(
            "A.JPG", "calibrated", "darktable")
        self.assertEqual(recipe["operations"], [])


if __name__ == "__main__":
    unittest.main()
