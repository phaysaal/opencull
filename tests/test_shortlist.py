import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from opencull_gui.assets import AssetError, index_asset_families
from opencull_gui.jobs import JobManager
from opencull_gui.photos import PhotoStore
from opencull_gui.report import load_report
from opencull_gui.reviews import ReviewStore
from opencull_gui.server import ReviewServer
from opencull_gui.shortlist import (
    ASSESSMENT_FIELDS,
    ShortlistError,
    load_shortlist,
    settled_order,
)
from opencull_gui.shortlist_reviews import (
    ShortlistReviewError,
    ShortlistReviewStore,
)
from shortlist_kernel import (
    CONFIDENCE_FLOOR,
    assessments_for_candidates,
    build_professional_candidates,
    build_professional_checkpoint,
    build_professional_shortlist,
    checkpoint_professional_assessments,
    normalize_professional_assessment,
    parse_professional_candidates,
    professional_batches,
    professional_calibration_json,
    professional_report_valid,
    remaining_professional_candidates,
    settled_tier,
    valid_professional_assessment,
    valid_professional_calibration,
)


def report_payload():
    return {
        "format": "opencull-report-v2",
        "manifest_sha256": "manifest",
        "clusters": [{
            "cluster_id": "group-0001",
            "photos": ["shoot/DSCF0001.JPG", "shoot/DSCF0002.JPG"],
        }],
        "keep": [{
            "cluster_id": "group-0001",
            "photos": ["shoot/DSCF0001.JPG"],
            "rationale": "stronger frame",
        }],
        "warnings": [],
    }


