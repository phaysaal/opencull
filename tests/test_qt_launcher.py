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

from opencull_gui import credentials, phases  # noqa: E402
from opencull_gui.providers import AGENTS, ProviderStore  # noqa: E402
from tests.test_qt_develop import build_shoot  # noqa: E402


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
        root = Path(cache or tempfile.mkdtemp())
        self.paths.cache = root
        self.paths.support = root / "support"
        self.paths.results = root / "results"
        self.paths.support.mkdir(parents=True, exist_ok=True)
        self.paths.results.mkdir(parents=True, exist_ok=True)
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

    def test_opening_a_folder_that_cannot_be_read_explains_rather_than_crashing(self):
        window, services = self.build()
        services.projects.manual_selection_report.side_effect = ValueError(
            "project contains no supported photographs")
        window.open_review({"id": "p1", "name": "A",
                            "report": "/gone.json", "photos": "/p"})
        self.assertIn("no supported photographs", window.notice_text.text())
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

        # The remove control is an icon and carries no text.
        return [b.text() for b in row.findChildren(QPushButton) if b.text()]

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

    def card_for(self, window, kind):
        self.folder(window, kind)
        window.refresh()
        return self.cards(window)[0]

    def test_clicking_the_pictures_opens_the_folder_for_editing(self):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        card = self.card_for(window, "bitmap")
        self.assertTrue(card.strip.opens())
        with mock.patch.object(window, "develop") as develop:
            card.strip.mousePressEvent(QMouseEvent(
                QMouseEvent.Type.MouseButtonPress, QPointF(10, 10),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
        develop.assert_called_once()
        self.assertEqual(develop.call_args[0][0]["id"], "p1")

    def test_the_pictures_say_where_they_lead(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        self.assertEqual(self.card_for(window, "raw").strip.toolTip(), "Develop A")
        self.assertEqual(self.card_for(window, "bitmap").strip.toolTip(), "Edit A")

    def test_pictures_that_lead_nowhere_are_not_a_target(self):
        # A folder with nothing readable in it has no treatment button, so
        # its strip must not look like a way in either.
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        card = self.card_for(window, "empty")
        self.assertFalse(card.strip.opens())
        self.assertEqual(card.strip.toolTip(), "")

    def test_an_offline_folder_is_not_a_target(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": False}])
        self.assertFalse(self.card_for(window, "bitmap").strip.opens())

    def test_a_folder_being_culled_is_not_a_target(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True,
             "culling": {"status": "running", "log_tail": "20 %"}}])
        card = self.card_for(window, "bitmap")
        # The button is gone while the run is in flight; so is the strip.
        self.assertNotIn("Edit", self.buttons(card))
        self.assertFalse(card.strip.opens())

    def test_a_culled_folder_offers_review_assess_and_recull(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True,
             "report_available": True, "report": "/p/a/report.json"}])
        self.folder(window, "raw")
        window.refresh()
        self.assertEqual(
            self.buttons(self.cards(window)[0]),
            ["Review", "Assess", "Develop", "Re-cull"])

    def test_an_uncalled_folder_is_not_offered_an_assessment(self):
        # The assessment reads the cull and its review. There is nothing for
        # it to read yet.
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        self.folder(window, "raw")
        window.refresh()
        self.assertNotIn("Assess", self.buttons(self.cards(window)[0]))

    def test_an_assessed_folder_offers_to_open_it_rather_than_redo_it(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True,
             "report_available": True, "report": "/p/a/report.json",
             "shortlist_available": True, "shortlist": "/p/a/s.json"}])
        self.folder(window, "raw")
        window.refresh()
        buttons = self.buttons(self.cards(window)[0])
        self.assertIn("Assessment", buttons)
        self.assertNotIn("Assess", buttons)

    def test_an_assessed_folder_says_so(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True,
             "report_available": True, "report": "/p/a/report.json",
             "shortlist_available": True, "shortlist": "/p/a/s.json"}])
        self.folder(window, "raw")
        window.refresh()
        self.assertEqual(self.cards(window)[0].badge.text(), "ASSESSED")

    def test_an_assessment_in_flight_is_what_the_card_reports(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True,
             "report_available": True, "report": "/p/a/report.json",
             "assessment": {"status": "running", "log_tail": "40 %"}}])
        self.folder(window, "raw")
        window.refresh()
        card = self.cards(window)[0]
        self.assertEqual(card.badge.text(), "ASSESSING")
        # And it is not offered a second one while the first is running.
        self.assertNotIn("Assess", self.buttons(card))

    def test_the_assessment_phase_opens_the_marks_rather_than_the_queue(self):
        window, services = self.build(projects=[])
        with mock.patch.object(window, "open_project") as opened:
            window.open_shortlist({
                "id": "p1", "name": "A", "photos": "/p/a",
                "shortlist_available": True, "shortlist": "/p/a/s.json"})
        opened.assert_called_once()
        self.assertEqual(opened.call_args[0][1], phases.ASSESSMENT)
        services.jobs.add_professional.assert_not_called()

    def test_assessing_queues_the_shortlist_with_the_culling_review(self):
        window, services = self.build(projects=[])
        shoot = Path(tempfile.mkdtemp())
        report_path, photos_path = build_shoot(shoot)
        window.assess_project({
            "id": "p1", "name": "A", "photos": str(photos_path),
            "report": str(report_path)})
        services.jobs.add_professional.assert_called_once()
        arguments = services.jobs.add_professional.call_args
        self.assertEqual(arguments[0][0], str(report_path))
        self.assertIn("mark", window.notice_text.text())

    def test_a_missing_report_cannot_be_assessed(self):
        window, services = self.build(projects=[])
        window.assess_project(
            {"id": "p1", "name": "A", "photos": "/p/a", "report": "/gone.json"})
        services.jobs.add_professional.assert_not_called()
        self.assertIn("no longer on disk", window.notice_text.text())

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
        shoot = Path(tempfile.mkdtemp())
        report_path, photos_path = build_shoot(shoot)
        services.projects.manual_selection_report.return_value = report_path
        window.develop({"id": "p1", "name": "A", "photos": str(photos_path),
                        "report": "", "available": True})
        self.addCleanup(window.show_projects)
        services.projects.manual_selection_report.assert_called_once_with("p1")
        self.assertEqual(window.review_page.current, phases.DEVELOPMENT)

    def test_developing_a_culled_folder_reuses_its_report(self):
        window, services = self.build(projects=[])
        shoot = Path(tempfile.mkdtemp())
        report_path, photos_path = build_shoot(shoot)
        window.develop({"id": "p1", "name": "A", "photos": str(photos_path),
                        "report": str(report_path), "available": True})
        self.addCleanup(window.show_projects)
        services.projects.manual_selection_report.assert_not_called()
        self.assertEqual(window.bench.report_path, report_path)

    def test_develop_never_leaves_the_window(self):
        # The browser hand-off was the thing this architecture removed.
        source = Path("opencull_qt/launcher.py").read_text(encoding="utf-8")
        self.assertNotIn("webbrowser", source)

    def test_every_phase_of_one_shoot_shares_one_shell(self):
        window, _ = self.build(projects=[])
        shoot = Path(tempfile.mkdtemp())
        report_path, photos_path = build_shoot(shoot)
        project = {"id": "p1", "name": "A", "photos": str(photos_path),
                   "report": str(report_path), "available": True,
                   "report_available": True}
        window.open_review(project)
        self.addCleanup(window.show_projects)
        shell = window.review_page
        self.assertEqual(shell.current, phases.CULL)
        # And the same shell, not a second one, carries the next phase.
        window.develop(project)
        self.assertEqual(window.review_page.current, phases.DEVELOPMENT)

    def test_the_two_phases_reach_their_own_pages(self):
        from opencull_qt.develop import DevelopPage
        from opencull_qt.review import ReviewPage

        window, _ = self.build(projects=[])
        shoot = Path(tempfile.mkdtemp())
        report_path, photos_path = build_shoot(shoot)
        project = {"id": "p1", "name": "A", "photos": str(photos_path),
                   "report": str(report_path), "available": True,
                   "report_available": True}
        window.open_review(project)
        self.addCleanup(window.show_projects)
        shell = window.review_page
        self.assertIsInstance(shell.page_for(phases.CULL), ReviewPage)
        shell.open_phase(phases.DEVELOPMENT)
        self.assertIsInstance(shell.page_for(phases.DEVELOPMENT), DevelopPage)

    def test_the_shoot_is_a_page_of_this_window(self):
        from opencull_qt.shell import ProjectShell

        window, _ = self.build(projects=[])
        shoot = Path(tempfile.mkdtemp())
        report_path, photos_path = build_shoot(shoot)
        window.develop({"id": "p1", "name": "A", "photos": str(photos_path),
                        "report": str(report_path), "available": True})
        self.addCleanup(window.show_projects)
        self.assertIsInstance(window.pages.currentWidget(), ProjectShell)
        # Leaving it returns to the library rather than closing anything.
        window.show_projects()
        self.assertIs(window.pages.currentWidget(), window.projects_page)

    def test_every_folder_can_be_removed(self):
        window, _ = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        self.assertTrue(hasattr(self.cards(window)[0], "remove_button"))

    def test_removing_asks_first(self):
        window, services = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        with mock.patch.object(window, "confirm_remove", return_value=False):
            window.remove_project({"id": "p1", "name": "A", "photos": "/p/a"})
        services.projects.remove.assert_not_called()

    def test_removing_forgets_the_folder_once_confirmed(self):
        window, services = self.build(projects=[
            {"id": "p1", "name": "A", "photos": "/p/a", "available": True}])
        with mock.patch.object(window, "confirm_remove", return_value=True):
            window.remove_project({"id": "p1", "name": "A", "photos": "/p/a"})
        services.projects.remove.assert_called_once_with("p1")

    def test_removing_says_the_photographs_are_untouched(self):
        window, _ = self.build(projects=[])
        with mock.patch.object(window, "confirm_remove", return_value=True):
            window.remove_project({"id": "p1", "name": "A", "photos": "/p/a"})
        self.assertIn("untouched", window.notice_text.text())

    def test_removal_never_reaches_the_filesystem(self):
        # The library entry is the only thing this may touch.
        source = Path("opencull_qt/launcher.py").read_text(encoding="utf-8")
        removal = source.split("def remove_project")[1].split("\n    def ")[0]
        for forbidden in ("unlink", "rmtree", "shutil", "os.remove"):
            self.assertNotIn(forbidden, removal)

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

