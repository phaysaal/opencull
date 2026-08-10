import json
import tempfile
import unittest
from pathlib import Path

import opencull_kernel as kernel

GROUP = {
    "id": "group-0001",
    "candidates": [
        {
            "name": "A.CR3",
            "captured": "2026-01-01T12:00:00",
            "technical_score": 88.0,
            "sharpness": 92.0,
            "exposure": 80.0,
            "contrast": 75.0,
            "clipping": 0.1,
            "composition_proxy": 70.0,
        },
        {
            "name": "B.CR3",
            "captured": "2026-01-01T12:00:01",
            "technical_score": 82.0,
            "sharpness": 84.0,
            "exposure": 85.0,
            "contrast": 72.0,
            "clipping": 0.2,
            "composition_proxy": 68.0,
        },
    ],
}


class KernelTests(unittest.TestCase):
    def test_manifest_validation(self):
        manifest = json.dumps({"groups": [GROUP]})
        self.assertEqual(kernel.parse_manifest(manifest), [GROUP])

    def test_recommendation_must_use_known_unique_names(self):
        valid = {"keepers": "A.CR3, B.CR3", "rationale": "best pair"}
        valid_array = {"keepers": ["A.CR3", "B.CR3"], "rationale": "best pair"}
        unknown = {"keepers": "A.CR3, X.CR3", "rationale": "no"}
        repeated = {"keepers": "A.CR3, A.CR3", "rationale": "no"}
        empty = {"keepers": "", "rationale": "all frames are unusably blurred"}
        irrelevant = {
            "keepers": "",
            "rationale": "These museum photographs contain no family members.",
        }
        self.assertTrue(kernel.valid_recommendation(valid, GROUP, 2))
        self.assertTrue(kernel.valid_recommendation(valid_array, GROUP, 2))
        self.assertTrue(kernel.valid_recommendation(empty, GROUP, 2))
        self.assertFalse(kernel.valid_recommendation(irrelevant, GROUP, 2))
        self.assertFalse(kernel.valid_recommendation(unknown, GROUP, 2))
        self.assertFalse(kernel.valid_recommendation(repeated, GROUP, 2))

    def test_empty_selection_emits_quality_warning(self):
        record = {
            "keepers": "none",
            "rationale": "Every frame is severely blurred.",
            "confidence": 0.9,
        }
        decision = kernel.decision_json(record, GROUP, 2)
        report = json.loads(kernel.build_report("abc", [GROUP], [decision]))
        self.assertEqual(report["keep"][0]["photos"], [])
        self.assertEqual(report["warnings"][0]["cluster_id"], "group-0001")

    def test_undecodable_photographs_reach_the_report(self):
        manifest = json.dumps({
            "errors": [
                {"name": "BROKEN.RAF", "error": "RuntimeError: decode failed"},
            ],
        })
        decision = kernel.decision_json(
            {"keepers": "A.JPG", "rationale": "Sharpest frame.",
             "confidence": 0.9}, GROUP, 2)
        report = json.loads(
            kernel.build_report("abc", [GROUP], [decision], "{}", manifest))
        self.assertEqual(
            [item["name"] for item in report["scan_errors"]], ["BROKEN.RAF"])
        self.assertIn("could not be read", report["notice"])

    def test_a_clean_scan_reports_no_unreadable_photographs(self):
        decision = kernel.decision_json(
            {"keepers": "A.JPG", "rationale": "Sharpest frame.",
             "confidence": 0.9}, GROUP, 2)
        report = json.loads(
            kernel.build_report("abc", [GROUP], [decision], "{}",
                                json.dumps({"errors": []})))
        self.assertEqual(report["scan_errors"], [])
        self.assertNotIn("could not be read", report["notice"])

    def test_scan_errors_tolerates_a_malformed_manifest(self):
        for manifest in ("", "not json", "[]", json.dumps({"errors": "no"})):
            self.assertEqual(kernel.scan_errors(manifest), [])

    def test_invalid_curator_falls_back_to_conservative_preservation(self):
        invalid = {
            "keepers": "",
            "rationale": "No relatives appear in this travel photograph.",
            "confidence": 1.0,
        }
        normalized = kernel.normalize_recommendation(invalid, GROUP, 1)
        self.assertTrue(normalized["_fallback"])
        self.assertEqual(normalized["keepers"], "A.CR3")
        self.assertTrue(kernel.valid_recommendation(normalized, GROUP, 1))

    def test_group_reading_requires_every_photo_and_all_layers(self):
        reading = dict.fromkeys(kernel.READING_FIELDS, "A.CR3 and B.CR3: assessed")
        reading["confidence"] = 0.8
        self.assertTrue(kernel.valid_group_reading(reading, GROUP))
        reading["eyes_and_gaze"] = ""
        self.assertFalse(kernel.valid_group_reading(reading, GROUP))
        normalized = kernel.normalize_group_reading(reading, GROUP)
        self.assertTrue(kernel.valid_group_reading(normalized, GROUP))
        self.assertIn("Uncertain:", normalized["eyes_and_gaze"])
        self.assertLess(normalized["confidence"], 0.8)
        frame = json.loads(
            kernel.frame_reading_json(normalized, GROUP["candidates"][0]))
        self.assertEqual(frame["filename"], "A.CR3")

    def test_curation_prioritizes_hard_to_repair_human_moments(self):
        reading = dict.fromkeys(kernel.READING_FIELDS, "A.CR3 and B.CR3 differ")
        reading["confidence"] = 0.8
        prompt = kernel.curation_prompt(
            GROUP,
            [kernel.frame_reading_json(reading, GROUP["candidates"][0])],
            2,
            "family",
        )
        self.assertIn("Pose, expression, readiness", prompt)
        self.assertIn("small open-mouth smile", prompt)
        self.assertIn("ceiling, not a quota", prompt)
        self.assertIn("Never reject a photograph", prompt)

    def test_oversized_cluster_tournament_stays_within_image_limit(self):
        candidates = [
            {**GROUP["candidates"][0], "name": f"P{number:03d}.JPG"}
            for number in range(35)
        ]
        large = {"id": "large", "candidates": candidates}
        active = candidates
        rounds = kernel.tournament_round_count(large, 8, 4)
        self.assertGreater(rounds, 1)
        for round_number in range(rounds - 1):
            batches = kernel.candidate_batches(active, 8)
            self.assertTrue(all(1 <= len(batch) <= 8 for batch in batches))
            active = [
                candidate
                for batch in batches
                for candidate in batch[:min(4, len(batch))]
            ]
        self.assertLessEqual(len(active), 8)

    def test_eleven_photo_cluster_needs_shortlist_then_final(self):
        candidates = [
            {**GROUP["candidates"][0], "name": f"P{number:02d}.JPG"}
            for number in range(11)
        ]
        large = {"id": "large", "candidates": candidates}
        self.assertEqual(kernel.tournament_round_count(large, 8, 4), 2)
        self.assertEqual(
            [len(batch) for batch in kernel.candidate_batches(candidates, 8)],
            [8, 3],
        )
        invalid_empty = {
            "keepers": "",
            "rationale": "all photos have poor quality",
            "confidence": 0.5,
        }
        normalized = kernel.normalize_shortlist_recommendation(
            invalid_empty, kernel.group_with_candidates(large, candidates[:8]), 4)
        self.assertTrue(kernel.valid_shortlist_recommendation(
            normalized, kernel.group_with_candidates(large, candidates[:8]), 4))
        self.assertEqual(len(kernel._selected_names(normalized)), 4)

    def test_report_covers_groups_in_order(self):
        record = {"keepers": "A.CR3", "rationale": "sharpest", "confidence": 0.8}
        decision = kernel.decision_json(record, GROUP, 1)
        report = kernel.build_report("abc", [GROUP], [decision])
        self.assertTrue(kernel.report_covers_all_groups(report, [GROUP]))
        parsed = json.loads(report)
        self.assertEqual(
            parsed["clusters"],
            [{"cluster_id": "group-0001", "photos": ["A.CR3", "B.CR3"]}],
        )
        self.assertEqual(parsed["keep"][0]["photos"], ["A.CR3"])
        evidence = kernel.report_evidence(json.dumps({"groups": [GROUP]}), report)
        self.assertIn("DETERMINISTIC REPORT PROJECTION", evidence)
        self.assertIn('"all_selected_filenames_known": true', evidence)

    def test_singleton_is_kept_without_model_judgment(self):
        group = {"id": "solo", "candidates": [{"name": "only.JPG"}]}
        decision = json.loads(kernel.singleton_decision(group))
        self.assertEqual(decision["keepers"], ["only.JPG"])
        # The whole group rides in the decision, so a live watcher can say
        # which frames each landed decision covered.
        self.assertEqual(decision["photos"], ["only.JPG"])
        self.assertEqual(decision["confidence"], 1.0)
        self.assertTrue(kernel.all_singletons([group]))
        self.assertFalse(kernel.all_singletons([GROUP]))

    def test_the_evidence_projection_fits_the_judges_window(self):
        """Flags the claim names must never fall past the 6000-char cut."""
        groups = [
            {"id": f"group-{n:04d}", "candidates": [
                {"name": f"F{n:04d}A.JPG", "sharpness": 80.0,
                 "technical_score": 75.0, "exposure": 90.0,
                 "clipping": 0.1},
                {"name": f"F{n:04d}B.JPG", "sharpness": 60.0,
                 "technical_score": 65.0, "exposure": 88.0,
                 "clipping": 0.2},
            ]}
            for n in range(1, 89)
        ]
        manifest = json.dumps({"groups": groups})
        decisions = [json.dumps({
            "group_id": g["id"],
            "keepers": [g["candidates"][0]["name"]],
            "rationale": ("Chosen for its markedly better sharpness and "
                          "steadier framing across the whole burst." * 2),
            "confidence": 0.9,
        }) for g in groups]
        report = kernel.build_report("sha", groups, decisions, "{}", manifest)
        evidence = kernel.report_evidence(manifest, report)
        self.assertLessEqual(len(evidence), 6000)
        visible = evidence[:6000]
        for flag in ("zero_selection_ids", "every_singleton_keeps_its_photo",
                     "claims_files_deleted", "maximum_selected_in_any_group"):
            self.assertIn(flag, visible)
        # The flags come before the bulky sample, so a truncated read
        # still sees every named field.
        self.assertLess(
            visible.index("zero_selection_ids"),
            visible.index('"decisions"'))

    def test_checkpoint_resumes_only_matching_valid_prefix(self):
        second = {
            "id": "group-0002",
            "candidates": [{"name": "C.CR3"}],
        }
        groups = [GROUP, second]
        first = kernel.decision_json(
            {"keepers": "A.CR3", "rationale": "best pose", "confidence": 0.8},
            GROUP,
            1,
        )
        checkpoint = kernel.build_checkpoint(
            "manifest-1", groups, [first], 1, "family")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            path.write_text(checkpoint, encoding="utf-8")
            loaded = kernel.load_checkpoint(
                str(path), "manifest-1", groups, 1, "family", True)
            decisions = kernel.checkpoint_decisions(loaded, groups, 1)
            self.assertEqual(len(decisions), 1)
            self.assertEqual(kernel.remaining_groups(groups, decisions), [second])

            stale = kernel.load_checkpoint(
                str(path), "different-manifest", groups, 1, "family", True)
            self.assertEqual(
                kernel.checkpoint_decisions(stale, groups, 1), [])

    def test_checkpoint_rejects_unknown_keeper(self):
        bad = json.dumps({
            "group_id": GROUP["id"],
            "keepers": ["UNKNOWN.CR3"],
            "rationale": "invented",
        })
        checkpoint = kernel.build_checkpoint(
            "manifest-1", [GROUP], [bad], 1, "family")
        self.assertEqual(
            kernel.checkpoint_decisions(checkpoint, [GROUP], 1), [])

    def test_adaptive_plan_is_bounded_and_requires_consensus(self):
        relaxed = json.dumps({
            "direction": "relaxed",
            "target": "both",
            "confidence": 0.9,
        })
        plan = kernel.build_tuning_plan([relaxed], 14, 8)
        self.assertTrue(plan["changed"])
        self.assertEqual(plan["hash_distance"], 17)
        self.assertEqual(plan["time_window"], 10.0)

        stricter = json.dumps({
            "direction": "stricter",
            "target": "hash_distance",
            "confidence": 0.9,
        })
        conflict = kernel.build_tuning_plan([relaxed, stricter], 14, 8)
        self.assertFalse(conflict["changed"])
        audit = json.loads(kernel.tuning_audit(
            "before", "after", [relaxed], plan, True))
        self.assertTrue(audit["enabled"])
        self.assertTrue(audit["applied"])
        self.assertEqual(audit["proposed"]["direction"], "relaxed")

    def test_tuned_manifest_must_be_effective_and_preserve_identity(self):
        def manifest(groups):
            return json.dumps({
                "groups": groups,
                "source_directory": "/photos",
            })

        left = {"id": "a", "candidates": [{"name": "A.JPG"}]}
        right = {"id": "b", "candidates": [{"name": "B.JPG"}]}
        merged = {
            "id": "a",
            "candidates": [{"name": "A.JPG"}, {"name": "B.JPG"}],
        }
        initial = manifest([left, right])
        candidate = manifest([merged])
        plan = {"direction": "relaxed"}
        self.assertEqual(
            kernel.accept_tuned_manifest(initial, candidate, plan),
            candidate,
        )
        missing = manifest([left])
        self.assertEqual(
            kernel.accept_tuned_manifest(initial, missing, plan),
            initial,
        )


if __name__ == "__main__":
    unittest.main()
