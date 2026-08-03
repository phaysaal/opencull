"""The native launcher: state rendering, actions, and the providers dialog.

Qt runs on its offscreen platform here, so these execute in CI with no
display attached.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui import credentials  # noqa: E402
from opencull_gui.providers import AGENTS, ProviderStore  # noqa: E402


def qt_available() -> bool:
    return QApplication is not None


@unittest.skipUnless(qt_available(), "PySide6 is not installed")
class LauncherLogicTests(unittest.TestCase):
    """Pure functions, no widgets."""

    def setUp(self):
        from opencull_qt import launcher

        self.launcher = launcher

    def test_a_running_cull_is_reported_as_running(self):
        state = self.launcher.project_state({"culling": {"status": "running"}})
        self.assertEqual(state, ("Culling", "running"))

    def test_a_queued_cull_is_also_in_flight(self):
        _label, tone = self.launcher.project_state({"culling": {"status": "queued"}})
        self.assertEqual(tone, "running")

    def test_a_finished_report_reads_as_reviewed(self):
        self.assertEqual(
            self.launcher.project_state({"report_available": True}),
            ("Reviewed", "ready"))

    def test_a_disconnected_folder_is_called_out(self):
        _label, tone = self.launcher.project_state(
            {"available": False, "report_available": False})
        self.assertEqual(tone, "failed")

    def test_a_fresh_folder_has_no_tone(self):
        self.assertEqual(
            self.launcher.project_state({"available": True}), ("Not culled", ""))

    def test_progress_reads_the_last_percentage_a_worker_printed(self):
        job = {"status": "running", "log_tail": "STEP 10 %\nSTEP 64 % scanning"}
        self.assertEqual(self.launcher.job_progress(job), 64)

    def test_progress_is_clamped_to_a_percentage(self):
        self.assertEqual(
            self.launcher.job_progress({"log_tail": "999 %"}), 100)

    def test_a_running_job_without_progress_starts_at_zero(self):
        self.assertEqual(self.launcher.job_progress({"status": "running"}), 0)

    def test_a_finished_job_reports_no_progress(self):
        self.assertIsNone(self.launcher.job_progress({"status": "completed"}))

    def test_paths_under_home_are_abbreviated(self):
        self.assertEqual(
            self.launcher.short_path(str(Path.home() / "Pictures" / "Shoot")),
            "~/Pictures/Shoot")

    def test_paths_elsewhere_are_left_alone(self):
        self.assertEqual(self.launcher.short_path("/mnt/photos"), "/mnt/photos")


class FakeServices:
    def __init__(self, projects=None, jobs=None):
        self._projects = projects or []
        self._jobs = jobs or []
        self.added: list[str] = []
        self.cancelled: list[str] = []
        self.opened: list[tuple[Path, Path]] = []
        self.closed = False
        self.projects = mock.Mock()
        self.projects.public.side_effect = lambda queue: {
            "projects": self._projects}
        self.projects.add.side_effect = self._add
        self.jobs = mock.Mock()
        self.jobs.public.side_effect = lambda: {"jobs": self._jobs, "revision": 1}
        self.jobs.add.side_effect = lambda *a, **k: self.added.append(a[0])
        self.jobs.action.side_effect = (
            lambda job_id, action: self.cancelled.append(job_id))
        self.providers = None

    def _add(self, folder):
        self._projects.append(
            {"id": "p1", "name": Path(folder).name, "photos": folder,
             "available": True, "report_available": False})
        return {"projects": list(self._projects)}

    def open_review(self, report, photos, shortlist=None):
        self.opened.append((report, photos))
        return {"url": "http://127.0.0.1:1/", "title": "x"}

    def close(self):
        self.closed = True


@unittest.skipUnless(qt_available(), "PySide6 is not installed")
class LauncherWindowTests(unittest.TestCase):
    application = None

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def build(self, **kwargs):
        from opencull_qt.launcher import Launcher

        services = FakeServices(**kwargs)
        # A very long interval: these tests drive refresh() themselves.
        window = Launcher(services, poll_interval=10_000_000)
        # Qt reports a child as invisible while its window is unshown, so the
        # window has to be shown for visibility assertions to mean anything.
        window.show()
        self.addCleanup(window.close)
        return window, services

    def rows(self, layout):
        return [
            layout.itemAt(index).widget() for index in range(layout.count())]

    def test_an_empty_library_invites_a_folder_instead_of_showing_zero(self):
        window, _ = self.build(projects=[])
        self.assertFalse(window.count.isVisible())
        self.assertTrue(window.lead.isVisible())
        self.assertFalse(window.library_band.isVisible())
        self.assertIn("Point Darkimiya at a shoot", window.summary.text())

    def test_folders_are_listed_with_their_state(self):
        window, _ = self.build(projects=[
            {"name": "600_FUJI", "photos": "/p/600", "available": True,
             "report_available": True},
            {"name": "Wedding", "photos": "/p/w", "available": True,
             "report_available": False},
        ])
        rows = self.rows(window.library_rows)
        self.assertEqual([row.name for row in rows], ["600_FUJI", "Wedding"])
        self.assertEqual(rows[0].badge.text(), "REVIEWED")
        self.assertEqual(rows[1].badge.text(), "NOT CULLED")
        self.assertEqual(window.count.text(), "2")

    def test_the_queue_is_hidden_until_something_is_running(self):
        window, _ = self.build(projects=[
            {"name": "A", "photos": "/p/a", "available": True}])
        self.assertFalse(window.queue_band.isVisible())

    def test_a_running_job_appears_in_the_queue_with_its_progress(self):
        window, _ = self.build(
            projects=[{"name": "A", "photos": "/p/a", "available": True}],
            jobs=[{"id": "j1", "status": "running", "photos": "/p/a",
                   "name": "A", "log_tail": "42 %"}])
        self.assertTrue(window.queue_band.isVisible())
        row = self.rows(window.queue_rows)[0]
        self.assertEqual(row.badge.text(), "CULLING")
        self.assertEqual(row.meter.value(), 42)

    def test_a_running_folder_advances_its_sprocket(self):
        window, _ = self.build(projects=[
            {"name": "A", "photos": "/p/a", "available": True,
             "culling": {"status": "running"}}])
        self.assertTrue(self.rows(window.library_rows)[0].sprocket.is_running())

    def test_a_settled_folder_does_not(self):
        window, _ = self.build(projects=[
            {"name": "A", "photos": "/p/a", "available": True}])
        self.assertFalse(self.rows(window.library_rows)[0].sprocket.is_running())

    def test_choosing_a_folder_adds_it_and_queues_a_cull(self):
        window, services = self.build(projects=[])
        with mock.patch(
            "opencull_qt.launcher.QFileDialog.getExistingDirectory",
            return_value="/p/new",
        ):
            window.cull_folder()
        self.assertEqual(services.added, ["/p/new"])
        self.assertEqual(len(self.rows(window.library_rows)), 1)

    def test_dismissing_the_chooser_starts_nothing(self):
        window, services = self.build(projects=[])
        with mock.patch(
            "opencull_qt.launcher.QFileDialog.getExistingDirectory",
            return_value="",
        ):
            window.cull_folder()
        self.assertEqual(services.added, [])

    def test_cancelling_a_job_reaches_the_queue(self):
        window, services = self.build(
            jobs=[{"id": "j1", "status": "running", "photos": "/p/a"}])
        window.cancel({"id": "j1"})
        self.assertEqual(services.cancelled, ["j1"])

    def test_a_failure_is_reported_rather_than_raised(self):
        window, services = self.build(projects=[])
        services.jobs.add.side_effect = RuntimeError("no provider credential")
        with mock.patch(
            "opencull_qt.launcher.QFileDialog.getExistingDirectory",
            return_value="/p/new",
        ):
            window.cull_folder()
        self.assertTrue(window.notice.isVisible())
        self.assertIn("no provider credential", window.notice_text.text())

    def test_opening_a_missing_report_explains_rather_than_crashing(self):
        window, services = self.build()
        window.open_review({"report": "/gone.json", "photos": "/p"})
        self.assertIn("no longer on disk", window.notice_text.text())
        self.assertEqual(services.opened, [])

    def test_closing_the_window_shuts_the_services_down(self):
        window, services = self.build()
        window.close()
        self.assertTrue(services.closed)


@unittest.skipUnless(qt_available(), "PySide6 is not installed")
class ProvidersDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.store = ProviderStore(
            root / "providers.json", root,
            keychain=credentials.FileCredentialStore(root / "creds.json"))
        self.addCleanup(self._temporary.cleanup)

    def dialog(self):
        from opencull_qt.providers import ProvidersDialog

        dialog = ProvidersDialog(self.store)
        self.addCleanup(dialog.close)
        return dialog

    def test_an_empty_store_asks_for_a_key(self):
        dialog = self.dialog()
        self.assertIn("Paste", dialog.key.placeholderText())
        self.assertEqual(dialog.key.property("stored"), "false")

    def test_saving_stores_the_credential(self):
        dialog = self.dialog()
        dialog.key.setText("sk-or-v1-example")
        dialog.save()
        profile = self.store.public()["profiles"][0]
        self.assertEqual(profile["credential"], "stored")
        self.assertEqual(self.store.credential(profile["id"]), "sk-or-v1-example")

    def test_a_stored_key_shows_as_dots_and_never_as_a_value(self):
        dialog = self.dialog()
        dialog.key.setText("sk-or-v1-example")
        dialog.save()
        reopened = self.dialog()
        self.assertRegex(reopened.key.placeholderText(), r"^•+$")
        # A value would be submitted on the next save, replacing the key.
        self.assertEqual(reopened.key.text(), "")

    def test_resaving_blank_keeps_the_stored_credential(self):
        dialog = self.dialog()
        dialog.key.setText("sk-or-v1-example")
        dialog.save()
        profile_id = self.store.public()["profiles"][0]["id"]
        reopened = self.dialog()
        reopened.save()
        self.assertEqual(len(self.store.public()["profiles"]), 1)
        self.assertEqual(self.store.credential(profile_id), "sk-or-v1-example")

    def test_the_dialog_names_where_the_credential_lives(self):
        dialog = self.dialog()
        text = " ".join(
            child.text() for child in dialog.findChildren(type(dialog.status)))
        self.assertTrue(
            "owner-only file" in text or "keyring" in text or "Keychain" in text)

    def test_every_agent_gets_a_model(self):
        dialog = self.dialog()
        dialog.key.setText("sk-or-v1-example")
        dialog.save()
        models = self.store.public()["profiles"][0]["models"]
        self.assertEqual(set(models), set(AGENTS))


if __name__ == "__main__":
    unittest.main()