class AssetFamilyTests(unittest.TestCase):
    def test_the_run_says_which_of_its_three_phases_it_is_in(self):
        import json as json_module

        from opencull_gui.shortlist import run_phases

        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            trace.write_text("")
            # Frames still being rated.
            self.assertEqual(
                run_phases(trace, rated=40, total=102)["phase"], "rating")
            # Rated; comparisons under way, counted from the trace.
            trace.write_text("\n".join(
                json_module.dumps({"kind": "gen", "agent": "B=m@h"})
                for _ in range(10)))
            state = run_phases(trace, rated=102, total=102)
            self.assertEqual(state["phase"], "comparing")
            self.assertEqual((state["compared"], state["batches"]), (10, 13))
            # Ratings ask A and must never be counted as comparisons.
            trace.write_text("\n".join(
                [json_module.dumps({"kind": "gen", "agent": "A=m@h"})] * 50))
            self.assertEqual(
                run_phases(trace, rated=102, total=102)["compared"], 0)
            # Every batch compared: the panel is what remains.
            trace.write_text("\n".join(
                [json_module.dumps({"kind": "gen", "agent": "B=m@h"})] * 13))
            settled = run_phases(trace, rated=102, total=102)
            self.assertEqual(settled["phase"], "certifying")
            self.assertEqual(settled["votes"], 5)

    def test_a_frame_the_model_cannot_read_is_recorded_not_fatal(self):
        import json as json_module

        from shortlist_kernel import (
            build_professional_shortlist,
            is_unassessed,
            professional_report_valid,
            unassessed_frame_json,
        )

        candidates = [
            {"photo": "A.JPG", "cluster_id": "group-0001", "raw_files": []},
            {"photo": "B.JPG", "cluster_id": "group-0002", "raw_files": []},
        ]
        bundle = json_module.dumps({
            "signature": "sig", "photos_root": "/x",
            "candidate_policy": "effective", "candidates": candidates,
            "source_report_sha256": "0" * 64, "review_revision": 0,
            "review_sha256": "", "minimum_dimension": 640})
        good = json_module.dumps({
            "photo": "A.JPG", "cluster_id": "group-0001",
            "tier": "promising", "score": 60, "confidence": 0.8,
            "rationale": "the light carries it", "raw_files": [],
            "local_warnings": [], "asset_warning": "",
            **dict.fromkeys(ASSESSMENT_FIELDS, "reading")})
        bad = unassessed_frame_json(candidates[1], "nothing readable came back")
        self.assertTrue(is_unassessed(json_module.loads(bad)))

        report = json_module.loads(build_professional_shortlist(
            bundle, [good, bad], [], "strict bar"))

        # The good frame is ranked; the bad one is named, not silent.
        self.assertEqual([e["photo"] for e in report["entries"]], ["A.JPG"])
        self.assertEqual(report["unassessed"][0]["photo"], "B.JPG")
        self.assertIn("readable", report["unassessed"][0]["reason"])
        # And the whole thing still validates: every candidate accounted for.
        self.assertTrue(professional_report_valid(
            json_module.dumps(report), bundle, candidates))

    def test_forgetting_one_rating_leaves_the_others(self):
        import json as json_module

        from opencull_gui.shortlist import forget_assessment

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "c.json"
            path.write_text(json_module.dumps({
                "assessments": [{"photo": "A.JPG"}, {"photo": "B.JPG"}],
                "completed": True}))
            self.assertTrue(forget_assessment(path, "B.JPG"))
            data = json_module.loads(path.read_text())
            self.assertEqual(
                [a["photo"] for a in data["assessments"]], ["A.JPG"])
            self.assertFalse(data["completed"])
            # A frame that was never there is not a change.
            self.assertFalse(forget_assessment(path, "Z.JPG"))

    def test_a_watcher_finds_the_checkpoint_a_run_adopted(self):
        from opencull_gui.shortlist import readable_checkpoint

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            per_bar = root / "out.json.1788e819.checkpoint.json"
            legacy = root / "out.json.checkpoint.json"
            legacy.write_text("{}")
            # Its own file is not written yet: look where it is reading.
            self.assertEqual(readable_checkpoint(per_bar), legacy)
            per_bar.write_text("{}")
            self.assertEqual(readable_checkpoint(per_bar), per_bar)

    def test_ratings_waiting_in_a_checkpoint_are_findable(self):
        import json as json_module

        from opencull_gui.shortlist import unfinished_assessments

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "Shoot.professional-shortlist.json"
            (root / f"{output.name}.abcd1234.checkpoint.json").write_text(
                json_module.dumps({
                    "assessments": [{"photo": "A.JPG"}, {"photo": "B.JPG"}],
                    "signature": {"profile": "gentle bar -- family",
                                  "candidate_count": 9}}))
            (root / f"{output.name}.99999999.checkpoint.json").write_text(
                json_module.dumps({"assessments": [], "signature": {}}))

            waiting = unfinished_assessments(output)

            self.assertEqual(len(waiting), 1)
            self.assertEqual(waiting[0]["rated"], 2)
            self.assertEqual(waiting[0]["total"], 9)
            self.assertIn("gentle", waiting[0]["bar"])

    def test_the_report_ranks_by_the_same_settled_order(self):
        """A shortlist must not present a 50 above a 74 as a ranking."""
        import json as json_module

        from shortlist_kernel import build_professional_shortlist

        candidates = [
            {"photo": f"F{n}.JPG", "cluster_id": f"group-{n:04d}",
             "raw_files": []} for n in range(12)
        ]
        bundle = json_module.dumps({
            "signature": "sig", "photos_root": "/x",
            "candidate_policy": "effective", "candidates": candidates,
            "source_report_sha256": "0" * 64, "review_revision": 0,
            "review_sha256": "", "minimum_dimension": 640})
        tiers = ["promising"] * 11 + ["strong"]
        scores = [70 - n for n in range(11)] + [5]
        assessments = [
            json_module.dumps({
                "photo": c["photo"], "cluster_id": c["cluster_id"],
                "tier": tier, "score": score, "confidence": 0.8,
                "rationale": "a visible reason",
                "raw_files": [], "local_warnings": [], "asset_warning": "",
                **dict.fromkeys(ASSESSMENT_FIELDS, "reading")})
            for c, tier, score in zip(candidates, tiers, scores)
        ]
        report = json_module.loads(build_professional_shortlist(
            bundle, assessments, [], "strict bar"))
        from opencull_gui.shortlist import standing

        # The order is the order of standing, descending.
        standings = [
            standing(e["tier"], e["score"]) for e in report["entries"]]
        self.assertEqual(standings, sorted(standings, reverse=True))
        self.assertEqual(report["entries"][0]["photo"], "F11.JPG")

    def test_standing_gives_each_tier_a_band_of_its_own(self):
        from opencull_gui.shortlist import standing

        for tier, low, high in (
            ("reject", 0, 20), ("ordinary", 20, 40), ("promising", 40, 60),
            ("strong", 60, 80), ("exceptional", 80, 100),
        ):
            self.assertEqual(standing(tier, 0), low)
            self.assertEqual(standing(tier, 100), high)
        # Inside a band the model's score orders the frames.
        self.assertLess(standing("strong", 5), standing("strong", 72))
        # The bands do not overlap, so a number never overturns a verdict.
        self.assertGreater(standing("strong", 0), standing("promising", 99))
        # A tier nobody recognises is worth nothing rather than crashing.
        self.assertEqual(standing("banquet", 90), 0.0)

    def test_a_contradicted_verdict_leads_but_is_flagged(self):
        """The verdict owns the band; the flag carries the doubt."""
        from opencull_gui.shortlist import (
            score_disagreements,
            settled_order,
        )

        entries = (
            [{"photo": f"P{n}.JPG", "tier": "promising", "score": 60 - n}
             for n in range(6)]
            + [{"photo": f"O{n}.JPG", "tier": "ordinary", "score": 45 - n}
               for n in range(6)]
            + [{"photo": "ODD-HIGH.JPG", "tier": "strong", "score": 5},
               {"photo": "ODD-LOW.JPG", "tier": "ordinary", "score": 70}]
        )
        order = settled_order(entries)
        # The strong frame leads on its band even at a score of five,
        # and wears the flag that says its readings disagree.
        self.assertEqual(order[0], "ODD-HIGH.JPG")
        self.assertIn("ODD-HIGH.JPG", score_disagreements(entries))

    def test_agreeing_readings_leave_the_verdict_in_charge(self):
        from opencull_gui.shortlist import settled_order

        entries = [
            {"photo": "S.JPG", "tier": "strong", "score": 80},
            {"photo": "P.JPG", "tier": "promising", "score": 60},
            {"photo": "O.JPG", "tier": "ordinary", "score": 40},
            {"photo": "R.JPG", "tier": "reject", "score": 20},
        ]
        self.assertEqual(
            settled_order(entries), ["S.JPG", "P.JPG", "O.JPG", "R.JPG"])

    def test_your_own_rating_settles_the_order(self):
        from opencull_gui.shortlist import settled_order

        entries = [
            {"photo": "A.JPG", "tier": "reject", "score": 30},
            {"photo": "B.JPG", "tier": "promising", "score": 55},
        ]
        self.assertEqual(
            settled_order(entries, {"A.JPG": "exceptional"})[0], "A.JPG")

    def test_each_bar_keeps_its_own_checkpoint(self):
        from opencull_gui.shortlist import bar_checkpoint_path

        out = "/x/Shoot.professional-shortlist.json"
        gentle = bar_checkpoint_path(out, "gentle bar -- family")
        strict = bar_checkpoint_path(out, "strict bar -- craft")
        self.assertNotEqual(gentle, strict)
        # The same bar, however it was typed, is the same run.
        self.assertEqual(
            gentle, bar_checkpoint_path(out, "  GENTLE BAR --  family "))
        # No bar at all keeps the name runs used before bars existed.
        self.assertEqual(
            bar_checkpoint_path(out, ""), f"{out}.checkpoint.json")

    def test_a_bar_adopts_a_legacy_checkpoint_written_under_it(self):
        """Renaming must not orphan ratings already paid for."""
        import json as json_module

        from shortlist_kernel import (
            build_professional_checkpoint,
            checkpoint_professional_assessments,
            load_professional_checkpoint,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bar = "gentle bar -- family"
            candidates = [{"photo": "A.JPG", "cluster_id": "group-0001"}]
            bundle = json_module.dumps(
                {"signature": "sig", "photos_root": str(root),
                 "candidates": candidates})
            assessment = {
                "photo": "A.JPG", "cluster_id": "group-0001",
                "tier": "promising", "score": 55, "confidence": 0.8,
                "rationale": "a reason",
                **{field: f"{field} reading" for field in ASSESSMENT_FIELDS},
            }
            state = build_professional_checkpoint(
                bundle, candidates, [json_module.dumps(assessment)],
                bar, False)
            legacy = root / "out.json.checkpoint.json"
            legacy.write_text(state)

            fresh = root / "out.json.abcd1234.checkpoint.json"
            loaded = load_professional_checkpoint(
                str(fresh), bundle, candidates, bar, True)

            self.assertEqual(
                len(checkpoint_professional_assessments(loaded, candidates)),
                1)

    def test_a_score_that_contradicts_its_verdict_is_flagged(self):
        from opencull_gui.shortlist import score_disagreements

        # Nine frames whose numbers follow their verdicts, and one
        # "strong" frame the model scored like a middling one.
        entries = [
            {"photo": f"G{n}.JPG", "tier": "promising", "score": 70 - n}
            for n in range(5)
        ] + [
            {"photo": f"O{n}.JPG", "tier": "ordinary", "score": 40 - n}
            for n in range(5)
        ] + [{"photo": "ODD.JPG", "tier": "exceptional", "score": 35}]

        flagged = score_disagreements(entries)

        self.assertIn("ODD.JPG", flagged)
        self.assertIn("35", flagged["ODD.JPG"])
        self.assertEqual(len(flagged), 1)

    def test_a_coherent_shoot_is_not_flagged_at_all(self):
        from opencull_gui.shortlist import score_disagreements

        entries = [
            {"photo": f"A{n}.JPG", "tier": tier, "score": score}
            for n, (tier, score) in enumerate([
                ("strong", 80), ("strong", 78), ("promising", 60),
                ("promising", 58), ("ordinary", 40), ("ordinary", 38),
                ("reject", 20), ("reject", 18)])
        ]
        self.assertEqual(score_disagreements(entries), {})

    def test_too_few_frames_to_speak_of_an_order_stay_silent(self):
        from opencull_gui.shortlist import score_disagreements

        self.assertEqual(score_disagreements([
            {"photo": "A.JPG", "tier": "strong", "score": 10},
            {"photo": "B.JPG", "tier": "reject", "score": 90}]), {})

    def test_the_evidence_shows_the_bar_the_claim_names(self):
        """A verifier cannot confirm a bar it was never shown."""
        import json as json_module

        from shortlist_kernel import (
            professional_report_evidence,
            professional_report_policy,
        )

        bar = "gentle bar (Is this worth keeping?) -- judged through family"
        report = json_module.dumps({
            "format": "opencull-professional-shortlist-v1",
            "candidate_policy": "effective", "profile": bar,
            "entries": [{
                "rank": 1, "photo": "A.JPG", "tier": "promising",
                "score": 60, "confidence": 0.8, "warnings": [],
                "rationale": "the light carries it", "raw_files": []}]})
        evidence = professional_report_evidence(report)
        self.assertIn(bar, evidence)
        self.assertIn(bar, professional_report_policy(bar))

    def test_the_claim_names_the_bar_the_run_was_actually_given(self):
        """A gentle run must not be certified against a strict bar."""
        from shortlist_kernel import professional_report_policy

        bar = ("gentle bar (Is this worth keeping?) -- judged through "
               "family: people and being together")
        claim = professional_report_policy(bar)
        self.assertIn(bar, claim)
        self.assertNotIn("strict professional bar", claim)
        # Without a bar the claim still stands on its flags.
        plain = professional_report_policy("")
        self.assertIn("ranks_contiguous", plain)
        self.assertIn("consistent stated bar", plain)

    def test_project_artifacts_are_not_indexed_as_source_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A.JPG").write_bytes(b"jpeg")
            managed = root / "Darkimiya" / "Developments"
            managed.mkdir(parents=True)
            (managed / "A.RAF").write_bytes(b"derived")
            assets = index_asset_families(root)
            self.assertEqual(assets.raw_companions("A.JPG"), ())
            self.assertIsNone(assets.family_for("Darkimiya/Developments/A.RAF"))

    def test_pairs_jpeg_and_raf_in_same_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shoot = root / "shoot"
            shoot.mkdir()
            Image.new("RGB", (20, 10), "green").save(
                shoot / "DSCF0001.JPG")
            (shoot / "DSCF0001.RAF").write_bytes(b"raw")
            (shoot / "DSCF0002.RAF").write_bytes(b"raw-only")

            assets = index_asset_families(root)
            family = assets.family_for("shoot/DSCF0001.JPG")
            self.assertIsNotNone(family)
            self.assertEqual(
                family.raw_files, ("shoot/DSCF0001.RAF",))
            self.assertEqual(
                assets.family_for("shoot/DSCF0001.RAF").asset_id,
                family.asset_id)
            self.assertEqual(
                assets.raw_companions("shoot/DSCF0002.RAF"),
                ("shoot/DSCF0002.RAF",))

    def test_does_not_pair_same_stem_across_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one").mkdir()
            (root / "two").mkdir()
            (root / "one" / "DSCF0001.JPG").write_bytes(b"jpeg")
            (root / "two" / "DSCF0001.RAF").write_bytes(b"raw")
            assets = index_asset_families(root)
            self.assertEqual(
                assets.raw_companions("one/DSCF0001.JPG"), ())

    def test_marks_multiple_raw_companions_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A.JPG").write_bytes(b"jpeg")
            (root / "A.RAF").write_bytes(b"raf")
            (root / "A.DNG").write_bytes(b"dng")
            family = index_asset_families(root).family_for("A.JPG")
            self.assertTrue(family.ambiguous)
            self.assertIn("multiple RAW", family.ambiguity)

    def test_refuses_symlink_that_escapes_photo_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            outside = root / "outside.JPG"
            outside.write_bytes(b"outside")
            (photos / "linked.JPG").symlink_to(outside)
            with self.assertRaisesRegex(AssetError, "escapes"):
                index_asset_families(photos)


class ShortlistSchemaTests(unittest.TestCase):
    def make_context(self, root: Path):
        photos = root / "photos"
        shoot = photos / "shoot"
        shoot.mkdir(parents=True)
        for name in ("DSCF0001.JPG", "DSCF0002.JPG"):
            Image.new("RGB", (20, 10), "blue").save(shoot / name)
        (shoot / "DSCF0001.RAF").write_bytes(b"raw")
        report_path = root / "report.json"
        report_path.write_text(
            json.dumps(report_payload()), encoding="utf-8")
        return photos, load_report(report_path)

    def write_shortlist(self, root: Path, report, **changes):
        entry = {
            "rank": 1,
            "photo": "shoot/DSCF0001.JPG",
            "raw_files": ["shoot/DSCF0001.RAF"],
            "cluster_id": "group-0001",
            "tier": "strong",
            "score": 88,
            "confidence": 0.9,
            "rationale": "Good pose and editing potential.",
            "assessment": {
                field: f"Assessed {field.replace('_', ' ')}."
                for field in ASSESSMENT_FIELDS
            },
            "warnings": [],
            **changes.pop("entry", {}),
        }
        payload = {
            "format": "opencull-professional-shortlist-v1",
            "source_report_sha256": report.sha256,
            "source_review_revision": 3,
            "candidate_policy": "effective",
            "entries": [entry],
            **changes,
        }
        path = root / "shortlist.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_and_indexes_valid_shortlist_without_changing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, report = self.make_context(root)
            original = report.data.copy()
            shortlist = load_shortlist(
                self.write_shortlist(root, report), report, photos)
            self.assertEqual(shortlist.entries[0]["tier"], "strong")
            self.assertEqual(
                shortlist.entry_by_photo["shoot/DSCF0001.JPG"]["rank"], 1)
            self.assertEqual(report.data, original)

    def test_rejects_wrong_report_and_unsafe_or_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, report = self.make_context(root)
            with self.assertRaisesRegex(ShortlistError, "different"):
                load_shortlist(
                    self.write_shortlist(
                        root, report, source_report_sha256="wrong"),
                    report, photos)
            with self.assertRaisesRegex(ShortlistError, "unsafe"):
                load_shortlist(
                    self.write_shortlist(
                        root, report,
                        entry={"raw_files": ["../outside.RAF"]}),
                    report, photos)
            with self.assertRaisesRegex(ShortlistError, "unrelated"):
                load_shortlist(
                    self.write_shortlist(
                        root, report,
                        entry={"raw_files": ["shoot/DSCF0002.JPG"]}),
                    report, photos)

    def test_rejects_unknown_photo_and_invalid_ranking(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, report = self.make_context(root)
            with self.assertRaisesRegex(ShortlistError, "unknown"):
                load_shortlist(
                    self.write_shortlist(
                        root, report,
                        entry={"photo": "shoot/UNKNOWN.JPG"}),
                    report, photos)
            with self.assertRaisesRegex(ShortlistError, "out of range"):
                load_shortlist(
                    self.write_shortlist(
                        root, report, entry={"score": 101}),
                    report, photos)


class ConfidenceTests(unittest.TestCase):
    """A frame nobody could judge must not lead the shortlist.

    A live shoot of eight eclipse frames: one came back at confidence
    0.0, the comparison pass promoted it from promising to strong, it
    landed at rank one above a frame assessed at 0.91, and a five-judge
    panel refused to certify the shortlist. It was right to. Confidence
    was carried everywhere and used nowhere.
    """

    def rows(self):
        return [
            {"photo": "A.ARW", "tier": "promising", "score": 50.0,
             "confidence": 0.0},
            {"photo": "B.ARW", "tier": "promising", "score": 50.0,
             "confidence": 0.9},
            {"photo": "C.ARW", "tier": "promising", "score": 50.0,
             "confidence": 0.5},
        ]

    def test_equal_standing_is_settled_by_who_was_surer(self):
        self.assertEqual(
            settled_order(self.rows()), ["B.ARW", "C.ARW", "A.ARW"])

    def test_a_higher_standing_still_wins_over_confidence(self):
        """Confidence breaks ties; it does not overturn a verdict."""
        rows = self.rows()
        rows[0]["tier"] = "strong"
        self.assertEqual(settled_order(rows)[0], "A.ARW")

    def test_confidence_that_cannot_be_read_counts_as_none(self):
        rows = self.rows()
        rows[1]["confidence"] = "very"
        self.assertEqual(settled_order(rows)[0], "C.ARW")

    def test_the_comparison_pass_may_not_promote_what_nobody_was_sure_of(self):
        self.assertEqual(
            settled_tier({"tier": "promising", "confidence": 0.0}, "strong"),
            "promising")

    def test_it_may_still_promote_a_frame_somebody_committed_to(self):
        self.assertEqual(
            settled_tier({"tier": "promising", "confidence": 0.9}, "strong"),
            "strong")

    def test_it_may_always_lower_a_verdict(self):
        """Seeing the batch is a reason to think less of a frame."""
        for confidence in (0.0, 0.9):
            with self.subTest(confidence=confidence):
                self.assertEqual(
                    settled_tier(
                        {"tier": "promising", "confidence": confidence},
                        "reject"),
                    "reject")

    def test_saying_nothing_leaves_the_verdict_alone(self):
        self.assertEqual(
            settled_tier({"tier": "strong", "confidence": 0.0}, None),
            "strong")

    def test_the_floor_is_the_boundary_it_says_it_is(self):
        just_under = settled_tier(
            {"tier": "promising", "confidence": CONFIDENCE_FLOOR - 0.01},
            "strong")
        exactly = settled_tier(
            {"tier": "promising", "confidence": CONFIDENCE_FLOOR}, "strong")
        self.assertEqual((just_under, exactly), ("promising", "strong"))

    def test_the_shoot_that_was_refused_now_ranks_the_confident_frame_first(self):
        """The real numbers from the run the panel turned down."""
        assessed = [
            {"photo": "DSC00704.ARW", "tier": "promising", "score": 50.0,
             "confidence": 0.0},
            {"photo": "DSC00703.ARW", "tier": "reject", "score": 38.0,
             "confidence": 0.91},
            {"photo": "DSC00700.ARW", "tier": "reject", "score": 50.0,
             "confidence": 0.0},
        ]
        proposed = {"DSC00704.ARW": "strong", "DSC00703.ARW": "strong",
                    "DSC00700.ARW": "reject"}
        tiers = {item["photo"]: settled_tier(item, proposed[item["photo"]])
                 for item in assessed}
        self.assertEqual(settled_order(assessed, tiers)[0], "DSC00703.ARW")
        self.assertEqual(tiers["DSC00704.ARW"], "promising")


class ProfessionalPipelineKernelTests(unittest.TestCase):
    def make_context(self, root: Path):
        photos = root / "photos"
        shoot = photos / "shoot"
        shoot.mkdir(parents=True)
        Image.new("RGB", (900, 700), "gray").save(
            shoot / "DSCF0001.JPG")
        Image.new("RGB", (900, 700), "orange").save(
            shoot / "DSCF0002.JPG")
        (shoot / "DSCF0001.RAF").write_bytes(b"raw-one")
        (shoot / "DSCF0002.RAF").write_bytes(b"raw-two")
        report_path = root / "report.json"
        report_path.write_text(
            json.dumps(report_payload()), encoding="utf-8")
        report = load_report(report_path)
        return photos, report_path, report

    def assessment(self, **changes):
        return {
            **{
                field: f"Visible evidence for {field}."
                for field in ASSESSMENT_FIELDS
            },
            "tier": "strong",
            "score": 87,
            "confidence": 0.85,
            "rationale": "A coherent frame with professional editing potential.",
            **changes,
        }

    def test_effective_policy_uses_human_selection_and_pairs_raw(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, report_path, report = self.make_context(root)
            review_path = root / "review.json"
            review_path.write_text(json.dumps({
                "format": "opencull-review-v1",
                "report_sha256": report.sha256,
                "revision": 4,
                "clusters": {
                    "group-0001": {
                        "reviewed": True,
                        "keepers": ["shoot/DSCF0002.JPG"],
                    },
                },
            }), encoding="utf-8")
            bundle = build_professional_candidates(
                str(report_path), str(photos), str(review_path), "effective")
            candidates = parse_professional_candidates(bundle)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["photo"], "shoot/DSCF0002.JPG")
            self.assertEqual(
                candidates[0]["raw_files"], ["shoot/DSCF0002.RAF"])
            self.assertIn(
                "severe softness", candidates[0]["local_warnings"][0])

    def test_local_softness_is_warning_not_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, report_path, _ = self.make_context(root)
            bundle = json.loads(build_professional_candidates(
                str(report_path), str(photos), policy="ai_only"))
            self.assertEqual(len(bundle["candidates"]), 1)
            self.assertEqual(bundle["excluded"], [])
            self.assertTrue(bundle["candidates"][0]["local_warnings"])

    def test_assessment_calibration_checkpoint_and_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, report_path, report = self.make_context(root)
            bundle = build_professional_candidates(
                str(report_path), str(photos), policy="ai_only")
            candidates = parse_professional_candidates(bundle)
            normalized = normalize_professional_assessment(self.assessment())
            self.assertTrue(valid_professional_assessment(normalized))
            assessment = json.dumps({
                "photo": candidates[0]["photo"],
                "cluster_id": candidates[0]["cluster_id"],
                "raw_files": candidates[0]["raw_files"],
                "local_warnings": candidates[0]["local_warnings"],
                "asset_warning": "",
                **normalized,
            })
            checkpoint = build_professional_checkpoint(
                bundle, candidates, [assessment], "family")
            restored = checkpoint_professional_assessments(
                checkpoint, candidates)
            self.assertEqual(len(restored), 1)
            self.assertEqual(
                remaining_professional_candidates(candidates, restored), [])
            self.assertEqual(professional_batches(candidates, 8), [candidates])
            calibration = {
                "ranking": candidates[0]["photo"],
                "exceptional": "",
                "strong": candidates[0]["photo"],
                "promising": "",
                "ordinary": "",
                "reject": "",
                "rationale": "Strongest professional editing candidate.",
                "confidence": 0.9,
            }
            self.assertTrue(
                valid_professional_calibration(calibration, candidates))
            calibration_json = professional_calibration_json(
                calibration, candidates)
            self.assertEqual(
                len(assessments_for_candidates(restored, candidates)), 1)
            result = build_professional_shortlist(
                bundle, restored, [calibration_json], "family")
            self.assertTrue(
                professional_report_valid(result, bundle, candidates))
            output = root / "professional.json"
            output.write_text(result, encoding="utf-8")
            loaded = load_shortlist(output, report, photos)
            self.assertEqual(
                loaded.entries[0]["raw_files"],
                ["shoot/DSCF0001.RAF"])


class ProfessionalBackendTests(unittest.TestCase):
    def make_context(self, root: Path):
        photos = root / "photos"
        shoot = photos / "shoot"
        shoot.mkdir(parents=True)
        Image.new("RGB", (900, 700), "navy").save(
            shoot / "DSCF0001.JPG")
        (shoot / "DSCF0001.RAF").write_bytes(b"raw")
        report_path = root / "report.json"
        payload = report_payload()
        payload["clusters"][0]["photos"] = ["shoot/DSCF0001.JPG"]
        payload["keep"][0]["photos"] = ["shoot/DSCF0001.JPG"]
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        report = load_report(report_path)
        shortlist_path = root / "report.professional-shortlist.json"
        shortlist_path.write_text(json.dumps({
            "format": "opencull-professional-shortlist-v1",
            "source_report_sha256": report.sha256,
            "source_review_revision": 0,
            "candidate_policy": "effective",
            "candidate_signature": "candidate-signature",
            "entries": [{
                "rank": 1,
                "photo": "shoot/DSCF0001.JPG",
                "raw_files": ["shoot/DSCF0001.RAF"],
                "cluster_id": "group-0001",
                "tier": "strong",
                "score": 88,
                "confidence": 0.9,
                "rationale": "Strong editing candidate.",
                "assessment": {
                    field: f"Evidence for {field}."
                    for field in ASSESSMENT_FIELDS
                },
                "warnings": [],
            }],
        }), encoding="utf-8")
        shortlist = load_shortlist(shortlist_path, report, photos)
        return photos, report_path, report, shortlist_path, shortlist

    def test_edit_directions_fall_back_and_allocate_versioned_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos_root, _, report, shortlist_path, _ = self.make_context(root)
            photos = PhotoStore(photos_root, root / "cache")
            reviews = ReviewStore(root / "review.json", report, photos_root)
            server = ReviewServer(
                ("127.0.0.1", 0), report, photos, reviews,
                shortlist_path=shortlist_path)
            try:
                server.shortlist_reviews.update(
                    "shoot/DSCF0001.JPG", "strong", True,
                    "Develop this.", True, 0, interesting=True)
                shortlist_sha = hashlib.sha256(
                    shortlist_path.read_bytes()).hexdigest()
                first = shortlist_path.with_name(
                    f"{shortlist_path.stem}.edit-directions-r1.json")
                first.write_text(json.dumps({
                    "format": "opencull-edit-directions-v1",
                    "shortlist_sha256": shortlist_sha,
                    "review_revision": 1,
                    "entries": [{"photo": "shoot/DSCF0001.JPG",
                                 "standard_title": "Natural"}],
                }), encoding="utf-8")

                server.shortlist_reviews.update(
                    "shoot/DSCF0001.JPG", "strong", True,
                    "Updated note.", True, 1, interesting=True)
                fallback = server.edit_directions_payload()
                self.assertTrue(fallback["available"])
                self.assertTrue(fallback["partial"])
                self.assertEqual(len(fallback["directions"]["entries"]), 1)
                effective = json.loads(Path(fallback["path"]).read_text())
                self.assertEqual(
                    effective["entries"], fallback["directions"]["entries"])
                self.assertIn("effective-edit-directions", fallback["path"])
                self.assertTrue(fallback["default_path"].endswith(
                    ".edit-directions-r2.json"))

                second = Path(fallback["default_path"])
                value = json.loads(first.read_text())
                value["review_revision"] = 2
                second.write_text(json.dumps(value), encoding="utf-8")
                exact = server.edit_directions_payload()
                self.assertFalse(exact["partial"])
                self.assertTrue(exact["regeneration_required"])
                self.assertEqual(exact["processed_count"], 1)
                self.assertEqual(
                    exact["processed_photos"], ["shoot/DSCF0001.JPG"])
                self.assertTrue(exact["default_path"].endswith(
                    ".edit-directions-r2-v2.json"))
            finally:
                server.server_close()

    def test_human_review_is_separate_revision_protected_and_exportable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, _, shortlist = self.make_context(root)
            review_path = root / "professional.review.json"
            store = ShortlistReviewStore(review_path, shortlist)
            state = store.update(
                "shoot/DSCF0001.JPG", "exceptional", True,
                "Prepare this RAF.", True, 0, interesting=True)
            self.assertEqual(state["revision"], 1)
            self.assertEqual(state["summary"]["edit_raw"], 1)
            self.assertEqual(state["summary"]["interesting"], 1)
            with self.assertRaisesRegex(
                    ShortlistReviewError, "reload"):
                store.update(
                    "shoot/DSCF0001.JPG", "strong", False,
                    "", True, 0)
            exported = store.export()
            self.assertEqual(
                exported["edit_raw_files"], ["shoot/DSCF0001.RAF"])
            self.assertEqual(
                exported["entries"][0]["effective_tier"], "exceptional")
            self.assertTrue(exported["entries"][0]["interesting"])
            undone = store.undo(1)
            self.assertEqual(undone["summary"]["reviewed"], 0)
            self.assertEqual(
                shortlist.entries[0]["tier"], "strong",
                "immutable AI evidence must not be overwritten")

    def test_legacy_review_merge_preserves_newer_project_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, shortlist_path, shortlist = self.make_context(root)
            legacy_path = shortlist_path.with_suffix(".review.json")
            legacy = ShortlistReviewStore(legacy_path, shortlist)
            legacy.update(
                "shoot/DSCF0001.JPG", "exceptional", True,
                "Legacy choice", True, 0, interesting=True)
            legacy_value = json.loads(legacy_path.read_text())
            legacy_value["entries"]["shoot/DSCF0001.JPG"]["updated_at"] = (
                "2026-01-01T00:00:00+00:00")
            legacy_path.write_text(json.dumps(legacy_value), encoding="utf-8")
            local_path = root / "Darkimiya" / "Reviews" / "review.json"
            local = ShortlistReviewStore(local_path, shortlist)
            local.update(
                "shoot/DSCF0001.JPG", "reject", False,
                "New project choice", False, 0, interesting=False)

            result = local.merge_legacy(legacy_path)
            state = local.public_state()

            self.assertTrue(result["merged"])
            self.assertEqual(result["preserved"], 1)
            self.assertTrue(Path(result["backup_path"]).is_file())
            self.assertEqual(
                state["entries"]["shoot/DSCF0001.JPG"]["tier"], "reject")
            self.assertEqual(len(state["migrations"]), 1)
            self.assertFalse(local.merge_legacy(legacy_path)["merged"])

    def test_review_server_shortlist_api_and_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos_root, _, report, shortlist_path, _ = self.make_context(root)
            photos = PhotoStore(photos_root, root / "cache")
            reviews = ReviewStore(root / "review.json", report, photos_root)
            server = ReviewServer(
                ("127.0.0.1", 0), report, photos, reviews,
                shortlist_path=shortlist_path,
                shortlist_default_path=root / "persistent" / "shortlist.json")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=5)
                connection.request(
                    "GET", "/api/shortlist",
                    headers={"Host": "127.0.0.1"})
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["available"])
                self.assertEqual(
                    Path(server.default_shortlist_path),
                    (root / "persistent" / "shortlist.json").resolve())
                body = json.dumps({
                    "photo": "shoot/DSCF0001.JPG",
                    "tier": "exceptional",
                    "edit_raw": True,
                    "note": "Edit carefully.",
                    "reviewed": True,
                    "revision": 0,
                })
                connection.request(
                    "POST", "/api/shortlist/review", body=body,
                    headers={
                        "Host": "127.0.0.1",
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body.encode())),
                        "X-OpenCull-CSRF": server.csrf_token,
                    })
                response = connection.getresponse()
                updated = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(updated["revision"], 1)
                connection.request(
                    "POST", "/api/shortlist/review", body=body,
                    headers={
                        "Host": "127.0.0.1",
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body.encode())),
                        "X-OpenCull-CSRF": server.csrf_token,
                    })
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                response.read()
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_professional_job_is_queued_and_command_is_resumable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos, report_path, _, _, _ = self.make_context(root)
            manager = JobManager(
                root / "jobs.json", Path(__file__).resolve().parents[1],
                autostart=False)
            try:
                state = manager.add_professional(
                    str(report_path), str(photos),
                    output=str(root / "generated-shortlist.json"),
                    policy="effective", profile="family")
                job = state["jobs"][0]
                self.assertEqual(job["kind"], "professional_shortlist")
                command = manager._command(job)
                self.assertIn("professional_shortlist.kim", " ".join(command))
                self.assertIn("resume=true", command)
                Path(job["checkpoint"]).write_text(json.dumps({
                    "assessments": [{}, {}],
                    "signature": {"candidate_count": 5},
                    "completed": False,
                }), encoding="utf-8")
                progress = manager.public()["jobs"][0]["progress"]
                self.assertEqual(progress["completed_items"], 2)
                self.assertEqual(progress["total_items"], 5)
                self.assertEqual(progress["fraction"], 0.4)
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
