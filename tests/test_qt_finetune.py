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
    from PySide6.QtWidgets import QApplication, QLabel
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

    def test_a_treatment_with_no_bounded_operations_says_so(self):
        page = self.page(recipe=json.dumps({"tone": ["make it nicer"]}))
        self.assertEqual(page.controls, [])
        self.assertIn("nothing here to move", self.text(page))
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
