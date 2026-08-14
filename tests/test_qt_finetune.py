"""Fine tuning: moving a treatment's own numbers, and saying so."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel, QWidget
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui import adjustments  # noqa: E402
from opencull_gui.photos import PhotoStore  # noqa: E402
from opencull_gui.report import load_report  # noqa: E402
from tests.test_qt_develop import (  # noqa: E402
    NAMES,
    assess_and_suggest,
    build_shoot,
)

# The compiler's own grammar. Prose it cannot bound produces no operation,
# and an operation it cannot bound is not a control.
RECIPE = json.dumps({
    "global_exposure": ["exposure +0.45", "contrast +4"],
    "hdr_levels_curves": ["shadows +18"],
    "white_balance_and_color": ["temperature 5400 kelvin"],
})


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class FineTunePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.report_path, self.photos_path = build_shoot(self.root)
        self.report = load_report(self.report_path)
        self.photos = PhotoStore(self.photos_path, self.root / "cache")
        self.addCleanup(self._temporary.cleanup)

    def page(self, recipe: str = RECIPE, styles=("standard",)):
        from opencull_qt.develop import workspace_for
        from opencull_qt.finetune import FineTunePage
        from opencull_qt.previews import PreviewLoader

        shortlist = assess_and_suggest(
            self.root, self.report_path, self.photos_path,
            marked=(NAMES[0],), styles=styles)
        self._rewrite_recipe(shortlist, recipe)
        workspace = workspace_for(self.report, self.photos.root, decoders=set())
        loader = PreviewLoader(self.photos, None)
        self.addCleanup(loader.shutdown)
        page = FineTunePage(self.report, workspace, loader)
        self.addCleanup(page.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def _rewrite_recipe(self, shortlist: Path, recipe: str) -> None:
        """Put a recipe the compiler can actually bound into the directions."""
        from opencull_gui.project import ensure_project_layout

        for path in ensure_project_layout(
                self.photos_path)["Recipes"].glob("*edit-directions-r*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            for entry in value.get("entries", []):
                for key in list(entry):
                    if key.endswith("_recipe"):
                        entry[key] = recipe
            path.write_text(json.dumps(value), encoding="utf-8")

    def text(self, page) -> str:
        return "\n".join(label.text() for label in page.findChildren(QLabel))

    # --- what is offered --------------------------------------------------

    def test_only_frames_with_a_treatment_are_offered(self):
        self.assertEqual(self.page().photos, [NAMES[0]])

    def test_the_baseline_is_not_offered_because_it_has_nothing_to_move(self):
        page = self.page()
        self.assertNotIn(
            "calibrated", [item["id"] for item in page.treatments])

    def test_every_bounded_operation_becomes_a_control(self):
        page = self.page()
        self.assertEqual(
            [item.control["op"] for item in page.controls],
            ["tone.exposure", "tone.contrast", "tone.shadow",
             "color.temperature"])

    def test_a_control_shows_the_sentence_that_produced_it(self):
        self.assertIn("exposure +0.45", self.text(self.page()))

    def test_a_guardrail_is_shown_but_is_not_a_control(self):
        page = self.page()
        self.assertIn("keep skin believable", self.text(page))
        self.assertNotIn(
            "guardrail", [item.control["op"] for item in page.controls])

    def test_a_treatment_with_no_bounded_operations_still_offers_the_instrument(self):
        """It used to say "nothing here to move" and stop. Advanced fine
        tuning means the whole surface is always there, folded; only the
        keep button waits for something to actually be in the recipe."""
        from PySide6.QtWidgets import QPushButton

        page = self.page(recipe=json.dumps({"tone": ["make it nicer"]}))
        self.assertEqual(page.controls, [])
        folds = [button.text() for button in page.findChildren(QPushButton)
                 if button.text().startswith(("More ", "Fewer "))]
        self.assertIn("More tone controls · 7", folds)
        self.assertFalse(page.keep_button.isEnabled())

    # --- moving them ------------------------------------------------------

    def control(self, page, operation: str):
        return next(item for item in page.controls
                    if item.control["op"] == operation)

    def test_moving_a_control_records_the_change(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        self.assertAlmostEqual(
            page.changes[exposure.control["id"]]["value"], 0.30, places=2)

    def test_moving_a_control_says_what_was_asked_and_what_was_set(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        self.assertIn("model asked +0.45 EV", exposure.provenance.text())
        self.assertIn("you set", exposure.provenance.text())

    def test_an_untouched_control_says_it_is_as_asked(self):
        self.assertIn(
            "as asked",
            self.control(self.page(), "tone.exposure").provenance.text())

    def test_a_control_can_be_switched_off(self):
        page = self.page()
        shadow = self.control(page, "tone.shadow")
        shadow.enabled.setChecked(False)
        self.assertIs(page.changes[shadow.control["id"]]["enabled"], False)
        self.assertFalse(shadow.slider.isEnabled())
        self.assertIn("switched it off", shadow.provenance.text())

    def test_a_slider_can_never_leave_the_compilers_bounds(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure.slider.maximum())
        self.assertLessEqual(exposure.value(), exposure.control["high"])
        exposure.slider.setValue(exposure.slider.minimum())
        self.assertGreaterEqual(exposure.value(), exposure.control["low"])

    def test_the_caption_says_whether_anything_has_been_moved(self):
        page = self.page()
        self.assertEqual(page.caption.text(), "AS SUGGESTED")
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(-1.0))
        self.assertEqual(page.caption.text(), "AS ADJUSTED")

    def test_going_back_to_the_suggestion_clears_every_change(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(-1.0))
        page.reset()
        self.assertEqual(page.changes, {})
        self.assertIn("as it was suggested", page.status.text())

    def test_changing_the_treatment_starts_from_what_that_one_asked(self):
        page = self.page(styles=("standard", "signature"))
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(-1.0))
        page.show_treatment("signature")
        self.assertEqual(page.changes, {})

    # --- saying it --------------------------------------------------------

    def test_typed_words_move_the_sliders(self):
        page = self.page()
        page.prompt.setText("shadows +7, contrast +2")
        page.speak()
        shadow = self.control(page, "tone.shadow")
        # The recipe asked +18; the words nudge from there.
        self.assertAlmostEqual(shadow.value(), 25.0, delta=0.2)
        self.assertIn("Moved Shadows", page.status.text())
        self.assertEqual(page.prompt.text(), "")

    def test_an_absolute_word_sets_rather_than_nudges(self):
        page = self.page()
        page.prompt.setText("temperature 6000 kelvin")
        page.speak()
        temperature = self.control(page, "color.temperature")
        self.assertAlmostEqual(temperature.value(), 6000.0, delta=30)

    def test_free_words_are_reported_and_kept_in_the_box(self):
        page = self.page()
        page.prompt.setText("shadows +5, make it moody")
        page.speak()
        self.assertIn("Not understood", page.status.text())
        self.assertIn("make it moody", page.status.text())
        self.assertEqual(page.prompt.text(), "shadows +5, make it moody")

    def test_a_control_this_treatment_does_not_have_is_named(self):
        page = self.page()
        page.prompt.setText("vignette -10")
        page.speak()
        self.assertIn("no Vignette to move", page.status.text())

    # --- keeping one ------------------------------------------------------

    def test_keeping_renders_at_full_size_with_the_adjustments(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        with mock.patch.object(
            page.workspace, "render_full",
            return_value={"render": {"variant": "standard-adjusted"}},
        ) as rendered:
            page.keep()
        rendered.assert_called_once()
        self.assertEqual(
            rendered.call_args.kwargs["adjustments"], page.changes)

    def test_keeping_an_unmoved_treatment_says_where_to_do_that(self):
        page = self.page()
        with mock.patch.object(page.workspace, "render_full") as rendered:
            page.keep()
        rendered.assert_not_called()
        self.assertIn("development page", page.status.text())

    def test_a_kept_version_is_named_as_its_own(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        with mock.patch.object(
            page.workspace, "render_full",
            return_value={"render": {"variant": "standard-adjusted"}},
        ):
            page.keep()
        self.assertIn("standard-adjusted", page.status.text())

    def test_a_failed_render_is_reported_rather_than_raised(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        with mock.patch.object(
            page.workspace, "render_full", side_effect=RuntimeError("no decoder"),
        ):
            page.keep()
        self.assertIn("no decoder", page.status.text())
        self.assertTrue(page.keep_button.isEnabled())


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class ZoneSliderTests(unittest.TestCase):
    """The groove that says how far is wise."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def slider(self):
        from opencull_qt.zoneslider import ZoneSlider

        made = ZoneSlider()
        made.setRange(0, 1000)
        made.resize(300, 26)
        return made

    def test_the_bands_cover_the_range_in_order(self):
        slider = self.slider()
        slider.set_zones(-5.0, 5.0, (-1.0, 0.7), (-2.5, 1.5))
        spans = [(round(a, 3), round(b, 3)) for a, b, _c in slider._bands]
        self.assertEqual(spans[0][0], 0.0)
        self.assertEqual(spans[-1][1], 1.0)
        for (before), (after) in zip(spans, spans[1:]):
            self.assertEqual(before[1], after[0])

    def test_the_safe_band_sits_where_the_numbers_say(self):
        slider = self.slider()
        slider.set_zones(-5.0, 5.0, (-1.0, 0.7), (-2.5, 1.5))
        teal = slider._bands[2]
        self.assertAlmostEqual(teal[0], ((-1.0) - (-5.0)) / 10.0, places=3)
        self.assertAlmostEqual(teal[1], (0.7 - (-5.0)) / 10.0, places=3)

    def test_a_control_pinned_at_one_end_has_no_lower_red(self):
        """Levels' white point: everything below 160 is red, above 216
        is safe -- the band list must survive zero-width segments."""
        slider = self.slider()
        slider.set_zones(0.0, 255.0, (216.0, 255.0), (160.0, 255.0))
        red_low, amber_low, teal, amber_high, red_high = slider._bands
        self.assertAlmostEqual(teal[1], 1.0, places=3)
        self.assertAlmostEqual(amber_high[0], amber_high[1], places=3)
        self.assertAlmostEqual(red_high[0], red_high[1], places=3)

    def test_marks_land_as_fractions_and_survive_none(self):
        slider = self.slider()
        slider.set_marks(-5.0, 5.0, asked=-1.5, neutral=0.0)
        self.assertAlmostEqual(slider._asked_fraction, 0.35, places=3)
        self.assertAlmostEqual(slider._neutral_fraction, 0.5, places=3)
        slider.set_marks(-5.0, 5.0, asked=None, neutral=None)
        self.assertIsNone(slider._asked_fraction)
        self.assertIsNone(slider._neutral_fraction)

    def test_it_paints_without_a_backing_window(self):
        """The whole point is custom paint; it must not need a screen."""
        from PySide6.QtGui import QImage

        slider = self.slider()
        slider.set_zones(-5.0, 5.0, (-1.0, 0.7), (-2.5, 1.5))
        slider.set_marks(-5.0, 5.0, asked=-1.5, neutral=0.0)
        canvas = QImage(300, 26, QImage.Format.Format_ARGB32)
        canvas.fill(0)
        slider.render(canvas)
        colours = {canvas.pixelColor(x, 13).name() for x in range(8, 292, 4)}
        self.assertGreater(len(colours), 2)