class CatalogRemovalTests(unittest.TestCase):
    """Forgetting a folder must never reach the photographs."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.shoot = self.root / "shoot"
        self.shoot.mkdir()
        for index in range(3):
            (self.shoot / f"DSCF{index:04d}.JPG").write_bytes(b"\xff\xd8\xff\xd9")
        self.addCleanup(self._temporary.cleanup)

    def catalog(self):
        from opencull_gui.project_catalog import ProjectCatalog

        return ProjectCatalog(self.root / "projects.json")

    def test_a_removed_folder_leaves_the_library(self):
        catalog = self.catalog()
        catalog.add(str(self.shoot))
        project_id = catalog.public()["projects"][0]["id"]
        catalog.remove(project_id)
        self.assertEqual(catalog.public()["projects"], [])

    def test_the_photographs_survive(self):
        catalog = self.catalog()
        catalog.add(str(self.shoot))
        catalog.remove(catalog.public()["projects"][0]["id"])
        self.assertEqual(len(list(self.shoot.glob("*.JPG"))), 3)

    def test_the_project_manifest_survives_so_the_work_can_come_back(self):
        catalog = self.catalog()
        record = catalog.add(str(self.shoot))
        manifest = Path(str(record.get("project", "")))
        catalog.remove(catalog.public()["projects"][0]["id"])
        if manifest.name:
            self.assertTrue(manifest.is_file())

    def test_adding_the_folder_again_restores_it(self):
        catalog = self.catalog()
        catalog.add(str(self.shoot))
        catalog.remove(catalog.public()["projects"][0]["id"])
        catalog.add(str(self.shoot))
        self.assertEqual(len(catalog.public()["projects"]), 1)

    def test_removing_an_unknown_project_is_refused(self):
        from opencull_gui.project_catalog import ProjectCatalogError

        with self.assertRaises(ProjectCatalogError):
            self.catalog().remove("nope")

    def test_removal_survives_a_restart(self):
        catalog = self.catalog()
        catalog.add(str(self.shoot))
        catalog.remove(catalog.public()["projects"][0]["id"])
        self.assertEqual(self.catalog().public()["projects"], [])

if __name__ == "__main__":
    unittest.main()
