import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from opencull_gui.project import load_project, project_manifest_path
from opencull_gui.project_catalog import ProjectCatalog
from opencull_gui.report import load_report


class ProjectCatalogTests(unittest.TestCase):
    def test_add_project_is_persistent_and_does_not_create_a_culling_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Family"; photos.mkdir()
            catalog = ProjectCatalog(root / "support" / "projects.json")

            state = catalog.add(str(photos))

            self.assertEqual(len(state["projects"]), 1)
            project = state["projects"][0]
            self.assertEqual(project["photos"], str(photos.resolve()))
            self.assertIsNone(project["culling"])
            self.assertEqual(project["activity_count"], 0)
            self.assertTrue(project_manifest_path(photos).is_file())
            reloaded = ProjectCatalog(root / "support" / "projects.json")
            self.assertEqual(reloaded.public()["projects"][0]["id"], project["id"])

    def test_manual_selection_groups_jpeg_and_raw_without_ai(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Shoot"; photos.mkdir()
            Image.new("RGB", (32, 24), "red").save(photos / "A.JPG")
            (photos / "A.RAF").write_bytes(b"raw-placeholder")
            Image.new("RGB", (32, 24), "blue").save(photos / "B.JPG")
            catalog = ProjectCatalog(root / "support" / "projects.json")
            project = catalog.add(str(photos))["projects"][0]

            report_path = catalog.manual_selection_report(project["id"])
            report = load_report(report_path)

            self.assertEqual(len(report.data["clusters"]), 2)
            first = report.data["clusters"][0]
            self.assertEqual(set(first["photos"]), {"A.JPG", "A.RAF"})
            self.assertEqual(report.data["keep"][0]["photos"], ["A.JPG"])
            self.assertIn("without model", report.data["notice"])
            manifest = load_project(project_manifest_path(photos))
            self.assertEqual(
                manifest["artifacts"]["culling_report"]["path"],
                str(report_path))

    def test_legacy_queue_project_is_imported_but_job_remains_activity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Legacy"; photos.mkdir()
            manifest = project_manifest_path(photos)
            catalog = ProjectCatalog(root / "support" / "projects.json")
            catalog.add(str(photos))
            project = load_project(manifest)
            fresh = ProjectCatalog(root / "support" / "fresh-projects.json")
            queue = {"jobs": [{
                "id": "job-1", "kind": "culling", "project_id": project["id"],
                "project": str(manifest), "photos": str(photos),
                "status": "running", "created_at": "2026-01-01T00:00:00+00:00",
                "progress": {"completed_clusters": 2, "total_clusters": 10,
                             "fraction": 0.2},
            }]}

            fresh.import_jobs(queue)
            state = fresh.public(queue)

            self.assertEqual(len(state["projects"]), 1)
            self.assertEqual(state["projects"][0]["activity_count"], 1)
            self.assertEqual(state["projects"][0]["culling"]["id"], "job-1")
            self.assertEqual(queue["jobs"][0]["status"], "running")

    def test_an_old_failure_does_not_shadow_the_run_that_succeeded(self):
        """The current job is the one in flight, else the newest by order."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Shoot"; photos.mkdir()
            manifest = project_manifest_path(photos)
            catalog = ProjectCatalog(root / "support" / "projects.json")
            catalog.add(str(photos))
            project = load_project(manifest)
            common = {"project_id": project["id"],
                      "project": str(manifest), "photos": str(photos)}
            queue = {"jobs": [
                {"id": "cull-lost", "kind": "culling", "status": "failed",
                 "created_at": "2026-01-01T00:00:00+00:00", **common},
                {"id": "cull-won", "kind": "culling", "status": "completed",
                 "created_at": "2026-01-01T01:00:00+00:00", **common},
                {"id": "assess-lost", "kind": "professional_shortlist",
                 "status": "failed",
                 "created_at": "2026-01-01T02:00:00+00:00", **common},
                {"id": "assess-won", "kind": "professional_shortlist",
                 "status": "completed",
                 "created_at": "2026-01-01T03:00:00+00:00", **common},
            ]}
            state = catalog.public(queue)
            listed = state["projects"][0]
            self.assertEqual(listed["culling"]["id"], "cull-won")
            self.assertEqual(listed["assessment"]["id"], "assess-won")

    def test_legacy_recent_report_never_overwrites_newer_project_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Shoot"; photos.mkdir()
            Image.new("RGB", (32, 24), "red").save(photos / "A.JPG")
            catalog = ProjectCatalog(root / "support" / "projects.json")
            project = catalog.add(str(photos))["projects"][0]
            current = catalog.manual_selection_report(project["id"])
            legacy = root / "legacy.json"
            legacy.write_text(json.dumps({
                "format": "opencull-report-v2", "manifest_sha256": "old",
                "clusters": [{"cluster_id": "old", "photos": ["A.JPG"]}],
                "keep": [{"cluster_id": "old", "photos": ["A.JPG"]}],
            }), encoding="utf-8")

            catalog.add_existing_report(str(photos), str(legacy))

            manifest = load_project(project_manifest_path(photos))
            self.assertEqual(
                manifest["artifacts"]["culling_report"]["path"], str(current))

    def test_partial_legacy_report_is_attached_with_recovery_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Shoot"; photos.mkdir()
            Image.new("RGB", (32, 24), "red").save(photos / "A.JPG")
            report = root / "partial.json"
            report.write_text(json.dumps({
                "format": "opencull-report-v2", "manifest_sha256": "old",
                "clusters": [{"cluster_id": "one", "photos": ["A.JPG", "B.JPG"]}],
                "keep": [{"cluster_id": "one", "photos": ["A.JPG"]}],
            }), encoding="utf-8")
            catalog = ProjectCatalog(root / "support" / "projects.json")

            state = catalog.add_existing_report(str(photos), str(report))

            project = state["projects"][0]
            self.assertTrue(project["report_available"])
            self.assertIn("1 of 2", project["import_warning"])

    def test_completed_legacy_cull_without_project_id_becomes_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Legacy"; photos.mkdir()
            Image.new("RGB", (32, 24), "red").save(photos / "A.JPG")
            report = root / "result.json"
            report.write_text(json.dumps({
                "format": "opencull-report-v2", "manifest_sha256": "old",
                "clusters": [{"cluster_id": "one", "photos": ["A.JPG"]}],
                "keep": [{"cluster_id": "one", "photos": ["A.JPG"]}],
            }), encoding="utf-8")
            queue = {"jobs": [{
                "id": "old-job", "kind": "culling", "status": "completed",
                "photos": str(photos), "output": str(report),
                "created_at": "2026-01-01T00:00:00+00:00",
            }]}
            catalog = ProjectCatalog(root / "support" / "projects.json")

            catalog.import_jobs(queue)
            state = catalog.public(queue)

            self.assertEqual(len(state["projects"]), 1)
            self.assertTrue(state["projects"][0]["report_available"])
            self.assertEqual(state["projects"][0]["activity_count"], 1)
            self.assertEqual(state["projects"][0]["culling"]["id"], "old-job")

    def test_disconnected_historical_project_remains_discoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Unmounted volume" / "Family"
            report = root / "result.json"
            report.write_text(json.dumps({
                "format": "opencull-report-v2", "manifest_sha256": "old",
                "clusters": [{"cluster_id": "one", "photos": ["A.JPG"]}],
                "keep": [{"cluster_id": "one", "photos": ["A.JPG"]}],
            }), encoding="utf-8")
            catalog = ProjectCatalog(root / "support" / "projects.json")

            state = catalog.add_existing_report(str(photos), str(report))

            project = state["projects"][0]
            self.assertFalse(project["available"])
            self.assertTrue(project["report_available"])
            self.assertIn("volume", project["import_warning"])


if __name__ == "__main__":
    unittest.main()
