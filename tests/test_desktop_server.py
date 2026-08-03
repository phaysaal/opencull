"""Contract tests for the authenticated native desktop bridge."""

from __future__ import annotations

import http.client
import json
import threading
import tempfile
import unittest
from pathlib import Path

from opencull_gui.desktop_server import DesktopBridgeServer
from opencull_gui.jobs import JobManager
from opencull_gui.project import project_manifest_path
from opencull_gui.project_catalog import ProjectCatalog


class FakeJobs:
    def __init__(self) -> None:
        self.actions = []

    def public(self):
        return {
            "format": "opencull-job-queue-v1",
            "revision": 1,
            "active_job_id": None,
            "jobs": [],
        }

    def add(self, *args):
        self.actions.append(("add", args))
        return self.public()

    def action(self, job_id, action):
        self.actions.append(("action", job_id, action))
        return self.public()

    def relink_source(self, job_id, photos):
        self.actions.append(("relink", job_id, photos))
        return self.public()

    def remove(self, job_id, remove_artifacts=False):
        self.actions.append(("remove", job_id, remove_artifacts))
        return self.public()


class FakeProviders:
    def __init__(self):
        self.actions = []

    def public(self):
        return {"revision": 0, "profiles": []}

    def save(self, profile, revision, secret):
        self.actions.append(("save", profile, revision, secret))
        return self.public()

    def delete(self, profile_id, revision, remove_credential):
        self.actions.append(
            ("delete", profile_id, revision, remove_credential))
        return self.public()

    def test_connection(self, profile_id):
        self.actions.append(("test", profile_id))
        return {
            "profile_id": profile_id, "reachable": True,
            "available_model_count": 4,
            "configured_models_present": True,
            "missing_models": [], "vision_capability": {},
            "notice": "Connection is healthy.",
        }


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.jobs = FakeJobs()
        self.providers = FakeProviders()
        self.projects = ProjectCatalog(self.root / "projects.json")
        self.opened = []

        def open_review(report, photos):
            self.opened.append((report, photos))
            return {"url": "http://127.0.0.1:54321/", "title": "Example"}

        self.server = DesktopBridgeServer(
            ("127.0.0.1", 0), self.jobs, self.providers,
            open_review, projects=self.projects)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3)

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method: str, path: str, body=None, token=True):
        headers = {
            "Authorization": (
                f"Bearer {self.server.token}" if token else "Bearer wrong"),
            "Content-Type": "application/json",
        }
        self.connection.request(
            method, path,
            body=None if body is None else json.dumps(body),
            headers=headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read())

    def test_state_requires_the_ephemeral_session_token(self) -> None:
        status, payload = self.request("GET", "/state", token=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "invalid desktop session")
        status, payload = self.request("GET", "/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["queue"]["revision"], 1)

        status, diagnostics = self.request("GET", "/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual(
            diagnostics["format"], "opencull-native-diagnostics-v1")
        self.assertNotIn("credential", json.dumps(diagnostics).lower())

    def test_add_and_action_delegate_to_the_persistent_queue(self) -> None:
        status, _ = self.request("POST", "/jobs", {
            "photos": "/photos", "keep_per_group": 3,
            "recursive": True, "profile": "family",
            "provider_profile_id": "provider-1",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.jobs.actions[0][1][0], "/photos")
        status, _ = self.request("POST", "/jobs/action", {
            "job_id": "job-1", "action": "pause",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.jobs.actions[-1], ("action", "job-1", "pause"))

    def test_project_is_added_before_and_independently_of_culling(self) -> None:
        photos = self.root / "Family"; photos.mkdir()
        status, payload = self.request("POST", "/projects", {
            "photos": str(photos),
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["projects"]), 1)
        self.assertEqual(self.jobs.actions, [])
        self.assertTrue(project_manifest_path(photos).is_file())
        project_id = payload["projects"][0]["id"]

        status, _ = self.request("POST", "/projects/cull", {
            "project_id": project_id, "keep_per_group": 2,
            "recursive": True, "profile": "family",
            "provider_profile_id": "provider-1",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.jobs.actions[-1][0], "add")
        self.assertEqual(self.jobs.actions[-1][1][0], str(photos.resolve()))

    def test_reveal_rejects_a_missing_path(self) -> None:
        status, payload = self.request("POST", "/reveal", {
            "path": str(Path("/definitely/not/an/opencull/item")),
        })
        self.assertEqual(status, 400)
        self.assertIn("no longer exists", payload["error"])

    def test_provider_mutations_delegate_without_returning_a_secret(self) -> None:
        secret = "write-only-secret"
        status, payload = self.request("POST", "/providers/save", {
            "revision": 0,
            "profile": {"name": "Local"},
            "secret": secret,
        })
        self.assertEqual(status, 200)
        self.assertNotIn(secret, json.dumps(payload))
        self.assertEqual(self.providers.actions[-1][-1], secret)

        status, result = self.request("POST", "/providers/test", {
            "profile_id": "abc123",
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["reachable"])
        self.assertEqual(self.providers.actions[-1], ("test", "abc123"))

        status, _ = self.request("POST", "/providers/delete", {
            "profile_id": "abc123", "revision": 0,
            "remove_credential": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            self.providers.actions[-1], ("delete", "abc123", 0, True))

    def test_completed_job_returns_an_embeddable_review_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "shoot-results.json"
            photos = root / "photos"
            report.write_text("{}", encoding="utf-8")
            photos.mkdir()
            original_public = self.jobs.public
            self.jobs.public = lambda: {
                **original_public(),
                "jobs": [{
                    "id": "job-ready",
                    "status": "completed",
                    "output": str(report),
                    "photos": str(photos),
                }],
            }
            status, payload = self.request("POST", "/jobs/review", {
                "job_id": "job-ready",
            })
            self.assertEqual(status, 200)
            self.assertEqual(payload["url"], "http://127.0.0.1:54321/")
            self.assertEqual(payload["title"], "Example")
            self.assertEqual(self.opened, [(report, photos)])

    def test_relink_and_existing_review_are_explicit_operations(self) -> None:
        status, _ = self.request("POST", "/jobs/relink", {
            "job_id": "job-1", "photos": "/Volumes/Reconnected/Shoot",
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            self.jobs.actions[-1],
            ("relink", "job-1", "/Volumes/Reconnected/Shoot"))

        status, payload = self.request("POST", "/reviews/open", {
            "report": "/tmp/archive-results.json",
            "photos": "/Volumes/Archive/Photos",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["title"], "Example")
        self.assertEqual(
            self.opened[-1],
            (Path("/tmp/archive-results.json").resolve(),
             Path("/Volumes/Archive/Photos").resolve()))

        status, _ = self.request("POST", "/jobs/remove", {
            "job_id": "job-1", "remove_artifacts": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            self.jobs.actions[-1], ("remove", "job-1", True))


class JobRelinkTests(unittest.TestCase):
    def test_stopped_job_source_is_relinked_only_by_explicit_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "Original"
            replacement = root / "Reconnected"
            original.mkdir()
            replacement.mkdir()
            manager = JobManager(
                root / "jobs.json", root, autostart=False)
            try:
                state = manager.add(
                    str(original), output=str(root / "result.json"))
                job_id = state["jobs"][0]["id"]
                original.rename(root / "Original-offline")
                state = manager.relink_source(job_id, str(replacement))
                self.assertEqual(
                    state["jobs"][0]["photos"], str(replacement.resolve()))
                self.assertIn("Source relinked", state["jobs"][0]["message"])
            finally:
                manager.shutdown()

    def test_remove_preserves_photos_and_result_but_can_clean_resume_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "Photos"
            photos.mkdir()
            source_photo = photos / "A.JPG"
            source_photo.write_bytes(b"source")
            result = root / "result.json"
            manager = JobManager(
                root / "jobs.json", root, autostart=False)
            try:
                state = manager.add(str(photos), output=str(result))
                job = state["jobs"][0]
                manager.action(job["id"], "cancel")
                checkpoint = Path(job["checkpoint"])
                log = Path(job["log"])
                checkpoint.write_text("checkpoint", encoding="utf-8")
                log.write_text("log", encoding="utf-8")
                result.write_text("result", encoding="utf-8")

                state = manager.remove(job["id"], remove_artifacts=True)
                self.assertEqual(state["jobs"], [])
                self.assertFalse(checkpoint.exists())
                self.assertFalse(log.exists())
                self.assertTrue(source_photo.exists())
                self.assertTrue(result.exists())
            finally:
                manager.shutdown()

    def test_new_jobs_keep_results_in_the_folder_owned_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = root / "BundleResources"
            results = root / "ApplicationSupportResults"
            photos = root / "Photos"
            resources.mkdir()
            results.mkdir()
            photos.mkdir()
            manager = JobManager(
                root / "jobs.json", resources,
                output_root=results, autostart=False)
            try:
                state = manager.add(str(photos))
                job = state["jobs"][0]
                output = Path(job["output"])
                self.assertEqual(
                    output.parent, (photos / "Darkimiya" / "Reports").resolve())
                self.assertNotEqual(output.parent, resources.resolve())
                self.assertTrue((photos / "Darkimiya" / "project.json").is_file())
                self.assertEqual(
                    Path(job["project"]),
                    (photos / "Darkimiya" / "project.json").resolve())
                self.assertTrue(job["project_id"])
            finally:
                manager.shutdown()


class NativeReviewWindowTests(unittest.TestCase):
    def test_webkit_javascript_dialogs_have_a_native_ui_delegate(self):
        source = (
            Path(__file__).parents[1]
            / "native-macos/Sources/OpenCullNative/ReviewWindow.swift"
        ).read_text(encoding="utf-8")
        self.assertIn("webView.uiDelegate = context.coordinator", source)
        self.assertIn("WKUIDelegate", source)
        self.assertIn("runJavaScriptConfirmPanelWithMessage", source)
        self.assertIn("runJavaScriptTextInputPanelWithPrompt", source)

    def test_native_overflow_menus_hide_the_automatic_chevron(self):
        source = (
            Path(__file__).parents[1]
            / "native-macos/Sources/OpenCullNative/ContentView.swift"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count(".menuIndicator(.hidden)"), 3)


if __name__ == "__main__":
    unittest.main()
