import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from renderer_comparison import (
    compare_renderers,
    image_metrics,
    render_full_resolution_pair,
)


class RendererComparisonTests(unittest.TestCase):
    def recipe(self):
        return {
            "format": "opencull-development-recipe-v1",
            "source_photo": "A.JPG", "source_kind": "jpeg",
            "style": "standard", "operations": [
                {"op": "tone.exposure", "value": 0.2, "mode": "delta"},
                {"op": "color.saturation", "value": 8, "mode": "delta"},
            ],
            "diagnostics": [],
        }

    def test_metrics_report_luminance_clipping_and_detail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.jpg"
            Image.new("RGB", (20, 10), (80, 120, 160)).save(path)
            metrics = image_metrics(path)
            self.assertEqual((metrics["width"], metrics["height"]), (20, 10))
            self.assertIn("mean_luminance", metrics)
            self.assertIn("sharpness_laplacian_variance", metrics)

    def test_comparison_preserves_three_honestly_labelled_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "A.JPG"
            Image.new("RGB", (48, 32), (75, 110, 145)).save(source)

            def fake_darktable(photo, output_dir, **kwargs):
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / "A.darktable-default.jpg"
                Image.new("RGB", (48, 32), (85, 120, 150)).save(output)
                return {
                    "format": "opencull-darktable-render-v1",
                    "output": {"path": str(output), "sha256": "fake"},
                    "engine": {"name": "darktable", "version": "5.6.0"},
                    "recipe_execution": {"mode": "native-control"},
                }

            with patch("renderer_comparison.render_darktable_default", fake_darktable):
                result = compare_renderers(
                    source, source, self.recipe(), root / "comparison")

            self.assertTrue(Path(result["visual_comparison"]["path"]).is_file())
            self.assertTrue(Path(result["report_path"]).is_file())
            self.assertEqual(
                set(result["renders"]),
                {"opencull", "darktable_native", "darktable_guided"})
            self.assertIn("does not yet claim", result["research_notice"])
            saved = json.loads(Path(result["report_path"]).read_text())
            self.assertEqual(saved["format"], "opencull-renderer-comparison-v1")

    def test_full_resolution_pair_does_not_request_a_dimension_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "A.RAF"
            reference = root / "A.JPG"
            source.write_bytes(b"raw fixture")
            Image.new("RGB", (96, 64), (70, 100, 130)).save(reference)
            calls = {}

            def fake_darktable(photo, output_dir, **kwargs):
                calls.update(kwargs)
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / "A.darktable-markesteijn-3-pass.jpg"
                Image.new("RGB", (96, 64), (80, 110, 140)).save(output)
                provenance = output.with_suffix(".render.json")
                provenance.write_text("{}\n")
                return {
                    "format": "opencull-darktable-render-v1",
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "output": {"path": str(output), "sha256": "native"},
                    "engine": {"name": "darktable", "version": "5.6.0",
                               "demosaic": {"requested": "markesteijn-3-pass"}},
                }

            def fake_render(baseline, recipe, output_dir, **kwargs):
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / "A.guided.jpg"
                Image.new("RGB", (96, 64), (85, 115, 145)).save(output)
                return {
                    "created_at": "2026-08-01T00:00:01+00:00",
                    "recipe": recipe,
                    "output": {"path": str(output), "sha256": "guided"},
                }

            with patch("renderer_comparison.render_darktable_default", fake_darktable), \
                    patch("renderer_comparison.render_recipe", fake_render):
                result = render_full_resolution_pair(
                    source, reference, self.recipe(), root / "exports",
                    demosaic_mode="markesteijn-3-pass")

            self.assertIsNone(calls["max_dimension"])
            self.assertEqual(result["dimensions"]["darktable_native"], [96, 64])
            self.assertEqual(result["dimensions"]["darktable_guided"], [96, 64])
            self.assertTrue(Path(result["report_path"]).is_file())
            self.assertIn(
                "darktable-full-resolution",
                result["renders"]["darktable_guided"]["recipe"]["engine_chain"])


if __name__ == "__main__":
    unittest.main()
