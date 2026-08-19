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
    def test_more_examples_than_one_call_can_carry_are_split(self):
        """A vision call takes eight images; a photographer may choose more."""
        import json as json_module

        from style_profile_kernel import (
            batch_paths,
            batch_request,
            style_example_batches,
        )

        request = json_module.dumps({
            "format": "opencull-style-profile-request-v1",
            "photos_root": "/x",
            "examples": [
                {"path": f"/x/{n}.jpg", "sha256": "z"} for n in range(11)],
            "existing_profile": None, "signature": "s"})

        batches = style_example_batches(request, 8)

        self.assertEqual([len(batch_paths(b)) for b in batches], [8, 3])
        # Each pass carries its own examples, and the profile built so far.
        first = json_module.loads(batch_request(request, batches[0]))
        self.assertEqual(len(first["examples"]), 8)
        self.assertIsNone(first["existing_profile"])
        carried = json_module.dumps({"profile": {"profile_name": "so far"}})
        second = json_module.loads(
            batch_request(request, batches[1], carried))
        self.assertEqual(len(second["examples"]), 3)
        self.assertEqual(
            second["existing_profile"], {"profile_name": "so far"})

    def test_request_accepts_kimiya_numeric_limit_for_selection_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
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
            root = Path(temporary).resolve()
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
