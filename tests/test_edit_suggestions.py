import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from edit_suggestion_kernel import (
    RECIPE_SECTIONS,
    build_edit_report,
    build_edit_request,
    chosen_style,
    edit_candidate_path,
    edit_direction_json,
    edit_direction_needs_validation,
    edit_direction_prompt,
    edit_direction_repair_prompt,
    inert_treatments,
    mark_edit_direction_validation,
    normalize_edit_direction,
    parse_edit_candidates,
    rejected_edit_direction_json,
    valid_edit_direction,
    valid_edit_report,
    valid_style_choice,
    validation_failures,
    viewed_as,
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
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidates = parse_edit_candidates(request)
            self.assertEqual([item["photo"] for item in candidates], ["A.JPG"])
            self.assertEqual(json.loads(request)["review_revision"], 3)

    def test_three_distinct_directions_form_valid_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
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
                section: [f"Apply a restrained {section} adjustment: Exposure +0.10."]
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
            section: [f"Apply a restrained {section} adjustment: Exposure +0.10."]
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
            root = Path(temporary).resolve()
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
            root = Path(temporary).resolve()
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
            root = Path(temporary).resolve()
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


class WhatTheModelSeesTests(EditSuggestionKernelTests):
    """The image judged and the image edited have to be the same image."""

    def test_a_folder_with_no_decoder_falls_back_to_the_camera_rendering(self):
        # The fixture folder has no project layout or report, so no
        # workspace can be built: the honest answer is the camera's own
        # rendering, said out loud on the entry rather than assumed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidate = parse_edit_candidates(request)[0]
            self.assertEqual(candidate["photos_root"], str(photos.resolve()))
            shown = edit_candidate_path(request, candidate)
            self.assertEqual(Path(shown).name, "A.JPG")
            self.assertEqual(viewed_as(candidate), "camera rendering")
            entry = edit_direction_json({}, candidate)
            self.assertEqual(entry["viewed"], "camera rendering")

    def test_the_prompt_says_which_rendering_is_on_the_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidate = parse_edit_candidates(request)[0]
            self.assertIn(
                "camera's own rendering",
                edit_direction_prompt(candidate, "professional"))

    def test_a_baseline_is_shown_and_named_when_one_can_be_rendered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidate = parse_edit_candidates(request)[0]
            baseline = root / "neutral.jpg"
            Image.new("RGB", (32, 24), (90, 92, 88)).save(baseline)
            with mock.patch(
                    "edit_suggestion_kernel.baseline_view",
                    return_value=baseline):
                self.assertEqual(
                    edit_candidate_path(request, candidate), str(baseline))
                self.assertEqual(viewed_as(candidate), "calibrated baseline")
                prompt = edit_direction_prompt(candidate, "professional")
            self.assertIn("neutral rendering of the RAW", prompt)
            self.assertIn("the exact image", prompt)


class InertAnswerTests(EditSuggestionKernelTests):
    """An answer that renders nothing is not a treatment."""

    def recipe_of(self, step: str) -> str:
        return json.dumps({section: [step] for section in RECIPE_SECTIONS})

    def direction(self, step: str) -> dict:
        value = {
            "scene_reading": "A quiet blue-hour portrait.",
            "standard_title": "Clean editorial",
            "standard_intent": "Natural polish.",
            "standard_instructions": "Balance exposure.",
            "signature_title": "Indigo warmth",
            "signature_intent": "Warm against cool.",
            "signature_instructions": "Restrained split color.",
            "creative_title": "Evening cinema",
            "creative_intent": "Cinematic atmosphere.",
            "creative_instructions": "Shape light.",
            "guardrails": "Preserve expression.",
            "confidence": 0.8,
            "personal_style_choice": 0,
        }
        for style in ("standard", "signature", "creative"):
            value[f"{style}_recipe"] = self.recipe_of(step)
        return normalize_edit_direction(value)

    def test_a_recipe_that_compiles_to_nothing_is_refused(self):
        """The exact answer one live frame came back with."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidate = parse_edit_candidates(request)[0]
            empty = self.direction(
                "No source recommendation was provided; make no change.")
            self.assertEqual(
                inert_treatments(empty),
                ["standard", "signature", "creative"])
            self.assertFalse(valid_edit_direction(empty, candidate))
            self.assertIn(
                "would render the photograph unchanged",
                " ".join(validation_failures(empty, candidate)))

    def test_a_recipe_with_a_real_adjustment_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidate = parse_edit_candidates(request)[0]
            real = self.direction("Set Exposure +0.15 and Contrast +12.")
            self.assertEqual(inert_treatments(real), [])
            self.assertTrue(valid_edit_direction(real, candidate))

    def test_an_absent_personal_treatment_is_honest_not_inert(self):
        # The personal treatment is optional. Leaving it out is fine;
        # writing one that does nothing is not.
        real = self.direction("Set Exposure +0.15.")
        self.assertEqual(inert_treatments(real), [])
        real["personal_recipe"] = self.recipe_of(
            "No source recommendation was provided; make no change.")
        self.assertEqual(inert_treatments(real), ["personal"])

    def test_the_prompt_asks_for_an_empty_section_not_a_sentence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            prompt = edit_direction_prompt(
                parse_edit_candidates(request)[0], "professional")
            self.assertIn("a complete treatment, not", prompt)
            self.assertIn("empty list", prompt)
            self.assertIn("never a sentence saying", prompt)

    def test_the_prompt_asks_a_treatment_to_account_for_its_own_effect(self):
        """Highlight recovery and shadow lift both flatten a photograph.

        Seven treatments written without this line all finished flatter
        than the frames they started from -- every one of them.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            prompt = edit_direction_prompt(
                parse_edit_candidates(request)[0], "professional")
            self.assertIn("finishes flatter and duller", prompt)
            self.assertIn("put the contrast back deliberately", prompt)
            self.assertIn("hold\nat least the contrast and colour", prompt)


class PersonalStyleChoiceTests(EditSuggestionKernelTests):
    """A photographer has several tastes; each photograph asks for one."""

    def styles(self, root: Path, *names: str) -> list[str]:
        paths = []
        for index, name in enumerate(names):
            path = root / f"style-{index}.json"
            path.write_text(json.dumps({
                "format": "opencull-personal-style-profile-v1",
                "profile": {
                    "profile_name": name,
                    "visual_signature": f"how {name} looks",
                    "scene_adaptation": f"when {name} suits a scene",
                    "color_preferences": f"which colours {name} leads with",
                    "saturation_preferences": f"how far {name} pushes colour",
                    "avoid_or_guardrails": f"what {name} never does"},
            }), encoding="utf-8")
            paths.append(str(path))
        return paths

    def request_with(self, root: Path, *names: str):
        photos, shortlist, review = self.make_context(root)
        request = build_edit_request(
            str(shortlist), str(review), str(photos),
            style_profiles=json.dumps(self.styles(root, *names)))
        return request, parse_edit_candidates(request)[0]

    def direction(self, choice, reason="the light suits it"):
        recipe = json.dumps({
            section: [f"Apply a restrained {section} adjustment: Exposure +0.10."]
            for section in RECIPE_SECTIONS})
        return normalize_edit_direction({
            "scene_reading": "A quiet blue-hour portrait.",
            "standard_title": "Clean editorial",
            "standard_intent": "Natural polish.",
            "standard_instructions": "Balance exposure.",
            "signature_title": "Indigo warmth",
            "signature_intent": "Warm against cool.",
            "signature_instructions": "Restrained split color.",
            "creative_title": "Evening cinema",
            "creative_intent": "Cinematic atmosphere.",
            "creative_instructions": "Shape light.",
            "guardrails": "Preserve expression.",
            "confidence": 0.86,
            "standard_recipe": recipe,
            "signature_recipe": recipe,
            "creative_recipe": recipe,
            "personal_title": "Its own hand",
            "personal_intent": "Speak in the chosen style.",
            "personal_instructions": "Push cyan, hold terracotta.",
            "personal_recipe": recipe,
            "personal_style_choice": choice,
            "personal_style_reason": reason,
        })

    def test_every_style_is_offered_to_every_photograph(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _request, candidate = self.request_with(
                root, "Coastal twilight", "Heritage street", "Child portrait")
            offered = candidate["style_profiles"]
            self.assertEqual(len(offered), 3)
            names = [item["profile"]["profile_name"] for item in offered]
            self.assertEqual(names[0], "Coastal twilight")
            # The menu the model reads names each style and how it looks.
            menu = edit_direction_prompt(candidate, "professional")
            self.assertIn("Heritage street", menu)
            self.assertIn("how Child portrait looks", menu)
            # A name is a label; what the look is and when it suits a
            # scene is the evidence a choice can rest on.
            self.assertIn("when Heritage street suits a scene", menu)
            self.assertIn("personal_style_choice", menu)

    def test_a_style_arrives_with_the_detail_needed_to_write_in_it(self):
        """A signature says which style; the rest says how to speak in it.

        An earlier menu carried the signature alone, and the personal
        treatments came out generic: the photographer's own words about
        which colours to lead with and which to hold back were in the
        profile and never reached the model meant to be using them.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _request, candidate = self.request_with(root, "Heritage street")
            menu = edit_direction_prompt(candidate, "professional")
            self.assertIn("which colours Heritage street leads with", menu)
            self.assertIn("how far Heritage street pushes colour", menu)
            self.assertIn("what Heritage street never does", menu)

    def test_the_chosen_style_is_resolved_to_the_file_it_came_from(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            request, candidate = self.request_with(
                root, "Coastal twilight", "Heritage street")
            direction = self.direction(2, "stone village in hard midday sun")
            self.assertTrue(valid_edit_direction(direction, candidate))
            entry = edit_direction_json(direction, candidate)
            self.assertEqual(
                entry["personal_style"]["profile_name"], "Heritage street")
            self.assertEqual(
                entry["personal_style"]["reason"],
                "stone village in hard midday sun")
            self.assertEqual(entry["personal_style"]["offered"], 2)
            # And the run records every style it could have spoken in.
            report = json.loads(build_edit_report(
                request, [entry], "professional"))
            self.assertEqual(
                [item["profile_name"] for item in report["style_profiles"]],
                ["Coastal twilight", "Heritage street"])
            self.assertTrue(valid_edit_report(
                build_edit_report(request, [entry], "professional"),
                request, [candidate]))

    def test_a_style_nobody_offered_is_not_a_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _request, candidate = self.request_with(root, "Coastal twilight")
            for invented in (2, 0, -1, 99):
                self.assertFalse(
                    valid_edit_direction(self.direction(invented), candidate),
                    f"choice {invented} was accepted against one style")
            self.assertIsNone(chosen_style(self.direction(7), candidate))

    def test_no_styles_offered_means_the_only_honest_choice_is_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, shortlist, review = self.make_context(root)
            request = build_edit_request(
                str(shortlist), str(review), str(photos))
            candidate = parse_edit_candidates(request)[0]
            self.assertTrue(valid_style_choice(self.direction(0), candidate))
            self.assertFalse(valid_style_choice(self.direction(1), candidate))
            self.assertIn("no personal style profile",
                          edit_direction_prompt(candidate, "professional"))

    def test_an_unreadable_choice_is_no_choice_rather_than_the_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _request, candidate = self.request_with(root, "Coastal twilight")
            direction = self.direction("the first one")
            self.assertEqual(direction["personal_style_choice"], -1)
            self.assertFalse(valid_edit_direction(direction, candidate))
