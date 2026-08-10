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
from opencull_gui.shortlist import ASSESSMENT_FIELDS, ShortlistError, load_shortlist
from opencull_gui.shortlist_reviews import (
    ShortlistReviewError,
    ShortlistReviewStore,
)
from shortlist_kernel import (
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