class AbsentControlTests(unittest.TestCase):
    """The whole instrument on the page, greyed until asked for."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def control(self, absent=True):
        from opencull_qt.finetune import Control

        return Control({
            "id": "tone.shadow", "op": "tone.shadow", "label": "Shadows",
            "section": "Tone", "value": 0.0, "asked": 0.0,
            "unit": "percent", "low": -100.0, "high": 100.0,
            "enabled": True, "source": "", "absent": absent,
        }, {"safe": (-15.0, 40.0), "artistic": (-40.0, 80.0)})

    def test_an_absent_control_arrives_unchecked_and_still(self):
        made = self.control()
        self.assertFalse(made.enabled.isChecked())
        self.assertFalse(made.slider.isEnabled())

    def test_ticking_it_asks_it_into_existence(self):
        made = self.control()
        heard = []
        made.wanted.connect(lambda op, value: heard.append((op, value)))
        made.enabled.setChecked(True)
        self.assertEqual(heard, [("tone.shadow", 0.0)])
        self.assertFalse(made.absent)

    def test_moving_the_slider_is_the_same_ask(self):
        made = self.control()
        made.slider.setEnabled(True)
        heard = []
        made.wanted.connect(lambda op, value: heard.append(op))
        changed = []
        made.changed.connect(lambda key, change: changed.append(change))
        made.slider.setValue(700)
        self.assertEqual(heard, ["tone.shadow"])
        self.assertEqual(changed, [])
        self.assertTrue(made.enabled.isChecked())

    def test_a_present_control_never_emits_wanted(self):
        made = self.control(absent=False)
        heard = []
        made.wanted.connect(lambda op, value: heard.append(op))
        made.slider.setValue(700)
        made.enabled.setChecked(False)
        self.assertEqual(heard, [])


class MaskCardTests(unittest.TestCase):
    """One mask as a card: geometry, effects and the tint request."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def card(self, shape="radial"):
        from opencull_qt.finetune import MaskCard

        mask = {
            "id": "mask:1", "shape": shape, "label": "the surroundings",
            "enabled": True,
            "geometry": {"centre_x": 49.0, "centre_y": 52.0,
                         "radius": 36.0, "feather": 80, "opacity": 100,
                         "inverted": True, "edge": "bottom",
                         "reach": 35.0, "band": "shadows"},
            "effects": [{
                "id": "mask:1/tone.exposure", "op": "tone.exposure",
                "label": "Exposure", "section": "Mask", "value": 0.8,
                "asked": 0.8, "unit": "EV", "low": -5.0, "high": 5.0,
                "enabled": True, "source": ""}],
        }
        return MaskCard(mask)

    def test_moving_the_radius_emits_the_changes_key_apply_reads(self):
        card = self.card()
        heard = []
        card.changed.connect(lambda key, change: heard.append((key, change)))
        slider = next(child for child in card.findChildren(QWidget)
                      if getattr(child, "key", "") == "radius")
        slider.slider.setValue(200)
        self.assertEqual(heard[-1][0], "mask:1")
        self.assertIn("geometry", heard[-1][1])
        self.assertAlmostEqual(
            heard[-1][1]["geometry"]["radius"], 1 + 99 * 0.2, places=1)

    def test_an_effect_rides_the_same_control_as_the_whole_frame(self):
        card = self.card()
        heard = []
        card.effect_changed.connect(
            lambda key, change: heard.append((key, change)))
        control = next(item for item in card.findChildren(QWidget)
                       if getattr(item, "control", {}).get("id")
                       == "mask:1/tone.exposure")
        control.slider.setValue(900)
        self.assertEqual(heard[-1][0], "mask:1/tone.exposure")
        self.assertIn("value", heard[-1][1])

    def test_the_show_button_asks_for_this_masks_tint(self):
        card = self.card()
        heard = []
        card.show_me.connect(lambda key, on: heard.append((key, on)))
        card.show_button.setChecked(True)
        card.show_button.setChecked(False)
        self.assertEqual(heard, [("mask:1", True), ("mask:1", False)])

    def test_a_linear_card_offers_edge_and_reach_not_centres(self):
        card = self.card("linear")
        keys = [getattr(child, "key", "")
                for child in card.findChildren(QWidget)]
        self.assertIn("reach", keys)
        self.assertNotIn("centre_x", keys)
        self.assertEqual(card.edge.currentText(), "bottom")


