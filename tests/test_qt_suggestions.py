"""The suggestions phase: what was proposed, and what is still to ask for."""

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
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui.directions import DirectionsIndex  # noqa: E402
from opencull_gui.project import ensure_project_layout  # noqa: E402
from opencull_gui.report import load_report  # noqa: E402
from opencull_gui.shortlist import load_shortlist  # noqa: E402
from opencull_gui.shortlist_reviews import (  # noqa: E402
    ShortlistReviewError,
    ShortlistReviewStore,
    default_shortlist_review_path,
)
from opencull_qt.previews import PreviewLoader  # noqa: E402
from tests.test_qt_develop import (  # noqa: E402
    NAMES,
    assess_and_suggest,
    build_shoot,
)


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class SuggestionsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.report_path, self.photos = build_shoot(self.root)
        self.addCleanup(self._temporary.cleanup)

    def page(self, marked=(NAMES[0],), styles=("standard", "signature"),
             suggested=True, answered=None):
        """A shoot marked for development, some of it already answered.

        ``marked`` is what the photographer marked; ``answered`` is what the
        suggestion pass has come back about. They are not the same list, and
        the difference is the whole reason the page has a cost dialog.
        """
        from opencull_qt.suggestions import SuggestionsPage

        shortlist_path = assess_and_suggest(
            self.root, self.report_path, self.photos,
            marked=marked if answered is None else answered, styles=styles)
        if not suggested:
            for stale in ensure_project_layout(self.photos)["Recipes"].glob(
                    "*edit-directions*"):
                stale.unlink()
        report = load_report(self.report_path)
        shortlist = load_shortlist(shortlist_path, report, self.photos)
        # The store, not a hand-written file, decides what a valid review of
        # this shortlist looks like.
        default_shortlist_review_path(shortlist_path).unlink(missing_ok=True)
        reviews = ShortlistReviewStore(
            default_shortlist_review_path(shortlist_path), shortlist)
        # Marked through the store rather than by writing its file, so the
        # marks carry the revision the store itself would give them.
        for photo in marked:
            reviews.update(
                photo, "strong", False, "", True,
                reviews.public_state()["revision"], interesting=True)
        directions = DirectionsIndex(
            shortlist, reviews, ensure_project_layout(self.photos)["Recipes"])
        loader = PreviewLoader(self.photos)
        page = SuggestionsPage(shortlist, reviews, directions, loader=loader)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def text(self, page) -> str:
        return "\n".join(
            label.text() for label in page.findChildren(QLabel))

    def test_only_the_marked_frames_are_listed(self):
        page = self.page(marked=(NAMES[0], NAMES[1]))
        self.assertEqual(page.photos, sorted([NAMES[0], NAMES[1]]))

    def test_the_directions_are_read_once_per_refresh_not_once_per_frame(self):
        # Regression: opening a folder re-read every recipe file once per
        # marked frame -- the whole-shortlist payload, recomputed inside
        # each frame's lookup -- which made a 300-frame eclipse folder take
        # eighteen seconds to open. The read is held and shared now, so a
        # refresh computes it exactly once however many frames are listed.
        page = self.page(marked=(NAMES[0], NAMES[1], NAMES[2]))
        self.assertGreaterEqual(len(page.photos), 3)
        with mock.patch.object(page.directions, "payload",
                               wraps=page.directions.payload) as spy:
            page.refresh()
        self.assertEqual(spy.call_count, 1)

    def test_the_reasoning_is_shown_not_just_the_treatment_name(self):
        shown = self.text(self.page())
        self.assertIn("Standard treatment", shown)
        self.assertIn("what standard is for", shown)
        self.assertIn("WHAT IT IS TRYING TO DO", shown)

    def test_the_recipe_that_will_run_is_shown_too(self):
        self.assertIn("increase exposure by 0.2 stops", self.text(self.page()))

    def test_guardrails_are_shown_once_as_true_of_every_treatment(self):
        shown = self.text(self.page())
        self.assertIn("keep skin believable", shown)
        self.assertEqual(shown.count("keep skin believable"), 1)

    def test_a_treatment_with_no_recipe_says_it_cannot_be_rendered(self):
        from opencull_qt.suggestions import Treatment

        card = Treatment("creative", {
            "creative_title": "Bold", "creative_intent": "go further",
            "creative_recipe": ""})
        self.addCleanup(card.deleteLater)
        shown = "\n".join(label.text() for label in card.findChildren(QLabel))
        self.assertIn("cannot be rendered", shown)

    def test_a_frame_with_nothing_yet_says_so_rather_than_showing_blank(self):
        page = self.page(suggested=False)
        self.assertIn("Nothing has been suggested", self.text(page))

    def test_the_counter_says_how_many_are_answered(self):
        page = self.page(marked=(NAMES[0], NAMES[1]), answered=(NAMES[0],))
        self.assertIn("1 of 2 marked frames answered", page.progress.text())

    def test_nothing_marked_opens_the_chooser_rather_than_a_dead_end(self):
        page = self.page(marked=())
        self.assertFalse(page.ask_button.isEnabled())
        # The page can fix this state itself, so it opens on the choice.
        self.assertIs(page.faces.currentWidget(), page.chooser)
        self.assertIsNotNone(page.sheet)
        self.assertEqual(
            set(page.sheet.selection), set(page.shortlist.entry_by_photo))

    def test_the_chooser_proposes_strong_and_above_and_no_further(self):
        page = self.page(marked=())
        strong = {
            photo for photo, entry in page.shortlist.entry_by_photo.items()
            if str(entry.get("tier")) in {"strong", "exceptional"}}
        self.assertEqual(set(page.sheet.chosen()), strong)

    def test_ticking_frames_here_writes_the_assessments_own_mark(self):
        page = self.page(marked=())
        page.sheet.include_only([NAMES[0], NAMES[1]])
        page.apply_marks()

        marks = page.reviews.public_state()["entries"]
        self.assertTrue(marks[NAMES[0]]["interesting"])
        self.assertTrue(marks[NAMES[1]]["interesting"])
        self.assertEqual(page.photos, sorted([NAMES[0], NAMES[1]]))
        self.assertTrue(page.ask_button.isEnabled())
        self.assertIs(page.faces.currentWidget(), page.faces.widget(0))

    def test_a_mark_is_not_a_claim_to_have_reviewed_the_frame(self):
        page = self.page(marked=())
        page.sheet.include_only([NAMES[0]])
        page.apply_marks()
        self.assertFalse(
            page.reviews.public_state()["entries"][NAMES[0]]["reviewed"])

    def test_unmarking_everything_stays_where_the_work_is(self):
        page = self.page(marked=(NAMES[0],))
        page.show_chooser()
        page.sheet.set_all(False)
        page.apply_marks()
        self.assertEqual(page.photos, [])
        self.assertIs(page.faces.currentWidget(), page.chooser)
        self.assertIn("nothing to ask about", page.chooser_status.text())

    def test_unticking_a_marked_frame_takes_the_mark_off(self):
        page = self.page(marked=(NAMES[0], NAMES[1]))
        page.show_chooser()
        self.assertEqual(set(page.sheet.chosen()), {NAMES[0], NAMES[1]})
        page.sheet.include_only([NAMES[0]])
        page.apply_marks()
        self.assertEqual(page.photos, [NAMES[0]])

    def test_a_refused_frame_is_named_and_the_rest_are_still_marked(self):
        page = self.page(marked=())
        page.sheet.include_only([NAMES[0], NAMES[1]])
        real = page.reviews.update

        def refuse(photo, *arguments, **keywords):
            if photo == NAMES[1]:
                raise ShortlistReviewError("no such frame")
            return real(photo, *arguments, **keywords)

        with mock.patch.object(page.reviews, "update", side_effect=refuse):
            page.apply_marks()
        self.assertEqual(page.photos, [NAMES[0]])
        self.assertIn(NAMES[1], page.chooser_status.text())
        self.assertIn("no such frame", page.chooser_status.text())


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class AskingTests(SuggestionsPageTests):
    """Asking again costs a call per frame, so the scope is put to the user."""

    def test_a_fresh_ask_offers_the_scene_and_per_frame_prices(self):
        # A.JPG and B.JPG share a cluster, so they share a scene: the ask
        # offers one call instead of two, and the photographer chooses.
        page = self.page(marked=(NAMES[0], NAMES[1]), suggested=False)
        asked = []
        page.suggested.connect(lambda output, photos: asked.append(photos))
        with mock.patch.object(
                page, "_ask_scope", return_value="all") as scope:
            page.suggest()
        # The plan itself is handed over, not a count: what the dialog has
        # to show is which frames share an answer with which.
        (waiting, done, plan), _ = scope.call_args
        self.assertEqual((waiting, done), (2, 0))
        self.assertEqual(len(plan), 1)
        self.assertEqual(
            sorted(plan[0]["photos"]), sorted([NAMES[0], NAMES[1]]))
        self.assertEqual(asked, [sorted([NAMES[0], NAMES[1]])])

    def test_a_refused_set_is_shown_and_stamped_rather_than_hidden(self):
        """The panel's verdict is information, not a reason to hide work."""
        page = self.page(marked=(NAMES[0],))
        entry = page.entry_for(NAMES[0])
        self.assertTrue(entry, "the fixture frame should carry directions")

        page.show_photo(NAMES[0])
        self.assertEqual(page.stamp.text(), "")

        for status, expected in (("accepted", "VERIFIED"),
                                 ("rejected", "NOT VERIFIED")):
            stamped = dict(entry, kimiya_validation={"status": status})
            with mock.patch.object(page, "entry_for", return_value=stamped):
                page.show_photo(NAMES[0])
            self.assertIn(expected, page.stamp.text())
            self.assertTrue(page.stamp.isVisibleTo(page))
            # The treatments are on screen either way.
            shown = self.text(page)
            self.assertIn("Standard treatment", shown)

    def test_an_unreadable_answer_says_so_rather_than_claiming_refusal(self):
        page = self.page(marked=(NAMES[0],))
        empty = {"photo": NAMES[0],
                 "kimiya_validation": {"status": "rejected"}}
        with mock.patch.object(page, "entry_for", return_value=empty):
            page.show_photo(NAMES[0])
        self.assertIn("NO ANSWER", page.stamp.text())

    def test_the_rail_marks_a_refused_frame_so_it_can_be_found(self):
        page = self.page(marked=(NAMES[0], NAMES[1]))
        entry = page.entry_for(NAMES[0])
        refused = dict(entry, kimiya_validation={"status": "rejected"})

        with mock.patch.object(
                page, "entry_for",
                side_effect=lambda photo: refused if photo == NAMES[0] else {}):
            page.refresh()
        marks = [page.list.item(row).text().strip()[0]
                 for row in range(page.list.count())]
        self.assertIn("!", marks)

    def test_the_scene_grouping_is_shown_before_it_is_agreed_to(self):
        """A saving nobody can inspect is a treatment applied on trust."""
        from opencull_gui import scenes
        from opencull_qt.suggestions import ScenePlanDialog, SceneStrip

        page = self.page(marked=(NAMES[0], NAMES[1]), suggested=False)
        plan = scenes.plan_for(page.shortlist, [NAMES[0], NAMES[1]])
        dialog = ScenePlanDialog(
            page, plan, 2, 0, page.loader,
            scenes.photos_root_of(page.shortlist))
        self.addCleanup(dialog.deleteLater)

        strips = dialog.findChildren(SceneStrip)
        self.assertEqual(len(strips), len(plan))
        self.assertEqual(
            sorted(strips[0].photos), sorted([NAMES[0], NAMES[1]]))
        # Every frame of the scene is on screen, and the one whose answer
        # the others inherit is named.
        self.assertEqual(len(strips[0].frames), 2)
        self.assertEqual(strips[0].representative, plan[0]["representative"])
        shown = "\n".join(
            label.text() for label in dialog.findChildren(QLabel))
        self.assertIn(plan[0]["representative"], shown)
        self.assertIn("2 frames, 1 scene", shown)

    def test_a_personal_treatment_names_the_style_it_spoke_in(self):
        from opencull_qt.suggestions import Treatment

        card = Treatment("personal", {
            "personal_title": "Cyan Alpine Architecture",
            "personal_intent": "adapt the signature to this valley",
            "personal_recipe": "{}",
            "personal_style": {
                "profile_name": "Vivid Heritage Street Architecture",
                "reason": "stone village under hard midday sun",
                "offered": 8},
        })
        self.addCleanup(card.deleteLater)
        shown = "\n".join(label.text() for label in card.findChildren(QLabel))
        self.assertIn("VIVID HERITAGE STREET ARCHITECTURE", shown)
        self.assertIn("Chosen from 8 of your styles", shown)
        self.assertIn("stone village under hard midday sun", shown)

    def test_a_treatment_from_before_the_choice_says_only_personal(self):
        from opencull_qt.suggestions import Treatment

        card = Treatment("personal", {
            "personal_title": "Its own hand", "personal_recipe": "{}"})
        self.addCleanup(card.deleteLater)
        shown = "\n".join(label.text() for label in card.findChildren(QLabel))
        self.assertIn("PERSONAL", shown)
        self.assertNotIn("Chosen from", shown)

    def test_the_ask_says_the_styles_are_chosen_scene_by_scene(self):
        from opencull_gui import scenes
        from opencull_qt.suggestions import ScenePlanDialog

        page = self.page(marked=(NAMES[0], NAMES[1]), suggested=False)
        plan = scenes.plan_for(page.shortlist, [NAMES[0], NAMES[1]])
        dialog = ScenePlanDialog(
            page, plan, 2, 0, page.loader, None,
            ["Coastal twilight", "Heritage street"])
        self.addCleanup(dialog.deleteLater)
        shown = "\n".join(
            label.text() for label in dialog.findChildren(QLabel))
        self.assertIn("all 2 of your personal styles", shown)
        self.assertIn("Coastal twilight", shown)

    def test_the_dialog_reports_which_scope_was_pressed(self):
        from opencull_gui import scenes
        from opencull_qt.suggestions import ScenePlanDialog

        page = self.page(marked=(NAMES[0], NAMES[1]), suggested=False)
        plan = scenes.plan_for(page.shortlist, [NAMES[0], NAMES[1]])
        for label, expected in (("scene", "scene"), ("all", "all")):
            dialog = ScenePlanDialog(page, plan, 2, 0, page.loader, None)
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.choice, "cancel")
            dialog._choose(label)
            self.assertEqual(dialog.choice, expected)

    def test_asking_again_can_still_ask_once_per_scene(self):
        """The redo path withheld the scene plan and charged per frame."""
        from opencull_qt.suggestions import ScenePlanDialog

        # Every marked frame already answered: the only question left is
        # whether to redo, and a redo is still made of scenes.
        page = self.page(marked=(NAMES[0], NAMES[1]))
        shown = {}
        with mock.patch.object(
                page, "_ask_scope",
                side_effect=lambda w, d, plan: shown.update(
                    waiting=w, done=d, plan=plan) or "cancel"):
            page.suggest()
        self.assertEqual((shown["waiting"], shown["done"]), (0, 2))
        self.assertEqual(len(shown["plan"]), 1)

        dialog = ScenePlanDialog(page, shown["plan"], 0, 2, page.loader, None)
        self.addCleanup(dialog.deleteLater)
        labels = [b.text() for b in dialog.findChildren(QPushButton)]
        self.assertIn("One per scene (1 call)", labels)
        self.assertIn("Redo every frame (2 calls)", labels)

    def test_choosing_scenes_asks_only_the_representatives(self):
        from opencull_gui import scenes

        page = self.page(marked=(NAMES[0], NAMES[1]), suggested=False)
        asked = []
        page.suggested.connect(lambda output, photos: asked.append(photos))
        with mock.patch.object(page, "_ask_scope", return_value="scene"):
            page.suggest()
        # One scene, so one call -- for its best-ranked frame.
        self.assertEqual(asked, [[NAMES[0]]])
        # And the sharing the photographer agreed to is on disk, bound to
        # this shortlist by hash.
        plan = scenes.plan_path(
            page.directions.recipes, page.shortlist.path.stem)
        self.assertTrue(plan.is_file())

    def test_a_shoot_with_no_saving_is_not_asked_about_scenes(self):
        # One marked frame is one scene: no choice worth interrupting for.
        page = self.page(marked=(NAMES[0],), suggested=False)
        asked = []
        page.suggested.connect(lambda output, photos: asked.append(photos))
        with mock.patch.object(page, "_ask_scope") as scope:
            page.suggest()
        scope.assert_not_called()
        self.assertEqual(asked, [[NAMES[0]]])

    def test_asking_when_some_are_answered_offers_only_the_rest(self):
        page = self.page(marked=(NAMES[0], NAMES[1]), answered=(NAMES[0],))
        asked = []
        page.suggested.connect(lambda output, photos: asked.append(photos))
        with mock.patch.object(page, "_ask_scope", return_value="missing"):
            page.suggest()
        self.assertEqual(asked, [[NAMES[1]]])

    def test_redoing_all_of_them_is_a_deliberate_choice(self):
        page = self.page(marked=(NAMES[0], NAMES[1]), answered=(NAMES[0],))
        asked = []
        page.suggested.connect(lambda output, photos: asked.append(photos))
        with mock.patch.object(page, "_ask_scope", return_value="all"):
            page.suggest()
        self.assertEqual(asked, [sorted([NAMES[0], NAMES[1]])])

    def test_cancelling_asks_for_nothing(self):
        page = self.page(marked=(NAMES[0], NAMES[1]), answered=(NAMES[0],))
        asked = []
        page.suggested.connect(lambda output, photos: asked.append(photos))
        with mock.patch.object(page, "_ask_scope", return_value="cancel"):
            page.suggest()
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
