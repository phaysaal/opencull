"""The project shell: one window per shoot, its phases along the top."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel, QWidget
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui import phases  # noqa: E402
from tests.test_phases import project  # noqa: E402


def culled(**changes) -> dict:
    return project(**{"report_available": True, "available": True, **changes})


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class PhaseBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def bar(self, plan=None):
        from opencull_qt.shell import PhaseBar

        bar = PhaseBar()
        self.addCleanup(bar.deleteLater)
        bar.show_plan(plan if plan is not None else phases.plan(
            project(), manifest={}))
        return bar

    def labels(self, bar) -> list[str]:
        return [button.text() for button in bar._buttons.values()]

    def test_an_invitations_press_carries_no_qt_checked_flag(self):
        """Qt's checked bool must never land in a handler's bound default."""
        from opencull_qt.shell import Invitation

        seen = []

        def handler(job="the-real-id"):
            seen.append(job)

        invitation = Invitation(
            "Paused", "body", "Resume", handler)
        self.addCleanup(invitation.deleteLater)
        invitation.button.click()
        self.assertEqual(seen, ["the-real-id"])

        other_seen = []
        second = Invitation(
            "Paused", "body", "Resume", lambda: None,
            instead=("Instead", lambda job="second-id":
                     other_seen.append(job)))
        self.addCleanup(second.deleteLater)
        second.other.click()
        self.assertEqual(other_seen, ["second-id"])

    def test_the_phases_are_numbered_in_the_order_they_happen(self):
        numbers = [text.split()[0] for text in self.labels(self.bar())]
        self.assertEqual(
            numbers, [str(index + 1) for index in range(len(phases.ORDER))])

    def test_choosing_an_open_phase_asks_for_it(self):
        bar = self.bar()
        chosen = []
        bar.chosen.connect(chosen.append)
        bar._pressed(phases.CULL)
        self.assertEqual(chosen, [phases.CULL])

    def test_choosing_a_blocked_phase_says_why_instead_of_nothing(self):
        bar = self.bar()
        chosen, refused = [], []
        bar.chosen.connect(chosen.append)
        bar.refused.connect(refused.append)
        bar._pressed(phases.EXPORT)
        self.assertEqual(chosen, [])
        self.assertIn("Develop a frame first", refused[0])

    def test_blocked_and_ready_differ_by_more_than_a_shade_of_grey(self):
        # The two dimmest readable greys in the palette are nearly the same
        # colour, so the state has to be carried by a mark as well.
        bar = self.bar()
        ready = bar._buttons[phases.CULL].text()
        blocked = bar._buttons[phases.EXPORT].text()
        self.assertTrue(ready.endswith("▸"))
        self.assertFalse(blocked.endswith("▸"))

    def test_a_finished_phase_is_marked_as_finished(self):
        bar = self.bar(phases.plan(culled(), manifest={}))
        self.assertTrue(bar._buttons[phases.CULL].text().endswith("✓"))

    def test_a_blocked_phase_carries_its_reason_as_its_tooltip(self):
        bar = self.bar()
        self.assertIn(
            "has none yet", bar._buttons[phases.ASSESSMENT].toolTip())

    def test_replacing_the_phases_keeps_the_pages_own_counter(self):
        bar = self.bar()
        counter = QLabel("12 of 23")
        bar.set_indicator(counter)
        bar.show_plan(phases.plan(culled(), manifest={}))
        self.assertEqual(counter.text(), "12 of 23")
        self.assertIsNotNone(counter.parent())


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class FirstOpenTests(unittest.TestCase):
    def first(self, project_value, **kwargs) -> str:
        from opencull_qt.shell import first_open

        return first_open(phases.plan(project_value, manifest={}, **kwargs))

    def test_a_fresh_folder_opens_on_the_cull(self):
        self.assertEqual(self.first(project(available=True)), phases.CULL)

    def test_a_culled_folder_opens_on_the_assessment(self):
        self.assertEqual(self.first(culled()), phases.ASSESSMENT)

    def test_it_never_lands_on_the_profile_which_is_not_the_shoots(self):
        for value in (project(available=True), culled()):
            self.assertNotEqual(self.first(value), phases.PROFILE)

    def test_a_finished_shoot_opens_on_its_own_debrief(self):
        from opencull_qt.shell import first_open

        plan = phases.plan(
            culled(shortlist_available=True), marked=2,
            manifest={"artifacts": {
                "edit_directions": [{}],
                "renders": [{"adjustments": ["op-001"]}],
                "exports": [{}]}})
        # Everything is delivered; the one thing left is reading it back.
        self.assertEqual(first_open(plan), phases.DEBRIEF)


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class ProjectShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def shell(self, project_value=None, plan=None):
        from opencull_qt.shell import ProjectShell

        self.built: list[str] = []

        def build(key: str):
            self.built.append(key)
            page = QWidget()
            page.indicator = QLabel(key)
            return page

        value = project_value if project_value is not None else culled()
        shell = ProjectShell(value, build)
        self.addCleanup(shell.deleteLater)
        shell.show_plan(plan if plan is not None else phases.plan(
            value, manifest={}))
        return shell

    def test_the_shoot_is_named_in_the_chrome(self):
        shell = self.shell(culled(name="A Journey"))
        self.assertEqual(shell.title.text(), "A JOURNEY")

    def test_a_phase_is_built_once_and_then_kept(self):
        shell = self.shell()
        shell.open_phase(phases.CULL)
        page = shell.page_for(phases.CULL)
        shell.open_phase(phases.DEVELOPMENT)
        shell.open_phase(phases.CULL)
        self.assertEqual(self.built.count(phases.CULL), 1)
        self.assertIs(shell.page_for(phases.CULL), page)

    def test_a_blocked_phase_is_never_built(self):
        shell = self.shell()
        self.assertFalse(shell.open_phase(phases.EXPORT))
        self.assertEqual(self.built, [])
        self.assertIn("Develop a frame first", shell.status.text())

    def test_the_open_pages_counter_moves_into_the_phase_row(self):
        shell = self.shell()
        shell.open_phase(phases.CULL)
        indicator = shell.page_for(phases.CULL).indicator
        self.assertEqual(shell.bar._tail_row.itemAt(0).widget(), indicator)

    def test_an_invitation_is_replaced_once_its_run_has_happened(self):
        from opencull_qt.shell import Invitation, ProjectShell

        pages = {phases.CULL: lambda: Invitation(
            "Not culled", "body", "Cull", lambda: None)}
        built: list[str] = []

        def build(key: str):
            built.append(key)
            maker = pages.get(key)
            return maker() if maker else QWidget()

        shell = ProjectShell(project(available=True), build)
        self.addCleanup(shell.deleteLater)
        shell.show_plan(phases.plan(project(available=True), manifest={}))
        shell.open_phase(phases.CULL)
        self.assertIsInstance(shell.page_for(phases.CULL), Invitation)

        # The cull finishes somewhere else; the bar has to notice.
        pages.clear()
        shell.show_plan(phases.plan(culled(), manifest={}))
        self.assertNotIsInstance(shell.page_for(phases.CULL), Invitation)
        self.assertEqual(built.count(phases.CULL), 2)

    def test_a_page_that_cannot_be_built_leaves_the_shell_standing(self):
        from opencull_qt.shell import ProjectShell

        shell = ProjectShell(culled(), lambda key: None)
        self.addCleanup(shell.deleteLater)
        shell.show_plan(phases.plan(culled(), manifest={}))
        self.assertFalse(shell.open_phase(phases.CULL))
        self.assertEqual(shell.current, "")

    def test_leaving_shuts_down_every_page_it_built(self):
        closed: list[str] = []

        class Page(QWidget):
            def __init__(self, key):
                super().__init__()
                self.key = key

            def shutdown(self):
                closed.append(self.key)

        from opencull_qt.shell import ProjectShell

        shell = ProjectShell(culled(), lambda key: Page(key))
        self.addCleanup(shell.deleteLater)
        shell.show_plan(phases.plan(culled(), manifest={}))
        shell.open_phase(phases.CULL)
        shell.open_phase(phases.DEVELOPMENT)
        shell.shutdown()
        self.assertEqual(sorted(closed), [phases.CULL, phases.DEVELOPMENT])


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class SelectionIsNotACullTests(unittest.TestCase):
    """A selection written locally must never be reported as a cull."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def bench(self, mode: str):
        import json

        from opencull_qt.bench import Bench
        from tests.test_qt_develop import build_shoot

        report_path, photos = build_shoot(self.root)
        value = json.loads(report_path.read_text(encoding="utf-8"))
        value["adaptive_clustering"] = {"enabled": False, "mode": mode}
        report_path.write_text(json.dumps(value), encoding="utf-8")
        return Bench(
            {"id": "p1", "photos": str(photos), "report_available": True},
            self.root / "cache", report_path)

    def test_an_everything_included_selection_is_not_a_cull(self):
        self.assertFalse(self.bench("manual-selection").culled())

    def test_a_real_cull_reads_as_one(self):
        self.assertTrue(self.bench("adaptive").culled())

    def test_the_phase_bar_believes_the_report_not_the_catalog(self):
        plan = phases.plan(culled(), culled=False, manifest={})
        state = next(item["state"] for item in plan if item["id"] == phases.CULL)
        self.assertEqual(state, "ready")


if __name__ == "__main__":
    unittest.main()