class InsertRenderTests(unittest.TestCase):
    """What phase one got wrong: an added control must reach the render.

    The page rewrote only its own copy of the recipe; the renderer
    recompiles from the treatment id and folds in the changes dict, so
    every hand-added operation was quietly ignored. The insert rides
    the changes dict now, and this is the seam that proves it.
    """

    def test_apply_carries_the_insert_to_the_renderer(self):
        from opencull_gui import adjustments

        recipe = {"format": "opencull-development-recipe-v1",
                  "operations": [{"op": "tone.exposure", "value": -1.0,
                                  "unit": "EV", "mode": "delta"}]}
        changes = {"+insert": [{"op": "tone.shadow", "value": 25.0}]}
        rendered = adjustments.apply(recipe, changes)
        self.assertIn("tone.shadow",
                      [item["op"] for item in rendered["operations"]])

    def test_the_insert_is_idempotent_across_rerenders(self):
        """The same changes dict is applied on every render; the second
        application must not double the operation."""
        from opencull_gui import adjustments

        recipe = {"format": "opencull-development-recipe-v1",
                  "operations": []}
        changes = {"+insert": [{"op": "tone.shadow", "value": 25.0}]}
        once = adjustments.apply(recipe, changes)
        twice = adjustments.apply(once, changes)
        self.assertEqual(
            [item["op"] for item in twice["operations"]].count("tone.shadow"),
            1)


