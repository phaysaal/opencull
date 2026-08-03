import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from edit_suggestion_kernel import (
    RECIPE_SECTIONS,
    build_edit_report,
    build_edit_request,
    edit_direction_json,
    edit_direction_needs_validation,
    edit_direction_repair_prompt,
    mark_edit_direction_validation,
    normalize_edit_direction,
    parse_edit_candidates,
    rejected_edit_direction_json,
    valid_edit_direction,
    valid_edit_report,
)


class EditSuggestionKernelTests(unittest.TestCase):
    def make_context(self, root: Path):
        photos = root / "photos"
        photos.mkdir()
        Image.new("RGB", (800, 600), "navy").save(photos / "A.JPG")
        shortlist = root / "shortlist.json"
        shortlist.write_text(json.dumps({
            "format": "opencull-professional-shortlist-v1",
            "source_report_sha256": "report",
            "candidate_signature": "candidates",
            "entries": [{
                "rank": 1, "photo": "A.JPG", "tier": "strong", "score": 88,
                "rationale": "Good moment.", "assessment": {},
                "raw_files": ["A.RAF"],
            }],
        }), encoding="utf-8")
        review = root / "shortlist.review.json"
        review.write_text(json.dumps({
            "format": "opencull-professional-review-v1",
            "source_report_sha256": "report",
            "candidate_signature": "candidates",
            "revision": 3,
            "entries": {
                "A.JPG": {
                    "tier": "strong", "interesting": True,
                    "edit_raw": True, "reviewed": True, "note": "Warm moment.",
                },
            },
        }), encoding="utf-8")
        return photos, shortlist, review

    def test_selected_photos_become_bound_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidates = parse_edit_candidates(request)
            self.assertEqual([item["photo"] for item in candidates], ["A.JPG"])
            self.assertEqual(json.loads(request)["review_revision"], 3)

    def test_three_distinct_directions_form_valid_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidate = parse_edit_candidates(request)[0]
            value = {
                "scene_reading": "A quiet blue-hour portrait.",
                "standard_title": "Clean editorial",
                "standard_intent": "Natural polish.",
                "standard_instructions": "Balance exposure and retain skin.",
                "signature_title": "Indigo warmth",
                "signature_intent": "Warm subject against cool context.",
                "signature_instructions": "Use restrained split color.",
                "creative_title": "Evening cinema",
                "creative_intent": "Tasteful cinematic atmosphere.",
                "creative_instructions": "Shape light without crushing detail.",
                "guardrails": "Preserve expression and believable skin.",
                "confidence": 0.86,
            }
            recipe = json.dumps({
                section: [f"Apply a restrained {section} adjustment."]
                for section in RECIPE_SECTIONS
            })
            value.update({
                "standard_recipe": recipe,
                "signature_recipe": recipe,
                "creative_recipe": recipe,
            })
            direction = normalize_edit_direction(value)
            self.assertTrue(valid_edit_direction(direction))
            entries = [edit_direction_json(direction, candidate)]
            report = build_edit_report(request, entries, "professional")
            self.assertTrue(valid_edit_report(
                report, request, [candidate]))

    def test_normalizer_repairs_missing_final_recipe_array_bracket(self):
        recipe = json.dumps({
            section: [f"Apply a restrained {section} adjustment."]
            for section in RECIPE_SECTIONS
        })
        truncated = recipe[:-2] + recipe[-1]
        value = {
            "scene_reading": "A documentary family scene.",
            "standard_title": "Natural",
            "standard_intent": "Preserve the moment.",
            "standard_instructions": "Balance the photograph.",
            "standard_recipe": recipe,
            "signature_title": "Authored",
            "signature_intent": "Add restrained character.",
            "signature_instructions": "Shape tone and color.",
            "signature_recipe": recipe,
            "creative_title": "Expressive",
            "creative_intent": "Interpret the atmosphere.",
            "creative_instructions": "Use careful local contrast.",
            "creative_recipe": truncated,
            "personal_title": "Personal",
            "personal_intent": "Apply learned preferences.",
            "personal_instructions": "Adapt the style to the scene.",
            "personal_recipe": truncated,
            "guardrails": "Preserve faces, expression, and context.",
            "confidence": "High confidence in the scene reading.",
        }
        direction = normalize_edit_direction(value)
        self.assertEqual(direction["confidence"], 0.9)
        self.assertEqual(
            set(json.loads(direction["creative_recipe"])),
            set(RECIPE_SECTIONS),
        )
        self.assertTrue(valid_edit_direction(direction))

    def test_repair_prompt_preserves_malformed_payload_and_contract(self):
        malformed = {
            "scene_reading": "A quiet portrait.",
            "signature_recipe": '{"composition":["Crop carefully."}',
            "confidence": "high",
        }
        prompt = edit_direction_repair_prompt(malformed)
        self.assertIn("A quiet portrait.", prompt)
        self.assertIn("Crop carefully", prompt)
        self.assertIn("Confidence must be a number", prompt)
        self.assertIn("format-only repair", edit_direction_repair_prompt.__doc__)

    def test_repaired_entry_is_auditable(self):
        entry = edit_direction_json(
            {"scene_reading": "Preserved."},
            {"photo": "A.JPG"},
            True,
        )
        self.assertEqual(entry["photo"], "A.JPG")
        self.assertTrue(entry["format_repaired"])

    def test_targeted_request_and_kimiya_rejection_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, shortlist, review = self.make_context(root)
            request = json.loads(build_edit_request(
                str(shortlist), str(review), str(photos), "", "A.JPG"))
            self.assertEqual(request["photos"], ["A.JPG"])
            self.assertEqual(request["only_photo"], "A.JPG")
        marked = mark_edit_direction_validation(
            {"photo": "A.JPG", "scene_reading": "Preserved."}, False)
        self.assertEqual(marked["kimiya_validation"]["status"], "rejected")
        self.assertIn(
            "regenerated independently",
            marked["kimiya_validation"]["message"],
        )

    def test_invalid_generation_is_a_reportable_per_photo_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidate = parse_edit_candidates(request)[0]
            entry = rejected_edit_direction_json(candidate, True)
            self.assertFalse(edit_direction_needs_validation(entry))
            self.assertEqual(entry["kimiya_validation"]["status"], "rejected")
            report = build_edit_report(request, [entry], "professional")
            self.assertTrue(valid_edit_report(report, request, [candidate]))

    def test_report_records_personal_style_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, shortlist, review = self.make_context(root)
            style = root / "style.json"
            style.write_text(json.dumps({
                "format": "opencull-personal-style-profile-v1",
                "profile": {"profile_name": "Family color"},
            }), encoding="utf-8")
            request = build_edit_request(
                str(shortlist), str(review), str(photos), str(style))
            candidate = parse_edit_candidates(request)[0]
            entry = rejected_edit_direction_json(candidate)
            report = json.loads(build_edit_report(
                request, [entry], "professional"))
            self.assertEqual(
                report["style_profile"]["profile_name"], "Family color")
            self.assertEqual(
                report["style_profile_sha256"],
                json.loads(request)["style_profile"]["sha256"])
