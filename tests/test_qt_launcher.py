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

    def test_a_finished_report_reads_as_culled(self):
        self.assertEqual(
            self.launcher.project_state({"report_available": True}),
            ("Culled", "ready"))

    def test_a_raw_folder_offers_development(self):
        self.assertEqual(self.launcher.treatment_label("raw"), "Develop")

    def test_a_rendered_folder_offers_editing(self):
        self.assertEqual(self.launcher.treatment_label("bitmap"), "Edit")

    def test_a_mixed_folder_offers_both(self):
        self.assertEqual(self.launcher.treatment_label("mixed"), "Develop / Edit")

    def test_an_empty_folder_offers_neither(self):
        self.assertEqual(self.launcher.treatment_label("empty"), "")

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
    def __init__(self, projects=None, jobs=None, cache=None):
        self.paths = mock.Mock()
        self.paths.cache = Path(cache or tempfile.mkdtemp())
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

    def cards(self, window):
        grid = window.library_grid
        return [grid.itemAt(index).widget() for index in range(grid.count())]

    def test_an_empty_library_invites_a_folder_instead_of_showing_zero(self):
        window, _ = self.build(projects=[])
        self.assertFalse(window.count.isVisible())
        self.assertTrue(window.lead.isVisible())
        self.assertFalse(window.library_band.isVisible())
        self.assertIn("Open a folder of photographs", window.summary.text())

    def test_folders_are_listed_with_their_state(self):
        window, _ = self.build(projects=[
            {"name": "600_FUJI", "photos": "/p/600", "available": True,
             "report_available": True},
            {"name": "Wedding", "photos": "/p/w", "available": True,
             "report_available": False},
        ])
        rows = self.cards(window)
        self.assertEqual([row.name for row in rows], ["600_FUJI", "Wedding"])
        self.assertEqual(rows[0].badge.text(), "CULLED")
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

    def test_a_running_folder_shows_its_progress(self):
        window, _ = self.build(projects=[
            {"name": "A", "photos": "/p/a", "available": True,
             "culling": {"status": "running", "log_tail": "40 %"}}])
        card = self.cards(window)[0]
        self.assertEqual(card.badge.text(), "CULLING")
        self.assertEqual(card.meter.value(), 40)

    def test_a_settled_folder_has_no_meter(self):
        window, _ = self.build(projects=[
            {"name": "A", "photos": "/p/a", "available": True}])
        self.assertFalse(hasattr(self.cards(window)[0], "meter"))

    def test_opening_a_folder_lists_it_without_culling_it(self):
        # Opening enlists the folder; what to do with it is the next choice.
        window, services = self.build(projects=[])
        with mock.patch(
            "opencull_qt.launcher.QFileDialog.getExistingDirectory",
            return_value="/p/new",
        ), mock.patch(
            "opencull_qt.launcher.classify_folder",
            return_value={"kind": "raw", "total": 3},
        ):
            window.open_folder()
        self.assertEqual(len(self.cards(window)), 1)
        self.assertEqual(services.added, [], "opening must not queue a cull")

    def test_opening_a_folder_says_what_it_found(self):
        window, _ = self.build(projects=[])
        with mock.patch(
            "opencull_qt.launcher.QFileDialog.getExistingDirectory",
            return_value="/p/new",
        ), mock.patch(
            "opencull_qt.launcher.classify_folder",
            return_value={"kind": "raw", "total": 12},
        ):
            window.open_folder()
        self.assertIn("12 photographs", window.notice_text.text())
        self.assertIn("develop", window.notice_text.text().lower())

    def test_a_folder_with_nothing_readable_says_so(self):
        window, _ = self.build(projects=[])
        with mock.patch(
            "opencull_qt.launcher.QFileDialog.getExistingDirectory",
            return_value="/p/new",
        ), mock.patch(
            "opencull_qt.launcher.classify_folder",
            return_value={"kind": "empty", "total": 0},
        ):
            window.open_folder()
        self.assertIn("no photographs", window.notice_text.text())

    def test_dismissing_the_chooser_adds_nothing(self):
        window, services = self.build(projects=[])
        with mock.patch(
            "opencull_qt.launcher.QFileDialog.getExistingDirectory",
            return_value="",
        ):
            window.open_folder()
        self.assertEqual(len(self.cards(window)), 0)
        self.assertEqual(services.added, [])

    def test_cancelling_a_job_reaches_the_queue(self):
        window, services = self.build(
            jobs=[{"id": "j1", "status": "running", "photos": "/p/a"}])
        window.cancel({"id": "j1"})
        self.assertEqual(services.cancelled, ["j1"])

    def test_a_failure_is_reported_rather_than_raised(self):
        window, services = self.build(projects=[])
        services.jobs.add.side_effect = RuntimeError("no provider credential")
        window.cull_project({"name": "A", "photos": "/p/a"})
        self.assertTrue(window.notice.isVisible())
        self.assertIn("no provider credential", window.notice_text.text())

    def test_opening_a_missing_report_explains_rather_than_crashing(self):
        window, services = self.build()
        window.open_review({"report": "/gone.json", "photos": "/p"})
        self.assertIn("no longer on disk", window.notice_text.text())
        self.assertEqual(services.opened, [])

    def test_the_window_lands_on_the_screen_being_used(self):
        # A second monitor that is off or unwatched makes a window there
        # indistinguishable from one that never opened: a taskbar icon and
        # nothing else.
        from PySide6.QtGui import QGuiApplication

        window, _ = self.build()
        screen = QGuiApplication.primaryScreen()
        self.assertIsNotNone(screen)
        available = screen.availableGeometry()
        self.assertTrue(
            available.contains(window.frameGeometry().center()),
            f"window centre {window.frameGeometry().center()} is outside "
            f"the screen {available}")

    def test_a_window_larger_than_the_screen_is_shrunk_to_fit(self):
        window, _ = self.build()
        from PySide6.QtGui import QGuiApplication

        available = QGuiApplication.primaryScreen().availableGeometry()
        self.assertLessEqual(window.width(), available.width())
        self.assertLessEqual(window.height(), available.height())

    def buttons(self, row):
        from PySide6.QtWidgets import QPushButton

        return [b.text() for b in row.findChildren(QPushButton)]

    def folder(self, window, kind):
        """Pretend a folder holds RAW, rendered, or both."""
        window._contents["/p/a"] = {"kind": kind, "total": 3}

    def test_a_raw_folder_offers_develop_and_cull(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        self.folder(window, "raw")
        window.refresh()
        self.assertEqual(
            self.buttons(self.cards(window)[0]), ["Develop", "Cull"])

    def test_a_jpeg_folder_offers_edit_instead_of_develop(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        self.folder(window, "bitmap")
        window.refresh()
        self.assertEqual(
            self.buttons(self.cards(window)[0]), ["Edit", "Cull"])

    def test_a_mixed_folder_offers_both_treatments(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        self.folder(window, "mixed")
        window.refresh()
        self.assertIn(
            "Develop / Edit", self.buttons(self.cards(window)[0]))

    def test_a_folder_with_no_photographs_offers_no_treatment(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        self.folder(window, "empty")
        window.refresh()
        self.assertEqual(
            self.buttons(self.cards(window)[0]), ["Cull"])

    def test_a_culled_folder_offers_review_and_recull(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True,
             "report_available": True, "report": "/p/a/report.json"}])
        self.folder(window, "raw")
        window.refresh()
        self.assertEqual(
            self.buttons(self.cards(window)[0]),
            ["Review", "Develop", "Re-cull"])

    def test_a_running_folder_offers_no_second_cull(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True,
             "culling": {"status": "running"}}])
        self.folder(window, "raw")
        window.refresh()
        self.assertEqual(self.buttons(self.cards(window)[0]), [])

    def test_culling_a_fresh_folder_asks_nothing(self):
        window, services = self.build(projects=[])
        with mock.patch.object(window, "confirm_recull") as confirm:
            window.cull_project({"name": "A", "photos": "/p/a"})
        confirm.assert_not_called()
        self.assertEqual(services.added, ["/p/a"])

    def test_reculling_is_refused_unless_confirmed(self):
        window, services = self.build(projects=[])
        with mock.patch.object(window, "confirm_recull", return_value=False):
            window.cull_project({"name": "A", "photos": "/p/a"}, again=True)
        self.assertEqual(services.added, [])

    def test_reculling_proceeds_once_confirmed(self):
        window, services = self.build(projects=[])
        with mock.patch.object(window, "confirm_recull", return_value=True):
            window.cull_project({"name": "A", "photos": "/p/a"}, again=True)
        self.assertEqual(services.added, ["/p/a"])

    def test_the_recull_warning_says_it_costs_another_run(self):
        from opencull_qt.launcher import RECULL_WARNING

        text = RECULL_WARNING.format(name="A")
        self.assertIn("another full run", text)
        self.assertIn("discards the current selection", text)

    def test_starting_a_cull_says_it_will_be_marked_when_it_finishes(self):
        window, _ = self.build(projects=[])
        window.cull_project({"name": "A", "photos": "/p/a"})
        self.assertIn("marked Culled", window.notice_text.text())

    def test_developing_an_unculled_folder_builds_a_selection_first(self):
        window, services = self.build(projects=[])
        services.projects.manual_selection_report.return_value = Path("/p/a/all.json")
        with mock.patch("opencull_qt.launcher.webbrowser.open") as opened:
            window.develop({"id": "p1", "name": "A", "photos": "/p/a", "report": ""})
        services.projects.manual_selection_report.assert_called_once_with("p1")
        opened.assert_called_once()

    def test_developing_a_culled_folder_reuses_its_report(self):
        window, services = self.build(projects=[])
        report = Path(tempfile.mkdtemp()) / "report.json"
        report.write_text("{}", encoding="utf-8")
        with mock.patch("opencull_qt.launcher.webbrowser.open"):
            window.develop(
                {"id": "p1", "name": "A", "photos": "/p/a", "report": str(report)})
        services.projects.manual_selection_report.assert_not_called()
        self.assertEqual(services.opened[0][0], report)

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