class AdjustedRenderTests(FineTunePageTests):
    """An adjusted rendering must never be mistaken for the treatment."""

    def test_an_adjusted_full_render_is_its_own_variant(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        with mock.patch.object(
            page.workspace, "recipe_preview",
            return_value=self.photos_path / NAMES[0],
        ):
            record = page.workspace.render_full(
                NAMES[0], "standard", "default", "markesteijn-3-pass",
                adjustments=page.changes)
        self.assertTrue(
            str(record["render"]["variant"]).endswith("-adjusted"))
        self.assertTrue(record["render"]["adjustments"])

    def test_an_unadjusted_full_render_keeps_the_treatments_own_name(self):
        page = self.page()
        with mock.patch.object(
            page.workspace, "recipe_preview",
            return_value=self.photos_path / NAMES[0],
        ):
            record = page.workspace.render_full(
                NAMES[0], "standard", "default", "markesteijn-3-pass")
        self.assertEqual(record["render"]["variant"], "standard")
        self.assertEqual(record["render"]["adjustments"], [])

    # --- keeping a look ---------------------------------------------------

    def own_presets(self) -> Path:
        """A presets folder belonging to this test and nobody else."""
        from opencull_gui import presets

        mine = self.root / "presets"
        self.enterContext(mock.patch.dict(
            "os.environ", {presets.PRESETS_ENVIRONMENT: str(mine)}))
        return mine

    def test_a_tuned_version_can_be_kept_as_a_preset(self):
        from opencull_gui import presets

        self.own_presets()
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        with mock.patch.object(
            page, "ask_preset_name", return_value=("Evening walk", True)
        ):
            page.save_preset()
        kept = presets.saved()
        self.assertEqual([item["name"] for item in kept], ["Evening walk"])
        moved = next(item for item in kept[0]["operations"]
                     if item["op"] == "tone.exposure")
        self.assertAlmostEqual(moved["value"], 0.30, places=2)
        self.assertIn("Evening walk", page.status.text())

    def test_what_is_kept_is_what_was_on_screen_not_what_was_suggested(self):
        self.own_presets()
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        self.assertAlmostEqual(exposure.control["asked"], 0.45, places=2)
        exposure.slider.setValue(exposure._tick(0.10))
        kept = {item["op"]: item["value"]
                for item in page.preset_operations()}
        self.assertAlmostEqual(kept["tone.exposure"], 0.10, places=2)

    def test_an_operation_switched_off_is_not_kept(self):
        self.own_presets()
        page = self.page()
        shadows = self.control(page, "tone.shadow")
        shadows.enabled.setChecked(False)
        self.assertNotIn(
            "tone.shadow", [item["op"] for item in page.preset_operations()])

    def test_saying_no_to_the_name_saves_nothing(self):
        from opencull_gui import presets

        self.own_presets()
        page = self.page()
        with mock.patch.object(
            page, "ask_preset_name", return_value=("", False)
        ):
            page.save_preset()
        self.assertEqual(presets.saved(), [])

    def test_a_refused_save_says_why_and_keeps_the_page_usable(self):
        from opencull_gui import presets

        self.own_presets()
        page = self.page()
        with mock.patch.object(page, "preset_operations", return_value=[]), \
             mock.patch.object(
                 page, "ask_preset_name", return_value=("Empty", True)):
            page.save_preset()
        self.assertEqual(presets.saved(), [])
        self.assertTrue(page.status.text())
        self.assertEqual(page.status.property("tone"), "alarm")

    def test_an_adjusted_proof_is_cached_apart_from_the_suggested_one(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        suggested = page.workspace.compiled_recipe(
            NAMES[0], "standard", "default")
        adjusted = adjustments.apply(
            suggested, {exposure.control["id"]: {"value": 0.3}})
        self.assertNotEqual(
            json.dumps(suggested, sort_keys=True),
            json.dumps(adjusted, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
