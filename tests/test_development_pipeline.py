import inspect
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
            root = Path(temporary).resolve()
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
            root = Path(temporary).resolve()
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
            root = Path(temporary).resolve()
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


class NativeDecodeCacheTests(unittest.TestCase):
    """The demosaic is the expensive part, and it does not depend on size."""

    def workspace(self, root: Path):
        from opencull_gui.development import DevelopmentWorkspace
        from opencull_gui.project import ensure_project_layout
        from opencull_gui.raw_sources import RawSourceStore

        photos = root / "photos"
        photos.mkdir(parents=True, exist_ok=True)
        (photos / "A.RAF").write_bytes(b"FUJIFILMCCD-RAW" + b"\x00" * 4096)
        layout = ensure_project_layout(photos)
        project_path = root / "project.json"
        create_project(project_path, name="decodes", source_folder=photos)

        class Report:
            path = layout["Reports"] / "shoot-results.json"
            photo_names = ("A.RAF",)

        workspace = DevelopmentWorkspace(
            project_path, layout,
            RawSourceStore(layout["Operations"] / "raw-sources.json", Report()),
            decoders={"darktable"})
        return workspace, photos

    def test_one_demosaic_serves_every_size_asked_for_after_it(self):
        from unittest import mock

        from opencull_gui import development

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workspace, photos = self.workspace(root)
            source = photos / "A.RAF"
            decoded = []

            def fake_decode(src, output_dir, *, max_dimension=None, **kw):
                decoded.append(max_dimension)
                output_dir.mkdir(parents=True, exist_ok=True)
                written = output_dir / "native.jpg"
                edge = max_dimension or 4000
                Image.new("RGB", (edge, edge * 2 // 3), (80, 100, 120)).save(
                    written)
                return {"output": {"path": str(written)}}

            with mock.patch.object(
                    development, "render_darktable_default", fake_decode):
                first = workspace._native_decode(
                    source, source.stat(), 320, "markesteijn-3-pass", root)
                self.assertEqual(decoded, [development.NATIVE_DECODE_FLOOR],
                                 "a thumbnail should decode generously, once")
                for size in (320, 900, 1600, development.NATIVE_DECODE_FLOOR):
                    workspace._native_decode(
                        source, source.stat(), size, "markesteijn-3-pass", root)
                self.assertEqual(
                    len(decoded), 1,
                    "every size at or below the decode must be resampled "
                    f"from it, but it decoded again: {decoded}")

                # Only something larger than anything held pays again.
                workspace._native_decode(
                    source, source.stat(), 4096, "markesteijn-3-pass", root)
                self.assertEqual(decoded, [development.NATIVE_DECODE_FLOOR, 4096])

            # What came back is the size that was asked for; what was kept
            # is the generous decode the rest are taken from.
            with Image.open(first) as returned:
                self.assertEqual(max(returned.size), 320)
            kept = sorted(
                (workspace.project_layout["Previews"] / "DevelopNative").glob(
                    "A.markesteijn-3-pass.*.jpg"))
            self.assertEqual(len(kept), 2)
            with Image.open(kept[0]) as held:
                self.assertIn(
                    max(held.size),
                    {development.NATIVE_DECODE_FLOOR, 4096})

    def test_a_smaller_ask_comes_back_at_the_size_it_asked_for(self):
        from unittest import mock

        from opencull_gui import development

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workspace, photos = self.workspace(root)
            source = photos / "A.RAF"

            def fake_decode(src, output_dir, *, max_dimension=None, **kw):
                output_dir.mkdir(parents=True, exist_ok=True)
                written = output_dir / "native.jpg"
                edge = max_dimension or 4000
                Image.new("RGB", (edge, edge * 2 // 3), (80, 100, 120)).save(
                    written)
                return {"output": {"path": str(written)}}

            with mock.patch.object(
                    development, "render_darktable_default", fake_decode):
                workspace._native_decode(
                    source, source.stat(), 2048, "markesteijn-3-pass", root)
                smaller = workspace._native_decode(
                    source, source.stat(), 640, "markesteijn-3-pass", root)
            with Image.open(smaller) as image:
                self.assertEqual(max(image.size), 640)


class RendererRevisionTests(unittest.TestCase):
    """A better renderer must reach the frames already rendered."""

    def test_a_proof_is_keyed_to_the_renderer_that_made_it(self):
        from development_engine import RECIPE_ENGINE_REVISION
        from opencull_gui import development

        source = inspect.getsource(development.DevelopmentWorkspace.recipe_preview)
        self.assertIn("RECIPE_ENGINE_REVISION", source)
        self.assertIsInstance(RECIPE_ENGINE_REVISION, int)


if __name__ == "__main__":
    unittest.main()
