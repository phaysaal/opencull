import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from style_profile_kernel import (
    build_style_request,
    style_profile_json,
    valid_style_profile,
)


class StyleProfileTests(unittest.TestCase):
    def test_request_accepts_kimiya_numeric_limit_for_selection_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "a.jpg"
            second = root / "b.jpg"
            Image.new("RGB", (8, 8), "teal").save(first)
            Image.new("RGB", (8, 8), "orange").save(second)
            manifest = root / "examples.json"
            manifest.write_text(json.dumps({
                "format": "opencull-style-examples-v1",
                "files": [str(first), str(second)],
            }), encoding="utf-8")

            request = json.loads(build_style_request(str(manifest), limit=1.0))

            self.assertEqual(len(request["examples"]), 1)
            self.assertEqual(
                Path(request["examples"][0]["path"]), first.resolve())

    def test_request_hashes_examples_and_profile_updates_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (8, 8), "teal").save(root / "a.jpg")
            request = build_style_request(str(root))
            fields = {
                "profile_name": "My style", "visual_signature": "Vivid cinematic travel.",
                "tonal_preferences": "Deep blacks.", "contrast_preferences": "Strong contrast.",
                "color_preferences": "Cyan and warm separation.", "white_balance_preferences": "Cool shadows.",
                "saturation_preferences": "High but intentional.", "highlight_shadow_preferences": "Protect highlights.",
                "texture_detail_preferences": "Crisp detail.", "composition_preferences": "Environmental context.",
                "subject_and_skin_preferences": "Preserve believable skin.", "scene_adaptation": "Adapt to scene.",
                "preferred_adjustments": "Contrast and color separation.", "avoid_or_guardrails": "No clipping.",
                "professional_refinement": "Keep the signature, remove technical flaws.", "confidence": .9,
            }
            output = json.loads(style_profile_json(request, fields, "replace"))
            self.assertEqual(output["revision"], 1)
            self.assertTrue(output["examples"][0]["sha256"])
            self.assertEqual(output["history"][0]["profile"]["profile_name"], "My style")
            self.assertTrue(valid_style_profile(output))


if __name__ == "__main__":
    unittest.main()
