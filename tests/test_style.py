"""The personal style profile: which one is in use, and building a new one."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui.style import (  # noqa: E402
    StyleProfileError,
    StyleProfileStore,
    profile_summary,
    read_profile,
)

PROFILE = {
    "format": "opencull-personal-style-profile-v1",
    "revision": 2,
    "updated_at": "2026-02-01T10:00:00+00:00",
    "examples": ["a.jpg", "b.jpg", "c.jpg"],
    "profile": {
        "profile_name": "Warm documentary",
        "visual_signature": "Warm, low-contrast, generous shadows.",
        "tonal_preferences": "Protects highlights; never crushes black.",
        "color_preferences": "Slightly warm, restrained saturation.",
        "avoid_or_guardrails": "No heavy clarity on skin.",
        "confidence": 0.82,
    },
}


def write_profile(path: Path, name="Warm documentary") -> Path:
    value = json.loads(json.dumps(PROFILE))
    value["profile"]["profile_name"] = name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class StyleProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.store = StyleProfileStore(
            self.root / "style-profile.json", self.results)
        self.addCleanup(self._temporary.cleanup)

    def test_nothing_is_in_use_to_begin_with(self):
        state = self.store.public()
        self.assertEqual(state["selected"], "")
        self.assertIsNone(state["profile"])
        self.assertEqual(state["available"], [])

    def test_a_profile_can_be_chosen_and_is_remembered(self):
        path = write_profile(self.results / "personal-style-1.json")
        self.store.select(path)
        fresh = StyleProfileStore(
            self.root / "style-profile.json", self.results)
        self.assertEqual(fresh.selected(), str(path.resolve()))
        self.assertEqual(fresh.public()["profile"]["name"], "Warm documentary")

    def test_something_that_is_not_a_profile_is_refused(self):
        stranger = self.results / "notes.json"
        stranger.write_text('{"format": "something else"}', encoding="utf-8")
        with self.assertRaises(StyleProfileError):
            self.store.select(stranger)
        self.assertEqual(self.store.selected(), "")

    def test_a_profile_that_has_gone_is_not_in_use(self):
        path = write_profile(self.results / "personal-style-1.json")
        self.store.select(path)
        path.unlink()
        # The selection file still names it; that does not make it in use.
        self.assertEqual(self.store.selected(), "")
        self.assertIsNone(self.store.public()["profile"])

    def test_stopping_leaves_the_profile_on_disk(self):
        path = write_profile(self.results / "personal-style-1.json")
        self.store.select(path)
        self.store.forget()
        self.assertEqual(self.store.selected(), "")
        self.assertTrue(path.is_file())
        self.assertEqual(len(self.store.available()), 1)

    def test_profiles_are_listed_newest_first(self):
        import time

        old = write_profile(self.results / "personal-style-1.json", "Older")
        time.sleep(0.01)
        new = write_profile(self.results / "personal-style-2.json", "Newer")
        os.utime(new, (time.time() + 5, time.time() + 5))
        os.utime(old, (time.time(), time.time()))
        self.assertEqual(
            [item["name"] for item in self.store.available()],
            ["Newer", "Older"])

    def test_other_json_in_the_results_folder_is_not_a_profile(self):
        (self.results / "shoot-results.json").write_text(
            '{"format": "opencull-report-v2"}', encoding="utf-8")
        (self.results / "broken.json").write_text("{not json", encoding="utf-8")
        write_profile(self.results / "personal-style-1.json")
        self.assertEqual(len(self.store.available()), 1)

    def test_a_new_profile_is_named_for_when_it_was_made(self):
        # Old profiles are kept, because a render made under one has to stay
        # explainable after the opinion changes.
        self.assertEqual(
            self.store.suggested_output("20260201-100000").name,
            "personal-style-20260201-100000.json")

    def test_the_selection_file_is_not_world_readable(self):
        path = write_profile(self.results / "personal-style-1.json")
        self.store.select(path)
        self.assertEqual(
            self.store.path.stat().st_mode & 0o077, 0,
            "the selection file names a path in the user's home")

    def test_a_summary_reads_the_profile_in_a_human_order(self):
        summary = profile_summary(read_profile(
            write_profile(self.results / "personal-style-1.json")))
        self.assertEqual(summary["name"], "Warm documentary")
        self.assertAlmostEqual(summary["confidence"], 0.82)
        self.assertEqual(summary["examples"], 3)
        labels = [label for label, _ in summary["fields"]]
        self.assertEqual(labels[0], "Visual signature")
        self.assertIn("What you avoid", labels)
        # Empty fields are not shown as empty headings.
        self.assertNotIn("Composition", labels)


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class StyleDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        from unittest import mock

        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.results = self.root / "results"
        self.results.mkdir()
        # The panel stages photographs that exist, so the fixture makes
        # them exist. Their content never matters; only their presence.
        for name in ("a.jpg", "b.jpg"):
            (self.root / name).write_bytes(b"jpeg")
        self.store = StyleProfileStore(
            self.root / "style-profile.json", self.results)
        self.jobs = mock.Mock()
        self.providers = mock.Mock()
        self.providers.public.return_value = {
            "profiles": [{"id": "p1", "name": "OpenRouter"}]}
        self.addCleanup(self._temporary.cleanup)

    def dialog(self):
        from opencull_qt.style import StylePanel

        page = StylePanel(self.store, self.jobs, self.providers)
        self.addCleanup(page.deleteLater)
        return page

    def test_a_running_profile_says_what_it_is_doing(self):
        panel = self.dialog()
        panel.show_run({
            "kind": "style_profile", "status": "running",
            "progress": {"fraction": 0.6,
                         "stage": "Asking the profile model to learn "
                                  "your editing style"}})
        self.assertTrue(panel.meter.isVisibleTo(panel))
        self.assertEqual(panel.meter.value(), 60)
        self.assertIn("learn your editing style", panel.stage.text())
        self.assertFalse(panel.build_button.isEnabled())

    def test_a_finished_profile_puts_its_meter_away_and_says_so(self):
        panel = self.dialog()
        panel.show_run({"id": "s1", "kind": "style_profile",
                        "status": "running",
                        "progress": {"fraction": 0.6, "stage": "x"}})
        panel.show_run({"id": "s1", "kind": "style_profile",
                        "status": "completed",
                        "progress": {"fraction": 1.0}})
        self.assertFalse(panel.meter.isVisibleTo(panel))
        self.assertTrue(panel.build_button.isEnabled())
        self.assertIn("ready", panel.status.text())

    def test_an_ended_run_is_reported_once_not_on_every_poll(self):
        panel = self.dialog()
        panel.show_run({"id": "s1", "kind": "style_profile",
                        "status": "failed", "message": "exited"})
        self.assertIn("stopped before it finished", panel.status.text())
        panel._report("", "")
        for _ in range(3):
            panel.show_run({"id": "s1", "kind": "style_profile",
                            "status": "failed", "message": "exited"})
        self.assertEqual(panel.status.text(), "")

    def test_a_failed_profile_says_so_rather_than_going_quiet(self):
        panel = self.dialog()
        panel.show_run({
            "id": "s9", "kind": "style_profile", "status": "failed",
            "message": "Kimiya exited with status 2."})
        self.assertIn("stopped before it finished", panel.status.text())

    def test_with_no_profile_it_says_what_is_missing(self):
        dialog = self.dialog()
        shown = self._text(dialog)
        self.assertIn("No style profile is in use", shown)
        self.assertIn("standard, signature and creative", shown)

    def test_a_profile_reads_in_full_once_it_is_opened(self):
        write_profile(self.results / "personal-style-1.json")
        self.store.select(self.results / "personal-style-1.json")
        dialog = self.dialog()

        # The page opens as a shelf: the reading waits to be asked for.
        self.assertFalse(dialog.detail_scroll.isVisibleTo(dialog))
        dialog.show_profile(0)

        shown = self._text(dialog)
        self.assertIn("Warm documentary", shown)
        self.assertIn("Warm, low-contrast, generous shadows.", shown)
        self.assertIn("No heavy clarity on skin.", shown)
        # And a second click puts it away again.
        dialog.show_profile(0)
        self.assertFalse(dialog.detail_scroll.isVisibleTo(dialog))

    def test_a_staged_set_survives_a_look_at_another_profile(self):
        write_profile(self.results / "personal-style-1.json")
        dialog = self.dialog()
        dialog.refresh()
        self.stage(dialog, [self.root / "a.jpg", self.root / "b.jpg"])
        self.assertEqual(len(dialog.examples), 2)

        dialog.show_profile(0)
        dialog.show_profile(0)

        # What the photographer chose is still chosen.
        self.assertEqual(len(dialog.examples), 2)
        self.assertEqual(dialog.build_button.text(), "Extract profile")

    def test_a_finished_run_leaves_the_shelf_not_its_working_set(self):
        dialog = self.dialog()
        self.stage(dialog, [self.root / "a.jpg", self.root / "b.jpg"])
        self.assertTrue(dialog.examples_scroll.isVisibleTo(dialog))

        dialog.show_run({"id": "s1", "kind": "style_profile",
                         "status": "running",
                         "progress": {"fraction": 0.6, "stage": "x"}})
        dialog.show_run({"id": "s1", "kind": "style_profile",
                         "status": "completed",
                         "progress": {"fraction": 1.0}})

        # The photographs have been read; nothing invites reading them
        # again, and the page is back to its shelf.
        self.assertEqual(dialog.examples, [])
        self.assertFalse(dialog.examples_scroll.isVisibleTo(dialog))
        self.assertFalse(dialog.add_button.isVisibleTo(dialog))
        self.assertTrue(dialog.create_button.isVisibleTo(dialog))

    def test_building_without_a_provider_says_why_rather_than_failing(self):
        self.providers.public.return_value = {"profiles": []}
        dialog = self.dialog()
        dialog.build()
        self.jobs.add_style_profile.assert_not_called()
        self.assertIn("Configure a model provider", dialog.status.text())

    def stage(self, dialog, picked):
        """Choose photographs the way the panel now asks for them."""
        from unittest import mock

        with mock.patch(
            "opencull_qt.style.QFileDialog.getOpenFileNames",
            return_value=([str(path) for path in picked], ""),
        ):
            dialog.add_examples()

    def test_the_page_stays_gathered_when_both_panes_are_put_away(self):
        """Spare height belongs at the foot, not shared out between labels."""
        from PySide6.QtWidgets import QLayout

        dialog = self.dialog()
        write_profile(self.results / "personal-style-1.json")
        dialog.refresh()
        dialog.resize(1000, 900)
        layout = dialog.layout()
        last = layout.itemAt(layout.count() - 1)
        self.assertIsNone(last.widget())
        self.assertTrue(last.spacerItem() is not None)
        self.assertEqual(
            layout.sizeConstraint(), QLayout.SizeConstraint.SetDefaultConstraint)

    def test_an_empty_page_offers_to_start_rather_than_saying_it_is_empty(
            self):
        dialog = self.dialog()
        self.assertTrue(dialog.create_button.isVisibleTo(dialog))
        # Nothing else in that band competes with the offer.
        self.assertFalse(dialog.examples_scroll.isVisibleTo(dialog))
        self.assertFalse(dialog.build_button.isVisibleTo(dialog))
        self.assertFalse(dialog.add_button.isVisibleTo(dialog))

        self.stage(dialog, [self.root / "a.jpg"])

        # Once something is chosen, the working controls take over.
        self.assertFalse(dialog.create_button.isVisibleTo(dialog))
        self.assertTrue(dialog.examples_scroll.isVisibleTo(dialog))
        self.assertTrue(dialog.build_button.isVisibleTo(dialog))
        self.assertIn("PHOTOGRAPHS TO READ", dialog.examples_title.text())

    def test_clearing_a_set_offers_to_start_again(self):
        dialog = self.dialog()
        self.stage(dialog, [self.root / "a.jpg"])
        dialog.clear_examples()
        self.assertTrue(dialog.create_button.isVisibleTo(dialog))

    def test_photographs_a_run_could_not_read_are_named_and_offered(self):
        import json as json_module

        dialog = self.dialog()
        path = self.results / "personal-style-1.json"
        write_profile(path)
        value = json_module.loads(path.read_text())
        value["unread"] = [{
            "photos": [str(self.root / "a.jpg"), str(self.root / "b.jpg")],
            "reason": "Two answers came back and neither could be read."}]
        path.write_text(json_module.dumps(value))
        dialog.refresh()

        dialog.show_profile(0)

        shown = dialog.unread_note.text()
        self.assertIn("2 photographs could not be read", shown)
        self.assertIn("a.jpg", shown)
        self.assertIn("neither could be read", shown)
        self.assertTrue(dialog.retry_button.isVisibleTo(dialog))

        dialog.retry_unread()

        # Exactly those are staged against that profile, nothing else.
        self.assertEqual(
            sorted(str(path) for path in dialog.staged),
            sorted([str(self.root / "a.jpg"), str(self.root / "b.jpg")]))
        self.assertIn("Refine with 2", dialog.build_button.text())

    def test_leaving_unread_photographs_out_keeps_the_record(self):
        import json as json_module

        dialog = self.dialog()
        path = self.results / "personal-style-1.json"
        write_profile(path)
        value = json_module.loads(path.read_text())
        value["unread"] = [{"photos": [str(self.root / "a.jpg")],
                            "reason": "unreadable"}]
        path.write_text(json_module.dumps(value))
        dialog.refresh()
        dialog.show_profile(0)

        dialog.ignore_unread()

        self.assertFalse(dialog.unread_note.isVisibleTo(dialog))
        self.assertIn("stay recorded as unread", dialog.status.text())
        # The profile still says what it could not read.
        self.assertEqual(len(dialog.available[0]["unread"]), 1)

    def test_each_profile_carries_its_own_rename_and_remove(self):
        from PySide6.QtWidgets import QPushButton

        dialog = self.dialog()
        write_profile(self.results / "personal-style-1.json")
        dialog.refresh()
        labels = [b.text() for b in dialog.cards[0].findChildren(QPushButton)]
        self.assertEqual(labels, ["Rename", "Remove"])

    def test_removing_a_profile_keeps_it_off_the_shelf_but_on_disk(self):
        from unittest import mock

        dialog = self.dialog()
        write_profile(self.results / "personal-style-1.json")
        self.store.select(self.results / "personal-style-1.json")
        dialog.refresh()

        # Refused, nothing happens.
        with mock.patch.object(dialog, "_confirm_retire", return_value=False):
            dialog.retire(0)
        self.assertEqual(len(dialog.available), 1)

        with mock.patch.object(dialog, "_confirm_retire", return_value=True):
            dialog.retire(0)

        self.assertEqual(dialog.available, [])
        self.assertFalse((self.results / "personal-style-1.json").is_file())
        self.assertTrue(
            (self.results / "Retired profiles"
             / "personal-style-1.json").is_file())
        self.assertEqual(self.store.selected(), "")
        self.assertIn("kept", dialog.status.text())

    def test_profiles_are_shown_as_their_photographs(self):
        from PySide6.QtWidgets import QLabel

        dialog = self.dialog()
        write_profile(self.results / "personal-style-1.json")
        dialog.refresh()
        self.assertEqual(len(dialog.cards), 1)
        card = dialog.cards[0]
        self.assertIn("photograph", card.findChildren(QLabel)[-1].text())

    def test_reading_a_profile_shows_it_without_adopting_it(self):
        dialog = self.dialog()
        write_profile(self.results / "personal-style-1.json")
        dialog.refresh()

        dialog.show_profile(0)

        # Every profile is equally available to the suggestion phase, so
        # looking at one settles nothing here.
        self.assertIn("PHOTOGRAPHS BEHIND", dialog.examples_title.text())
        self.assertEqual(self.store.selected(), "")

    def test_a_profile_is_named_for_the_folder_it_was_read_from(self):
        from opencull_gui.style import StyleProfileStore

        self.assertEqual(
            StyleProfileStore.group_name(
                ["/p/Alps Tour/a.jpg", "/p/Alps Tour/b.jpg"]), "Alps Tour")
        # Gathered from several folders, it says so rather than choosing.
        self.assertEqual(
            StyleProfileStore.group_name(["/p/one/a.jpg", "/p/two/b.jpg"]),
            "2 folders")
        self.assertEqual(StyleProfileStore.group_name([]), "")

    def test_an_old_profile_shows_the_photographs_behind_it(self):
        dialog = self.dialog()
        write_profile(self.results / "personal-style-1.json")
        dialog.refresh()
        self.assertTrue(dialog.available)

        dialog.show_group(0)

        # Its own group replaces whatever was staged, and the button
        # offers to refine rather than to start again.
        self.assertIn("PHOTOGRAPHS BEHIND", dialog.examples_title.text())
        self.assertEqual(dialog.build_button.text(), "Refine this profile")
        # Photographs added while it is open refine that profile, and
        # the button says what it will read.
        self.stage(dialog, [self.root / "a.jpg"])
        self.assertIn("Refine with 1", dialog.build_button.text())

    def test_chosen_photographs_are_staged_and_shown_before_any_run(self):
        dialog = self.dialog()
        self.stage(dialog, [self.root / "a.jpg", self.root / "b.jpg"])
        self.assertEqual(len(dialog.examples), 2)
        self.jobs.add_style_profile.assert_not_called()
        # The same photograph chosen twice is staged once.
        self.stage(dialog, [self.root / "a.jpg"])
        self.assertEqual(len(dialog.examples), 2)
        # And it can be taken out again.
        dialog.drop_example(str(self.root / "a.jpg"))
        self.assertEqual(len(dialog.examples), 1)

    def test_building_reads_the_photographs_that_were_chosen(self):
        dialog = self.dialog()
        picked = [str(self.root / "a.jpg"), str(self.root / "b.jpg")]
        self.stage(dialog, picked)
        dialog.build()
        self.jobs.add_style_profile.assert_called_once()
        arguments = self.jobs.add_style_profile.call_args
        self.assertEqual(arguments[0][0], picked)
        self.assertEqual(arguments[1]["mode"], "replace")
        self.assertEqual(arguments[1]["existing"], "")

    def test_a_second_profile_stands_beside_the_first(self):
        """A photographer has more than one taste; profiles do not replace."""
        write_profile(self.results / "personal-style-1.json")
        self.store.select(self.results / "personal-style-1.json")
        dialog = self.dialog()
        dialog.refresh()

        self.stage(dialog, [self.root / "a.jpg"])
        dialog.build()

        arguments = self.jobs.add_style_profile.call_args
        # Nothing is asked, nothing is refined, nothing is overwritten:
        # a new profile is read from the new photographs alone.
        self.assertEqual(arguments[1]["mode"], "replace")
        self.assertEqual(arguments[1]["existing"], "")
        self.assertNotEqual(
            arguments[1]["output"], str(self.results / "personal-style-1.json"))
        self.assertIn("beside the ones you already have",
                      dialog.status.text())

    def test_refining_reads_only_the_photographs_newly_added(self):
        write_profile(self.results / "personal-style-1.json")
        dialog = self.dialog()
        dialog.refresh()
        dialog.show_profile(0)

        # With nothing new, there is nothing to refine with.
        dialog.build()
        self.jobs.add_style_profile.assert_not_called()
        self.assertIn("Add the photographs", dialog.status.text())

        self.stage(dialog, [self.root / "a.jpg"])
        self.assertIn("Refine with 1", dialog.build_button.text())
        dialog.build()

        arguments = self.jobs.add_style_profile.call_args
        self.assertEqual(arguments[0][0], [str(self.root / "a.jpg")])
        self.assertEqual(arguments[1]["mode"], "update")
        self.assertEqual(
            arguments[1]["existing"], str(self.results / "personal-style-1.json"))

    def test_more_photographs_than_are_read_is_said_when_added(self):
        from opencull_qt.style import EXAMPLE_LIMIT

        dialog = self.dialog()
        picked = []
        for index in range(EXAMPLE_LIMIT + 10):
            path = self.root / f"{index}.jpg"
            path.write_bytes(b"jpeg")
            picked.append(path)

        self.stage(dialog, picked)

        # The cap is said where it bites, not after a run has quietly
        # read a subset of what was chosen.
        self.assertEqual(len(dialog.examples), EXAMPLE_LIMIT)
        self.assertIn(str(EXAMPLE_LIMIT), dialog.status.text())
        dialog.build()
        self.assertEqual(
            len(self.jobs.add_style_profile.call_args[0][0]), EXAMPLE_LIMIT)

    @staticmethod
    def _text(dialog) -> str:
        from PySide6.QtWidgets import QLabel

        return "\n".join(
            label.text() for label in dialog.findChildren(QLabel))


if __name__ == "__main__":
    unittest.main()


class NamingTests(unittest.TestCase):
    """Several profiles must be tellable apart."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.store = StyleProfileStore(
            self.root / "style-profile.json", self.results)
        write_profile(self.results / "personal-style-1.json")
        write_profile(self.results / "personal-style-2.json")
        self.addCleanup(self._temporary.cleanup)

    def path(self, number: int) -> Path:
        return self.results / f"personal-style-{number}.json"

    def test_a_named_profile_lists_under_its_name(self):
        self.store.set_name(self.path(1), "Hard editorial")
        names = {item["path"]: item["name"]
                 for item in self.store.available()}
        self.assertEqual(names[str(self.path(1))], "Hard editorial")

    def test_the_name_survives_selecting_and_forgetting(self):
        self.store.set_name(self.path(1), "Hard editorial")
        self.store.select(self.path(2))
        self.store.forget()
        self.assertEqual(self.store.name_for(self.path(1)), "Hard editorial")

    def test_the_selected_profiles_summary_carries_the_name(self):
        self.store.set_name(self.path(2), "Muted film")
        self.store.select(self.path(2))
        self.assertEqual(self.store.public()["profile"]["name"], "Muted film")

    def test_an_empty_name_goes_back_to_the_profiles_own(self):
        self.store.set_name(self.path(1), "Hard editorial")
        self.store.set_name(self.path(1), "  ")
        self.assertEqual(self.store.name_for(self.path(1)), "")

    def test_a_name_cannot_be_hung_on_a_file_that_is_not_a_profile(self):
        stray = self.results / "not-a-profile.json"
        stray.write_text("{}", encoding="utf-8")
        with self.assertRaises(StyleProfileError):
            self.store.set_name(stray, "anything")


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class StudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        from unittest import mock

        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.store = StyleProfileStore(
            self.root / "style-profile.json", self.results)
        self.jobs = mock.Mock()
        self.providers = mock.Mock()
        self.providers.public.return_value = {"profiles": []}
        self.addCleanup(self._temporary.cleanup)

    def studio(self, open_providers=None):
        from opencull_qt.studio import StudioPage

        page = StudioPage(
            self.store, self.jobs, self.providers,
            open_providers or (lambda: None))
        self.addCleanup(page.deleteLater)
        return page

    def text(self, page) -> str:
        from PySide6.QtWidgets import QLabel

        return "\n".join(
            label.text() for label in page.findChildren(QLabel))

    def test_the_studio_holds_the_photographers_three_things(self):
        shown = self.text(self.studio())
        self.assertIn("PERSONAL PROFILES", shown)
        self.assertIn("THE TASTE LEDGER", shown)
        self.assertIn("PROVIDERS", shown)

    def test_the_ledger_is_honest_about_being_reserved(self):
        self.assertIn("Not built yet", self.text(self.studio()))

    def test_the_providers_door_opens_the_providers(self):
        opened = []
        page = self.studio(lambda: opened.append(True))
        from PySide6.QtWidgets import QPushButton

        button = next(
            b for b in page.findChildren(QPushButton)
            if b.text() == "Providers…")
        button.click()
        self.assertTrue(opened)

    def test_the_profile_panel_inside_is_the_real_one(self):
        write_profile(self.results / "personal-style-1.json")
        page = self.studio()
        page.panel.refresh()
        self.assertEqual(len(page.panel.available), 1)
