import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from semantic_verifier import (
    SemanticVerificationError,
    normalize_judgment,
    verify_consensus,
    verify_triplet,
)


class SemanticVerifierTests(unittest.TestCase):
    def test_normalizes_fenced_json(self):
        value = normalize_judgment("```json\n" + json.dumps({
            "satisfactory": True, "confidence": .9,
            "follows_suggestion": .8, "preserves_subject": .9,
            "preserves_composition": .7, "photographic_quality": .8,
            "concerns": [], "reasoning": "Fits the requested direction.",
        }) + "\n```")
        self.assertTrue(value["satisfactory"])
        self.assertEqual(value["confidence"], .9)

    def test_triplet_payload_and_certificate_are_hashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original, thumb, developed = (root / name for name in ("a.jpg", "a-thumb.jpg", "b.jpg"))
            for path, color in ((original, "red"), (thumb, "red"), (developed, "blue")):
                Image.new("RGB", (8, 8), color).save(path)
            captured = {}

            def transport(endpoint, key, payload):
                captured.update(payload=payload, endpoint=endpoint, key=key)
                return json.dumps({
                    "satisfactory": True, "confidence": .85,
                    "follows_suggestion": .9, "preserves_subject": .95,
                    "preserves_composition": .8, "photographic_quality": .82,
                    "concerns": [], "reasoning": "The result follows the intent.",
                })

            result = verify_triplet(original, developed, "Natural warm documentary edit.",
                                    thumb, api_key="ignored", transport=transport)
            self.assertEqual(result["format"], "opencull-semantic-verification-v1")
            self.assertEqual(set(result["evidence"]), {"original", "thumbnail", "developed"})
            self.assertEqual(captured["payload"]["temperature"], 0)
            self.assertGreater(len(captured["payload"]["messages"][0]["content"]), 4)

    def test_rejects_missing_credential_without_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original = root / "a.jpg"
            developed = root / "b.jpg"
            Image.new("RGB", (2, 2)).save(original)
            Image.new("RGB", (2, 2)).save(developed)
            with self.assertRaisesRegex(SemanticVerificationError, "credential"):
                verify_triplet(original, developed, "edit", api_key="")

    def test_consensus_requires_majority_and_retains_judgments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original, developed = root / "a.jpg", root / "b.jpg"
            Image.new("RGB", (2, 2), "red").save(original)
            Image.new("RGB", (2, 2), "blue").save(developed)

            def transport(endpoint, key, payload):
                model = payload["model"]
                satisfactory = model != "model-b"
                return json.dumps({
                    "satisfactory": satisfactory, "confidence": .8,
                    "follows_suggestion": .8, "preserves_subject": .9,
                    "preserves_composition": .9, "photographic_quality": .8,
                    "concerns": [] if satisfactory else ["dark face"],
                    "reasoning": model,
                })

            result = verify_consensus(original, developed, "edit",
                                      models=["model-a", "model-b", "model-c"],
                                      api_key="ignored", transport=transport)
            self.assertTrue(result["judgment"]["satisfactory"])
            self.assertEqual(result["judgment"]["votes_satisfactory"], 2)
            self.assertEqual(len(result["judgments"]), 3)
            self.assertEqual(result["judgment"]["concerns"], ["dark face"])


if __name__ == "__main__":
    unittest.main()
