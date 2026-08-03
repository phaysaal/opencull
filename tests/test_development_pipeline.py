import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from development_pipeline import run_pipeline
from opencull_gui.project import create_project, load_project


class DevelopmentPipelineTests(unittest.TestCase):
    def test_jpeg_guided_treatment_registers_photo_specific_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_folder = root / "photos"
            source_folder.mkdir()
            photo = source_folder / "A.JPG"
            Image.new("RGB", (40, 30), (90, 120, 150)).save(photo)
            directions = root / "directions.json"
            directions.write_text(json.dumps({
                "entries": [{
                    "photo": "A.JPG", "guardrails": "Preserve the subject.",
                    "standard_title": "Natural finish",
                    "standard_intent": "Keep the photograph believable.",
                    "standard_recipe": json.dumps({
                        "global_exposure": ["Exposure +0.2"],
                    }),
                }],
            }), encoding="utf-8")
            project = root / "project.json"
            create_project(project, "Test", source_folder)

            result = run_pipeline(
                photo, photo, directions, "A.JPG", "standard",
                root / "developed", project)

            self.assertTrue(Path(result["output"]["path"]).is_file())
            renders = load_project(project)["artifacts"]["renders"]
            artifact = next(item for item in renders if item["variant"] == "standard")
            self.assertEqual(artifact["source_photo"], "A.JPG")
            self.assertEqual(artifact["variant"], "standard")
            calibrated = next(item for item in renders if item["variant"] == "calibrated")
            self.assertTrue(Path(calibrated["path"]).is_file())

    def test_jpeg_personal_treatment_is_renderable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_folder = root / "photos"
            source_folder.mkdir()
            photo = source_folder / "A.JPG"
            Image.new("RGB", (40, 30), (90, 120, 150)).save(photo)
            directions = root / "directions.json"
            directions.write_text(json.dumps({
                "entries": [{
                    "photo": "A.JPG", "guardrails": "Preserve the subject.",
                    "personal_title": "Learned family finish",
                    "personal_intent": "Apply the learned style carefully.",
                    "personal_recipe": json.dumps({
                        "global_exposure": ["Exposure +0.2"],
                    }),
                }],
            }), encoding="utf-8")
            project = root / "project.json"
            create_project(project, "Test", source_folder)

            result = run_pipeline(
                photo, photo, directions, "A.JPG", "personal",
                root / "developed", project)

            self.assertTrue(Path(result["output"]["path"]).is_file())
            renders = load_project(project)["artifacts"]["renders"]
            artifact = next(item for item in renders if item["variant"] == "personal")
            self.assertEqual(artifact["variant"], "personal")

    def test_calibrated_treatment_renders_without_an_ai_recipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_folder = root / "photos"
            source_folder.mkdir()
            photo = source_folder / "A.JPG"
            Image.new("RGB", (40, 30), (90, 120, 150)).save(photo)
            directions = root / "directions.json"
            directions.write_text(json.dumps({
                "entries": [{"photo": "A.JPG"}],
            }), encoding="utf-8")
            project = root / "project.json"
            create_project(project, "Test", source_folder)

            result = run_pipeline(
                photo, photo, directions, "A.JPG", "calibrated",
                root / "developed", project)

            self.assertTrue(Path(result["output"]["path"]).is_file())
            self.assertEqual(result["recipe"]["style"], "calibrated")
            renders = load_project(project)["artifacts"]["renders"]
            self.assertEqual([item["variant"] for item in renders], ["calibrated"])


if __name__ == "__main__":
    unittest.main()
