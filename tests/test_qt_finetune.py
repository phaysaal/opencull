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
    from PySide6.QtCore import Qt
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

    def page(self, recipe: str = RECIPE, styles=("standard",),
             marked=(NAMES[0],)):
        from opencull_qt.develop import workspace_for
        from opencull_qt.finetune import FineTunePage
        from opencull_qt.previews import PreviewLoader

        shortlist = assess_and_suggest(
            self.root, self.report_path, self.photos_path,
            marked=marked, styles=styles)
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

    def test_a_drawn_curve_lands_in_the_changes_and_identity_clears_it(self):
        page = self.page()
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        self.assertEqual(page.changes["curve"]["points"][1],
                         [128.0, 190.0])
        applied = adjustments.apply(page.recipe, page.changes)
        self.assertIn("tone.curve",
                      [item["op"] for item in applied["operations"]])
        page._curve_changed([[0.0, 0.0], [255.0, 255.0]])
        self.assertNotIn("curve", page.changes)

    def test_the_colour_hold_rides_the_drawn_curve(self):
        page = self.page()
        page._show_controls()
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        page._curve_panel.points = [[0.0, 0.0], [128.0, 190.0],
                                    [255.0, 255.0]]
        page._curve_preserved("preserve", 70.0)
        self.assertEqual(page.changes["curve"]["preserve"], 70.0)
        applied = adjustments.apply(page.recipe, page.changes)
        op = next(item for item in applied["operations"]
                  if item.get("op") == "tone.curve")
        self.assertEqual(op["value"]["preserve"], 70.0)
        # Redrawing keeps the dial where the hand left it.
        page._curve_changed([[0.0, 0.0], [128.0, 170.0], [255.0, 255.0]])
        self.assertEqual(page.changes["curve"]["preserve"], 70.0)

    def test_the_hold_alone_asks_nothing_until_a_curve_is_drawn(self):
        page = self.page()
        page._show_controls()
        page._curve_preserved("preserve", 80.0)
        self.assertNotIn("curve", page.changes)
        # But the wish is remembered for the first drawing.
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        self.assertEqual(page.changes["curve"]["preserve"], 80.0)

    def test_the_wedge_walls_and_eveners_sit_on_the_colour_layer(self):
        page = self.page()
        page._create_mask("color")
        told = self.text(page)
        for label in ("Sat ceiling", "Lit floor", "Lit ceiling",
                      "Even hue", "Even colour", "Even light"):
            self.assertIn(label, told)

    def test_an_evener_slider_lands_in_recipe_and_survives_a_refold(self):
        page = self.page()
        page._create_mask("color")
        ordinal = adjustments.masks(page.recipe)[-1]["ordinal"]
        page._mask_changed(f"mask:{ordinal}", {"add_effects": [
            {"op": "uniformity.hue", "value": 45.0}]})
        held = [op for op in page.recipe["operations"]
                if op.get("op") == "mask.color"][-1]
        placed = [e for e in held["value"]["effects"]
                  if e.get("op") == "uniformity.hue"]
        self.assertEqual(placed[0]["value"], 45.0)
        # Moving it again moves the same effect, live and refolded.
        page._mask_changed(f"mask:{ordinal}", {"add_effects": [
            {"op": "uniformity.hue", "value": 80.0}]})
        page._rebuild_mirror()
        held = [op for op in page.recipe["operations"]
                if op.get("op") == "mask.color"][-1]
        placed = [e for e in held["value"]["effects"]
                  if e.get("op") == "uniformity.hue"]
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]["value"], 80.0)

    def test_painted_strokes_survive_a_refold(self):
        page = self.page()
        page._create_mask("brush")
        ordinal = adjustments.masks(page.recipe)[-1]["ordinal"]
        page._mask_changed(f"mask:{ordinal}", {"map": "QUJD"})
        page._rebuild_mirror()
        held = [op for op in page.recipe["operations"]
                if op.get("op") == "mask.brush"][-1]
        self.assertEqual(held["value"].get("map"), "QUJD")

    def test_picking_a_colour_also_sets_the_eveners_aim(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page._create_mask("color")
        ordinal = adjustments.masks(page.recipe)[-1]["ordinal"]
        image = QImage(320, 240, QImage.Format.Format_RGB888)
        image.fill(QColor(204, 128, 102))    # warm skin-ish
        page.frame.resize(320, 240)
        page.frame.set_source(QPixmap.fromImage(image))
        page._start_pick(f"mask:{ordinal}")
        centre_x = (page.frame.width()) / 2.0
        centre_y = (page.frame.height()) / 2.0
        page._pick_at(QPointF(centre_x, centre_y))
        held = [op for op in page.recipe["operations"]
                if op.get("op") == "mask.color"][-1]
        anchor = held["value"]["anchor"]
        self.assertIn("target saturation", anchor)
        self.assertIn("target light", anchor)

    def test_each_frame_keeps_its_own_settings_across_switches(self):
        page = self.page(marked=(NAMES[0], NAMES[1]))
        page.show_photo(NAMES[0])
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        page.show_photo(NAMES[1])
        self.assertEqual(page.changes, {})
        page._curve_changed([[0.0, 0.0], [128.0, 100.0], [255.0, 255.0]])
        page.show_photo(NAMES[0])
        self.assertEqual(page.changes["curve"]["points"][1],
                         [128.0, 190.0])
        applied = [op["op"] for op in page.recipe.get("operations", [])]
        self.assertIn("tone.curve", applied)   # folded back in, not lost
        page.show_photo(NAMES[1])
        self.assertEqual(page.changes["curve"]["points"][1],
                         [128.0, 100.0])

    def test_the_profile_survives_a_new_page(self):
        page = self.page()
        page.show_photo(NAMES[0])
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        page.shutdown()
        reopened = self.page()
        reopened.show_photo(NAMES[0])
        self.assertEqual(reopened.changes["curve"]["points"][1],
                         [128.0, 190.0])

    def test_unexported_edits_wear_a_dot_and_export_takes_it_off(self):
        page = self.page()
        page.show_photo(NAMES[0])
        row = page.photos.index(NAMES[0])
        self.assertEqual(page.list.item(row).text(), NAMES[0])
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        self.assertIn("●", page.list.item(row).text())
        page.workspace.render_full = (
            lambda *args, **kw: {"render": {"variant": "v1"}})
        page.keep()
        # The export runs on a worker now; wait for it to announce.
        import time

        from PySide6.QtWidgets import QApplication

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline \
                and page.keep_button.text() != "Export":
            QApplication.instance().processEvents()
            time.sleep(0.01)
        self.assertEqual(page.list.item(row).text(), NAMES[0])

    def test_undoing_everything_settles_the_dot(self):
        page = self.page()
        page.show_photo(NAMES[0])
        row = page.photos.index(NAMES[0])
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        self.assertIn("●", page.list.item(row).text())
        page._curve_changed([[0.0, 0.0], [255.0, 255.0]])   # identity
        self.assertEqual(page.list.item(row).text(), NAMES[0])

    def test_the_export_button_says_export(self):
        page = self.page()
        self.assertEqual(page.keep_button.text(), "Export")

    def test_looking_closer_asks_for_a_bigger_proof(self):
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page.frame.resize(400, 300)
        image = QImage(400, 300, QImage.Format.Format_RGB888)
        image.fill(QColor(90, 80, 70))
        page.frame.set_source(QPixmap.fromImage(image))
        at_fit = page.proof_edge()
        page.frame._zoom = page.frame._fit_scale() * 3.0
        self.assertAlmostEqual(page.frame.magnification(), 3.0, places=3)
        self.assertGreater(page.proof_edge(), at_fit * 2)
        page.frame._zoom = page.frame._fit_scale() * 40.0
        self.assertLessEqual(page.proof_edge(), 1600 * 4)

    def test_a_sharper_proof_keeps_the_same_view(self):
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page.frame.resize(400, 300)
        def coloured(w, h):
            image = QImage(w, h, QImage.Format.Format_RGB888)
            image.fill(QColor(90, 80, 70))
            return QPixmap.fromImage(image)
        page.frame.set_source(coloured(800, 600))
        page.frame._zoom = page.frame._fit_scale() * 4.0
        before = page.frame.magnification()
        page.frame.set_source(coloured(1600, 1200))   # same photo, sharper
        self.assertAlmostEqual(
            page.frame.magnification(), before, places=2)

    def test_copied_settings_paste_onto_another_frame(self):
        page = self.page(marked=(NAMES[0], NAMES[1]))
        page.show_photo(NAMES[0])
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        told = page._settings_of(NAMES[0])
        self.assertIsNotNone(told)
        self.assertTrue(page._paste_onto(NAMES[1], told))
        page.show_photo(NAMES[1])
        self.assertEqual(page.changes["curve"]["points"][1],
                         [128.0, 190.0])
        applied = [op["op"] for op in page.recipe.get("operations", [])]
        self.assertIn("tone.curve", applied)

    def test_scene_mates_receive_the_settings_together(self):
        from unittest import mock

        page = self.page(marked=(NAMES[0], NAMES[1], NAMES[2]))
        page.show_photo(NAMES[0])
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        one_scene = [{"id": "scene-0001", "photos": list(NAMES)}]
        with mock.patch("opencull_gui.scenes.scene_groups",
                        return_value=one_scene):
            mates = [m for m in page._scene_mates(NAMES[0])
                     if m != NAMES[0]]
            told = page._settings_of(NAMES[0])
            landed = [m for m in mates if page._paste_onto(m, told)]
        self.assertEqual(sorted(landed), [NAMES[1], NAMES[2]])
        self.assertTrue(page._ledger.unexported(NAMES[1]))
        self.assertTrue(page._ledger.unexported(NAMES[2]))

    def test_presets_wait_behind_the_more_styles_door(self):
        page = self.page()
        page.show_photo(NAMES[0])
        shown = [str(item.get("id")) for item in page.treatments]
        self.assertFalse(any(str(item.get("kind")) == "preset"
                             for item in page.treatments))
        self.assertTrue(page._more)
        door = page.treatment_list.item(len(page.treatments))
        self.assertIn("More styles", door.text())
        # And no thumbnail was asked for a style behind the door.
        self.assertNotIn(
            str(page._more[0]["id"]), shown)

    def test_an_invited_style_joins_the_strip(self):
        page = self.page()
        page.show_photo(NAMES[0])
        style = str(page._more[0]["id"])
        page._invited.setdefault(NAMES[0], set()).add(style)
        page.show_photo(NAMES[0])
        self.assertIn(style,
                      [str(item.get("id")) for item in page.treatments])

    def test_a_scene_mates_customised_preset_is_shown_here_too(self):
        from unittest import mock

        page = self.page(marked=(NAMES[0], NAMES[1]))
        page.show_photo(NAMES[0])
        style = str(page._more[0]["id"])
        page._ledger.save(NAMES[1], style, {"curve": {
            "points": [[0.0, 0.0], [90.0, 120.0], [255.0, 255.0]]}}, 0)
        one_scene = [{"id": "scene-0001",
                      "photos": [NAMES[0], NAMES[1]]}]
        with mock.patch("opencull_gui.scenes.scene_groups",
                        return_value=one_scene):
            page.show_photo(NAMES[0])
        self.assertIn(style,
                      [str(item.get("id")) for item in page.treatments])

    def test_the_visible_window_follows_the_zoomed_eye(self):
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page.frame.resize(400, 300)
        image = QImage(800, 600, QImage.Format.Format_RGB888)
        image.fill(QColor(90, 80, 70))
        page.frame.set_source(QPixmap.fromImage(image))
        self.assertIsNone(page.frame.visible_window())   # fit: no window
        page.frame._zoom = page.frame._fit_scale() * 4.0
        page.frame._centre = [0.5, 0.5]
        window = page.frame.visible_window()
        self.assertLess(window["w"], 0.5)                # a real crop
        self.assertGreater(window["w"], 0.1)
        self.assertLessEqual(window["x"] + window["w"], 1.0)
        self.assertLessEqual(window["y"] + window["h"], 1.0)

    def test_the_fast_patch_rides_the_proof_and_a_new_source_clears_it(self):
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page.frame.resize(400, 300)
        def coloured(w, h, c):
            image = QImage(w, h, QImage.Format.Format_RGB888)
            image.fill(QColor(*c))
            return QPixmap.fromImage(image)
        page.frame.set_source(coloured(800, 600, (40, 40, 40)))
        page.frame._zoom = page.frame._fit_scale() * 4.0
        window = page.frame.visible_window()
        page.frame.set_patch(coloured(200, 150, (250, 60, 60)), window)
        shown = page.frame.pixmap().toImage()
        centre = shown.pixelColor(shown.width() // 2, shown.height() // 2)
        self.assertGreater(centre.red(), 200)      # the patch is on screen
        page.frame.set_source(coloured(800, 600, (40, 40, 40)))
        self.assertIsNone(page.frame._patch)       # a new proof clears it

    def test_zoomed_edits_ask_the_fast_channel_first(self):
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page.frame.resize(400, 300)
        image = QImage(800, 600, QImage.Format.Format_RGB888)
        image.fill(QColor(90, 80, 70))
        page.frame.set_source(QPixmap.fromImage(image))
        asked = []
        page.fast.render = lambda *args, **kw: asked.append(kw)
        page.render()
        self.assertEqual(asked, [])                # at fit: one pass only
        page.frame._zoom = page.frame._fit_scale() * 4.0
        page.render()
        self.assertEqual(len(asked), 1)
        self.assertIsNotNone(asked[0].get("window"))

    def test_a_cropped_recipe_takes_the_one_pass_road(self):
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page.frame.resize(400, 300)
        image = QImage(800, 600, QImage.Format.Format_RGB888)
        image.fill(QColor(90, 80, 70))
        page.frame.set_source(QPixmap.fromImage(image))
        page.frame._zoom = page.frame._fit_scale() * 4.0
        page.changes["crop"] = {"rect": [0.1, 0.1, 0.8, 0.8]}
        asked = []
        page.fast.render = lambda *args, **kw: asked.append(kw)
        page.render()
        self.assertEqual(asked, [])

    def test_a_late_fast_patch_never_covers_a_newer_full_render(self):
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page.frame.resize(400, 300)
        image = QImage(800, 600, QImage.Format.Format_RGB888)
        image.fill(QColor(90, 80, 70))
        page.frame.set_source(QPixmap.fromImage(image))
        page.frame._zoom = page.frame._fit_scale() * 4.0
        page._fast_window = page.frame.visible_window()
        page._render_pending = False               # the full one landed
        page._fast_rendered(page.current, page.treatment,
                            QPixmap.fromImage(image))
        self.assertIsNone(page.frame._patch)

    def test_a_windowed_preview_is_the_windows_own_size(self):
        page = self.page()
        path = page.workspace.recipe_preview(
            NAMES[0], "standard", "default", "markesteijn-3-pass",
            640, window={"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5})
        from PIL import Image as PILImage

        with PILImage.open(path) as out:
            width, height = out.size
        full = page.workspace.recipe_preview(
            NAMES[0], "standard", "default", "markesteijn-3-pass", 640)
        with PILImage.open(full) as out:
            full_w, full_h = out.size
        self.assertAlmostEqual(width / full_w, 0.5, delta=0.02)
        self.assertAlmostEqual(height / full_h, 0.5, delta=0.02)
        self.assertNotEqual(str(path), str(full))

    def test_a_mask_added_after_an_erasure_gets_its_own_controls(self):
        from PySide6.QtWidgets import QLabel, QPushButton

        page = self.page()
        page._create_mask("color")
        page._mask_structure(1, {"deleted": True})
        if page.layer == 1:
            page.layer = 0
        page._rebuild_mirror()
        page._create_mask("color")
        # The erased mask keeps ordinal 1 forever; the new one is 2,
        # and the page must be standing on 2 -- the count said 1, and
        # the colour controls vanished into the corpse.
        self.assertEqual(page.layer, 2)
        texts = [label.text() for label in page.findChildren(QLabel)]
        self.assertIn("Hue", texts)
        self.assertIn("EVEN OUT", texts)
        self.assertTrue([b for b in page.findChildren(QPushButton)
                         if "Pick" in b.text()])
        # And the dance holds for a second round with another shape.
        page._mask_structure(2, {"deleted": True})
        if page.layer == 2:
            page.layer = 0
        page._rebuild_mirror()
        page._create_mask("brush")
        self.assertEqual(page.layer, 3)
        texts = [label.text() for label in page.findChildren(QLabel)]
        self.assertIn("Brush", texts)

    def test_the_layer_survives_rebuilds_with_erased_ordinals_around(self):
        page = self.page()
        page._create_mask("color")
        page._mask_structure(1, {"deleted": True})
        page.layer = 0
        page._rebuild_mirror()
        page._create_mask("radial")          # ordinal 2, selected
        page._show_controls()                # a rebuild must not evict it
        self.assertEqual(page.layer, 2)

    def test_the_ab_button_holds_the_frame_as_shot(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        page.show_photo(NAMES[0])
        def coloured(c):
            image = QImage(80, 60, QImage.Format.Format_RGB888)
            image.fill(QColor(*c))
            return QPixmap.fromImage(image)
        page._as_shot_pixmap = coloured((10, 10, 10))
        page._plain_pixmap = coloured((200, 200, 200))
        page.frame.set_source(page._plain_pixmap)
        self.assertEqual(page.ab_button.focusPolicy(),
                         Qt.FocusPolicy.NoFocus)   # Space cannot reach it
        page.ab_button.setChecked(True)
        self.assertEqual(page.caption.text(), "AS SHOT")
        page.ab_button.setChecked(False)
        self.assertIn(page.caption.text(),
                      ("AS ADJUSTED", "AS SUGGESTED"))

    def test_the_key_and_the_button_agree(self):
        page = self.page()
        page.show_photo(NAMES[0])
        from PySide6.QtGui import QColor, QImage, QPixmap

        image = QImage(80, 60, QImage.Format.Format_RGB888)
        image.fill(QColor(10, 10, 10))
        page._as_shot_pixmap = QPixmap.fromImage(image)
        page._plain_pixmap = QPixmap.fromImage(image)
        page.hold(True)                       # the key's road
        self.assertTrue(page.ab_button.isChecked())
        page.hold(False)
        self.assertFalse(page.ab_button.isChecked())

    def test_switching_frames_releases_a_pinned_comparison(self):
        page = self.page(marked=(NAMES[0], NAMES[1]))
        page.show_photo(NAMES[0])
        from PySide6.QtGui import QColor, QImage, QPixmap

        image = QImage(80, 60, QImage.Format.Format_RGB888)
        image.fill(QColor(10, 10, 10))
        page._as_shot_pixmap = QPixmap.fromImage(image)
        page._plain_pixmap = QPixmap.fromImage(image)
        page.ab_button.setChecked(True)
        page.show_photo(NAMES[1])
        self.assertFalse(page.ab_button.isChecked())
        self.assertFalse(page._holding)

    def test_guardrails_wait_behind_their_door(self):
        from PySide6.QtWidgets import QLabel, QPushButton

        page = self.page()
        page.recipe.setdefault("guardrails", []).extend([
            "Keep skin believable.", "No crushed blacks."])
        page._show_controls()
        rails = [label for label in page.findChildren(QLabel)
                 if label.text().startswith("◆")]
        self.assertTrue(rails)
        self.assertTrue(all(label.isHidden() for label in rails))
        door = next(button for button in page.findChildren(QPushButton)
                    if button.text().startswith("Guardrails"))
        self.assertIn("· 3", door.text().replace("  ", " "))  # 2 + the fixture own
        door.setChecked(True)
        self.assertTrue(all(not label.isHidden() for label in rails))
        door.setChecked(False)
        self.assertTrue(all(label.isHidden() for label in rails))

    def test_as_shot_is_a_tunable_starting_point(self):
        page = self.page()
        page.show_photo(NAMES[0])
        page.show_treatment("as-shot")
        self.assertEqual(page.recipe.get("title"), "As shot")
        self.assertEqual(page.recipe.get("operations"), [])
        self.assertNotIn("could not be read", page.status.text())
        # Touch a control and the whole thing exports like any other.
        page.changes["+insert"] = [{"op": "tone.exposure", "value": 0.5}]
        page._show_controls()
        self.assertTrue(page.keep_button.isEnabled())

    def test_adjusted_as_shot_renders_from_the_cameras_picture(self):
        import numpy as np
        from PIL import Image as PILImage

        page = self.page()
        adjusted = page.workspace.recipe_preview(
            NAMES[0], "as-shot", "default", "markesteijn-3-pass", 320,
            adjustments={"+insert": [{"op": "tone.exposure",
                                      "value": 0.5}]})
        plain = page.workspace.recipe_preview(
            NAMES[0], "as-shot", "default", "markesteijn-3-pass", 320)
        bright = np.asarray(
            PILImage.open(adjusted).convert("RGB"), float).mean()
        camera = np.asarray(
            PILImage.open(plain).convert("RGB"), float).mean()
        self.assertGreater(bright, camera + 5)   # the EV landed
        # And untouched as-shot is still the fast path: the JPEG itself.
        self.assertNotEqual(str(adjusted), str(plain))

    def test_an_export_never_freezes_the_page(self):
        import threading
        import time

        from PySide6.QtWidgets import QApplication

        page = self.page()
        page.show_photo(NAMES[0])
        page._curve_changed([[0.0, 0.0], [128.0, 190.0], [255.0, 255.0]])
        gui_thread = threading.get_ident()
        seen = {}

        def slow_render(*args, **kw):
            seen["thread"] = threading.get_ident()
            time.sleep(0.2)
            return {"render": {"variant": "v1"}}
        page.workspace.render_full = slow_render
        page.keep()
        self.assertEqual(page.keep_button.text(), "Exporting…")
        self.assertFalse(page.keep_button.isEnabled())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline \
                and page.keep_button.text() != "Export":
            QApplication.instance().processEvents()
            time.sleep(0.01)
        self.assertNotEqual(seen["thread"], gui_thread)
        self.assertTrue(page.keep_button.isEnabled())
        self.assertIn("Exported", page.status.text())

    def test_an_adjusted_as_shot_export_is_its_own_variant(self):
        import time

        from PySide6.QtWidgets import QApplication

        page = self.page()
        page.show_photo(NAMES[0])
        page.show_treatment("as-shot")
        page.changes["+insert"] = [{"op": "tone.exposure", "value": 0.5}]
        page._rebuild_mirror()
        page.keep()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline \
                and page.keep_button.text() != "Export":
            QApplication.instance().processEvents()
            time.sleep(0.02)
        # Registered as its own variant, never as the untouched JPEG --
        # the untouched path once swallowed the adjustments silently.
        self.assertIn("as-shot-adjusted", page.status.text())

    def twice_asked(self):
        page = self.page()
        page.recipe["operations"].append({
            "id": "op-090", "op": "tone.shadow", "unit": "percent",
            "mode": "delta", "value": -6.0, "enabled": True,
            "source_instruction": "shadows -6 for the second thought"})
        page._pristine = json.loads(json.dumps(page.recipe))
        page._show_controls()
        return page

    def test_a_move_asked_for_twice_is_one_controller(self):
        page = self.twice_asked()
        rows = [w.control for w in page.controls
                if w.control["op"] == "tone.shadow"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "Shadows")
        self.assertEqual(rows[0]["value"], 12.0)   # 18 + (-6), the net
        # Both sentences ride the one (!) dot.
        self.assertIn("second thought", rows[0]["source"])

    def test_moving_the_net_steers_the_second_thought(self):
        page = self.twice_asked()
        combined = next(w.control["id"] for w in page.controls
                        if w.control["op"] == "tone.shadow")
        page._control_changed(combined, {"value": 20.0})
        folded = adjustments.apply(page._pristine, page.changes)
        values = {item["id"]: item["value"]
                  for item in adjustments.controls(folded)
                  if item["op"] == "tone.shadow"}
        self.assertEqual(values["op-090"], 2.0)    # 18 stands; net is 20
        self.assertEqual(sum(values.values()), 20.0)
        page._control_changed(combined, {"value": 5.0})
        folded = adjustments.apply(page._pristine, page.changes)
        net = sum(item["value"]
                  for item in adjustments.controls(folded)
                  if item["op"] == "tone.shadow")
        self.assertEqual(net, 5.0)                 # a second move counts

    def test_switching_the_net_off_switches_every_asking_off(self):
        page = self.twice_asked()
        combined = next(w.control["id"] for w in page.controls
                        if w.control["op"] == "tone.shadow")
        page._control_changed(combined, {"enabled": False})
        folded = adjustments.apply(page._pristine, page.changes)
        self.assertEqual(
            [item["enabled"] for item in adjustments.controls(folded)
             if item["op"] == "tone.shadow"], [False, False])

    def test_an_unfoldable_duplicate_keeps_numbered_rows(self):
        page = self.page()
        page.recipe["operations"].append({
            "id": "op-091", "op": "color.temperature", "unit": "kelvin",
            "mode": "absolute", "value": 6200.0, "enabled": True,
            "source_instruction": "temperature 6200 kelvin again"})
        page._pristine = json.loads(json.dumps(page.recipe))
        page._show_controls()
        labels = [w.control["label"] for w in page.controls
                  if w.control["op"] == "color.temperature"]
        self.assertEqual(len(labels), 2)           # absolutes do not sum
        self.assertTrue(any("· 2" in label for label in labels))

    def test_a_colour_mask_layer_offers_its_own_geometry(self):
        page = self.page()
        made = {"shape": "color", "geometry": {
            "hue": 25.0, "range": 30.0, "softness": 20.0,
            "sat_floor": 10.0, "feather": 100, "opacity": 100},
            "effects": [{"op": "tone.exposure", "value": 0.0}]}
        page.changes.setdefault("+mask", []).append(made)
        page.recipe = adjustments.apply(page.recipe, {"+mask": [made]})
        placed = adjustments.masks(page.recipe)
        self.assertEqual(placed[-1]["shape"], "color")
        page.layer = len(placed)
        page._show_controls()          # must build the colour geometry rows
        told = self.text(page)
        self.assertIn("Hue", told)
        self.assertIn("Softness", told)

    def test_picking_a_colour_sets_the_layers_hue_from_the_picture(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        made = {"shape": "color", "geometry": {
            "hue": 0.0, "range": 30.0, "softness": 20.0,
            "sat_floor": 10.0, "feather": 100, "opacity": 100},
            "effects": [{"op": "tone.exposure", "value": 0.0}]}
        page.changes.setdefault("+mask", []).append(made)
        page.recipe = adjustments.apply(page.recipe, {"+mask": [made]})
        ordinal = len(adjustments.masks(page.recipe))
        # A green proof on the pane; the click lands mid-picture.
        image = QImage(64, 48, QImage.Format.Format_RGB888)
        image.fill(QColor(30, 200, 40))
        page.frame.resize(200, 150)
        page.frame.setPixmap(QPixmap.fromImage(image))
        page._start_pick(f"mask:{ordinal}")
        page._pick_at(QPointF(page.frame.width() / 2,
                              page.frame.height() / 2))
        told = adjustments.masks(page.recipe)[ordinal - 1]["geometry"]
        self.assertAlmostEqual(told["hue"], 124, delta=6)   # green
        self.assertEqual(page._picking_for, "")             # mode ended

    def test_picking_grey_refuses_and_keeps_the_crosshair(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        image = QImage(64, 48, QImage.Format.Format_RGB888)
        image.fill(QColor(128, 128, 128))
        page.frame.resize(200, 150)
        page.frame.setPixmap(QPixmap.fromImage(image))
        page._start_pick("mask:1")
        page._pick_at(QPointF(page.frame.width() / 2,
                              page.frame.height() / 2))
        self.assertEqual(page._picking_for, "mask:1")   # still waiting
        self.assertIn("grey", page.status.text())

    def test_undo_walks_back_and_redo_walks_forward(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        page._remember_key = ""              # end the coalescing beat
        shadow = self.control(page, "tone.shadow")
        shadow.enabled.setChecked(False)
        self.assertIn(shadow.control["id"], page.changes)
        page.undo()
        self.assertNotIn(shadow.control["id"], page.changes)
        self.assertIn(exposure.control["id"], page.changes)
        page.undo()
        self.assertEqual(page.changes, {})
        page.redo()
        self.assertIn(exposure.control["id"], page.changes)

    def test_a_slider_drag_is_one_undo_step_not_forty(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        for tick in range(5):
            exposure.slider.setValue(exposure._tick(0.10 + tick * 0.05))
        page.undo()
        self.assertEqual(page.changes, {})

    def test_erasing_a_layer_hides_it_and_keeps_the_others_addressed(self):
        page = self.page()
        page._create_mask("radial")
        page._create_mask("linear")
        placed = adjustments.masks(page.recipe)
        first = placed[0]["ordinal"]
        page._mask_structure(first, {"deleted": True})
        remaining = adjustments.masks(page.recipe)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["shape"], "linear")
        # And its ordinal survives for future edits.
        self.assertGreater(remaining[0]["ordinal"], first)

    def test_renaming_a_layer_says_the_new_name(self):
        page = self.page()
        page._create_mask("radial")
        ordinal = adjustments.masks(page.recipe)[0]["ordinal"]
        page._mask_structure(ordinal, {"label": "the crescent"})
        self.assertEqual(
            adjustments.masks(page.recipe)[0]["label"], "the crescent")

    def test_controls_stay_in_place_when_ticked_in(self):
        page = self.page()
        before = [item.control["op"] for item in page.controls]
        target = self.control(page, "detail.clarity")
        self.assertTrue(target.absent)
        target.slider.setValue(target._tick(10.0))
        page._show_controls()
        after = [item.control["op"] for item in page.controls]
        self.assertEqual(before, after)

    def test_the_colour_bands_answer_to_the_hand(self):
        from opencull_qt.finetune import HslPanel

        page = self.page()
        panel = next(w for w in page.findChildren(HslPanel))
        panel._switch("saturation")
        row = panel._rows["blue"]
        row.slider.setValue(row.slider.maximum())   # +100 saturation
        state = adjustments.hsl_state(page.recipe)
        self.assertEqual(state[("blue", "saturation")]["value"], 100.0)
        # And undo takes the whole move back.
        page._remember_key = ""
        page.undo()
        self.assertNotIn("+hsl", page.changes)

    def test_the_wb_dropper_neutralises_the_clicked_colour(self):
        page = self.page()
        # A warm cast: red high, blue low -- the dropper should cool it.
        page._wb_from(0.6, 0.5, 0.4)
        applied = adjustments.apply(page._pristine, page.changes)
        held = {op["op"]: op for op in applied["operations"]
                if isinstance(op, dict)}
        # The pristine treatment holds an absolute 5400 K; a warm click
        # nudges it cooler in its own terms rather than replacing it.
        import math
        factor = math.sqrt(0.4 / 0.6)
        self.assertAlmostEqual(
            held["color.temperature"]["value"],
            5400 + round((factor - 1) * 5000), delta=1)
        self.assertLess(held["color.temperature"]["value"], 5400)

    def test_a_clipped_wb_click_refuses(self):
        page = self.page()
        page._picking_for = "@wb"
        from PySide6.QtGui import QColor, QImage, QPixmap
        from PySide6.QtCore import QPointF

        image = QImage(64, 48, QImage.Format.Format_RGB888)
        image.fill(QColor(255, 255, 255))
        page.frame.resize(200, 150)
        page.frame.setPixmap(QPixmap.fromImage(image))
        page._pick_at(QPointF(page.frame.width() / 2,
                              page.frame.height() / 2))
        self.assertIn("clipped", page.status.text())
        self.assertNotIn("color.temperature", page.changes)

    def test_the_crop_overlay_moves_and_resizes_in_fractions(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QImage, QPixmap

        from opencull_qt.develop import PhotoLabel
        from opencull_qt.finetune import CropOverlay

        frame = PhotoLabel()
        frame.resize(400, 300)
        image = QImage(1200, 900, QImage.Format.Format_RGB888)
        image.fill(QColor(60, 50, 40))
        frame.set_source(QPixmap.fromImage(image))
        overlay = CropOverlay(frame)
        overlay.setGeometry(frame.rect())
        overlay.rect_f = [0.2, 0.2, 0.5, 0.5]

        class Event:
            def __init__(self, x, y):
                self._at = QPointF(x, y)

            def position(self):
                return self._at

        centre = overlay._frame_rect().center()
        overlay.mousePressEvent(Event(centre.x(), centre.y()))
        overlay.mouseMoveEvent(Event(centre.x() + 40, centre.y()))
        overlay.mouseReleaseEvent(Event(0, 0))
        self.assertGreater(overlay.rect_f[0], 0.2)
        self.assertAlmostEqual(overlay.rect_f[2], 0.5, places=3)
        # A corner drag resizes; the rectangle never leaves the frame.
        corner = overlay._frame_rect().bottomRight()
        overlay.mousePressEvent(Event(corner.x(), corner.y()))
        overlay.mouseMoveEvent(Event(corner.x() + 4000, corner.y() + 4000))
        overlay.mouseReleaseEvent(Event(0, 0))
        self.assertLessEqual(overlay.rect_f[0] + overlay.rect_f[2], 1.0)
        self.assertLessEqual(overlay.rect_f[1] + overlay.rect_f[3], 1.0)

    def test_applying_the_crop_folds_it_into_the_recipe(self):
        page = self.page()
        page.crop_button.setChecked(True)
        page._crop_overlay.rect_f = [0.1, 0.1, 0.6, 0.6]
        page._crop_apply()
        self.assertFalse(page.crop_button.isChecked())
        held = {op["op"]: op for op in page.recipe["operations"]}
        self.assertEqual(held["geometry.crop"]["value"]["width"], 0.6)
        # And undo takes the frame back off.
        page._remember_key = ""
        page.undo()
        self.assertNotIn("crop", page.changes)

    def test_a_painted_stroke_lands_in_the_recipe_and_renders(self):
        import numpy as np
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QImage, QPixmap

        from development_engine import mask_weights

        page = self.page()
        page._create_mask("brush")
        ordinal = adjustments.masks(page.recipe)[-1]["ordinal"]
        # A proof on the pane to paint over.
        image = QImage(640, 480, QImage.Format.Format_RGB888)
        image.fill(QColor(60, 50, 40))
        page.frame.resize(320, 240)
        page.frame.set_source(QPixmap.fromImage(image))
        page._begin_paint(ordinal, erasing=False)

        class Event:
            def __init__(self, x, y):
                self._at = QPointF(x, y)

            def position(self):
                return self._at

        centre = page._brush_canvas._shown().center()
        page._brush_canvas.mousePressEvent(
            Event(centre.x() - 30, centre.y()))
        page._brush_canvas.mouseMoveEvent(
            Event(centre.x() + 30, centre.y()))
        page._brush_canvas.mouseReleaseEvent(Event(0, 0))
        held = [op for op in page.recipe["operations"]
                if op.get("op") == "mask.brush"][0]
        self.assertTrue(held["value"].get("map"))
        weights = mask_weights(
            np.zeros((480, 640, 3), np.float32), "brush", held["value"])
        self.assertGreater(float(weights[240, 320]), 0.5)   # painted
        self.assertLess(float(weights[20, 20]), 0.05)       # untouched
        # The eraser takes it back off.
        page._begin_paint(ordinal, erasing=True)
        for _pass in range(3):
            page._brush_canvas.mousePressEvent(
                Event(centre.x() - 30, centre.y()))
            page._brush_canvas.mouseMoveEvent(
                Event(centre.x() + 30, centre.y()))
            page._brush_canvas.mouseReleaseEvent(Event(0, 0))
        held = [op for op in page.recipe["operations"]
                if op.get("op") == "mask.brush"][0]
        weights = mask_weights(
            np.zeros((480, 640, 3), np.float32), "brush", held["value"])
        self.assertLess(float(weights[240, 320]), 0.4)

    def test_the_histogram_reads_the_arriving_render(self):
        from PySide6.QtGui import QColor, QImage, QPixmap

        page = self.page()
        image = QImage(64, 48, QImage.Format.Format_RGB888)
        image.fill(QColor(128, 128, 128))
        page._rendered(page.current, page.treatment,
                       QPixmap.fromImage(image))
        self.assertIsNotNone(page.histogram._bins)
        self.assertEqual(int(page.histogram._bins[0].argmax()), 128)

    def test_the_colour_controls_wear_their_ramp_and_others_do_not(self):
        from opencull_qt.finetune import Control

        page = self.page()
        ramps = {
            widget.control["op"]: bool(widget.slider._ramp)
            for widget in page.findChildren(Control)}
        self.assertTrue(ramps.get("color.temperature"))
        self.assertTrue(ramps.get("color.tint"))
        self.assertTrue(ramps.get("color.saturation"))
        self.assertFalse(ramps.get("tone.exposure"))

    def test_the_action_buttons_sit_in_two_readable_rows(self):
        # Five buttons in one row of this panel clipped every label to a
        # syllable. Restore on one row, persist-and-ask on the next.
        page = self.page()
        page.resize(1100, 700)
        page.show()
        self.addCleanup(page.hide)
        QApplication.processEvents()
        restore = page.reset_button.mapToGlobal(
            page.reset_button.rect().center()).y()
        keep = page.preset_button.mapToGlobal(
            page.preset_button.rect().center()).y()
        self.assertGreater(keep, restore)

    def test_the_baseline_is_not_offered_because_it_has_nothing_to_move(self):
        page = self.page()
        self.assertNotIn(
            "calibrated", [item["id"] for item in page.treatments])

    def test_every_bounded_operation_becomes_a_control(self):
        page = self.page()
        compiled = [item.control["op"] for item in page.controls
                    if not item.absent]
        self.assertEqual(
            compiled,
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
        """It used to say "nothing here to move" and stop, then folded
        the unused controls behind a count -- and the fold read as
        absence: the photographer asked where the rest of the controls
        were. Every control is on the page now, always; only the keep
        button waits for something to actually be in the recipe."""
        page = self.page(recipe=json.dumps({"tone": ["make it nicer"]}))
        offered = {item.control["op"] for item in page.controls}
        self.assertEqual(offered, set(adjustments.LABELS))
        self.assertTrue(all(item.absent for item in page.controls))
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
        """And it follows the pixels, not the request: until a render
        lands the pane shows the frame as shot and says so; the caption
        settles when _rendered delivers the picture it describes."""
        from PySide6.QtGui import QPixmap

        page = self.page()
        self.assertIn("AS SHOT", page.caption.text())
        rendered = QPixmap(60, 40)
        page._rendered(page.current, page.treatment, rendered)
        self.assertEqual(page.caption.text(), "AS SUGGESTED")
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(-1.0))
        page._rendered(page.current, page.treatment, rendered)
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

    def test_words_reach_a_control_the_treatment_never_used(self):
        """It used to answer "no Vignette to move". Every control is on
        the page now, so the words move it -- and moving it is the ask
        that makes the operation real."""
        page = self.page()
        page.prompt.setText("vignette -10")
        page.speak()
        vignette = self.control(page, "finish.vignette")
        self.assertFalse(vignette.absent)
        self.assertAlmostEqual(vignette.value(), -10.0, delta=0.3)
        self.assertIn("Vignette", page.status.text())

    # --- keeping one ------------------------------------------------------

    def exported(self, page, deadline_s: float = 5.0) -> None:
        """Pump events until the worker-thread export announces itself."""
        import time

        from PySide6.QtWidgets import QApplication

        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline \
                and page.keep_button.text() != "Export":
            QApplication.instance().processEvents()
            time.sleep(0.01)

    def test_keeping_renders_at_full_size_with_the_adjustments(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        with mock.patch.object(
            page.workspace, "render_full",
            return_value={"render": {"variant": "standard-adjusted"}},
        ) as rendered:
            page.keep()
            self.exported(page)
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
            self.exported(page)
        self.assertIn("standard-adjusted", page.status.text())

    def test_a_failed_render_is_reported_rather_than_raised(self):
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        exposure.slider.setValue(exposure._tick(0.30))
        with mock.patch.object(
            page.workspace, "render_full", side_effect=RuntimeError("no decoder"),
        ):
            page.keep()
            self.exported(page)
        self.assertIn("no decoder", page.status.text())
        self.assertTrue(page.keep_button.isEnabled())


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class HistogramTests(unittest.TestCase):
    """What the pixels actually did, read from the pane's own proof."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def _pixmap(self, red, green, blue):
        from PySide6.QtGui import QColor, QImage, QPixmap

        image = QImage(64, 48, QImage.Format.Format_RGB888)
        image.fill(QColor(red, green, blue))
        return QPixmap.fromImage(image)

    def test_a_flat_grey_spikes_every_channel_at_its_level(self):
        from opencull_qt.finetune import Histogram

        widget = Histogram()
        widget.show_pixmap(self._pixmap(128, 128, 128))
        for bins in widget._bins:
            self.assertEqual(int(bins.argmax()), 128)

    def test_a_red_frame_puts_reds_mass_high_and_blues_low(self):
        from opencull_qt.finetune import Histogram

        widget = Histogram()
        widget.show_pixmap(self._pixmap(230, 20, 20))
        red, green, blue = widget._bins
        self.assertGreater(int(red.argmax()), 200)
        self.assertLess(int(blue.argmax()), 60)

    def test_nothing_shown_paints_nothing_and_does_not_crash(self):
        from PySide6.QtGui import QImage

        from opencull_qt.finetune import Histogram

        widget = Histogram()
        widget.show_pixmap(None)
        self.assertIsNone(widget._bins)
        canvas = QImage(200, 88, QImage.Format.Format_ARGB32)
        canvas.fill(0)
        widget.resize(200, 88)
        widget.render(canvas)


class CurvePanelTests(unittest.TestCase):
    """The curve the hand draws is the curve the renderer runs."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def panel(self, points=None):
        from opencull_qt.finetune import CurvePanel

        made = CurvePanel(points)
        made.resize(300, 170)
        return made

    def test_panel_and_curve_space_round_trip(self):
        panel = self.panel()
        for x, y in ((0, 0), (128, 128), (255, 255), (64, 200)):
            spot = panel._to_panel(x, y)
            back = panel._to_curve(spot)
            self.assertAlmostEqual(back[0], x, delta=1)
            self.assertAlmostEqual(back[1], y, delta=1)

    def test_a_middle_point_stays_between_its_neighbours(self):
        panel = self.panel([[0, 0], [128, 128], [255, 255]])
        panel._dragging = 1

        class Event:
            def position(self_inner):
                return panel._to_panel(250, 100)   # dragged past the end

        panel.mouseMoveEvent(Event())
        self.assertLess(panel.points[1][0], 255.0)
        self.assertGreater(panel.points[1][0], 0.0)

    def test_endpoints_move_only_up_and_down(self):
        panel = self.panel()
        panel._dragging = 0

        class Event:
            def position(self_inner):
                return panel._to_panel(90, 40)

        panel.mouseMoveEvent(Event())
        self.assertEqual(panel.points[0][0], 0.0)
        self.assertAlmostEqual(panel.points[0][1], 40, delta=2)

    def test_identity_reads_as_identity(self):
        self.assertTrue(self.panel().is_identity())
        self.assertFalse(self.panel([[0, 10], [255, 255]]).is_identity())


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

    def test_a_colour_ramp_paints_under_the_groove(self):
        """Temperature's blue-to-amber strip: left end cool, right warm,
        and the zone bands above it untouched."""
        from PySide6.QtGui import QImage

        slider = self.slider()
        slider.resize(300, 30)
        slider.set_zones(-5.0, 5.0, (-1.0, 0.7), (-2.5, 1.5))
        slider.set_ramp([(0.0, (64, 120, 210)), (0.5, (150, 150, 150)),
                         (1.0, (235, 160, 60))])
        canvas = QImage(300, 30, QImage.Format.Format_ARGB32)
        canvas.fill(0)
        slider.render(canvas)
        groove = slider._groove_rect()
        y = int(groove.bottom() + 3 + slider.RAMP / 2)
        left = canvas.pixelColor(int(groove.left() + 4), y)
        right = canvas.pixelColor(int(groove.right() - 4), y)
        self.assertGreater(left.blue(), left.red())    # cool end
        self.assertGreater(right.red(), right.blue())  # warm end

    def test_a_slider_without_a_ramp_paints_none(self):
        from PySide6.QtGui import QImage

        slider = self.slider()
        slider.resize(300, 30)
        slider.set_zones(-5.0, 5.0, (-1.0, 0.7), (-2.5, 1.5))
        canvas = QImage(300, 30, QImage.Format.Format_ARGB32)
        canvas.fill(0)
        slider.render(canvas)
        groove = slider._groove_rect()
        y = int(groove.bottom() + 3 + slider.RAMP / 2)
        # Below the groove there is only the widget's own background:
        # the two ends of the row match, where a ramp would part them.
        left = canvas.pixelColor(int(groove.left()) + 4, y)
        right = canvas.pixelColor(int(groove.right()) - 4, y)
        self.assertEqual(left.name(), right.name())


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


class LayerTests(FineTunePageTests):
    """Capture One's shape: the base layer always there, one layer per
    mask, and the whole control surface pointing at whichever layer is
    selected. Visibility rides the layer row.

    The first shape put mask cards at the bottom of the control scroll,
    and the photographer looked at the page and said there was no mask
    panel. Buried is invisible.
    """

    def masked_page(self):
        page = self.page()
        made = {"shape": "radial",
                "geometry": {"centre_x": 40.0, "centre_y": 45.0,
                             "radius": 25.0, "feather": 100},
                "effects": [{"op": "tone.exposure", "value": 0.5}]}
        page.changes.setdefault("+mask", []).append(made)
        page.recipe = adjustments.apply(page.recipe, {"+mask": [made]})
        page._show_controls()
        return page

    def test_the_base_layer_is_always_there(self):
        page = self.page()
        self.assertEqual(page.layers.count(), 1)
        self.assertIn("Base", page.layers.item(0).text())
        self.assertEqual(page.layer, 0)

    def test_a_mask_is_a_layer_with_a_visibility_tick(self):
        page = self.masked_page()
        self.assertEqual(page.layers.count(), 2)
        row = page.layers.item(1)
        self.assertIn("Mask · radial", row.text())
        self.assertEqual(row.checkState(), Qt.CheckState.Checked)

    def test_adding_a_mask_selects_its_layer(self):
        page = self.page()
        page._create_mask("radial")
        self.assertEqual(page.layer, len(adjustments.masks(page.recipe)))
        self.assertGreaterEqual(page.layers.count(), 2)

    def test_the_story_waits_behind_the_dot(self):
        # Seventeen sliders each trailing two lines of prose made the
        # column mostly prose; the story unfolds on the (!) instead.
        page = self.page()
        exposure = self.control(page, "tone.exposure")
        self.assertTrue(exposure.provenance.isHidden())
        exposure.about.setChecked(True)
        self.assertFalse(exposure.provenance.isHidden())
        self.assertIn("exposure +0.45", exposure.source.text())
        exposure.about.setChecked(False)
        self.assertTrue(exposure.provenance.isHidden())

    def test_rebuilding_the_column_keeps_the_scroll_where_it_was(self):
        page = self.page()
        page.resize(1100, 500)
        page.show()
        self.addCleanup(page.hide)
        QApplication.processEvents()
        bar = page._scroll.verticalScrollBar()
        if bar.maximum() == 0:
            self.skipTest("column fits without scrolling at this size")
        bar.setValue(bar.maximum() // 2)
        held = bar.value()
        page._show_controls()
        # The purge and the relayout land on successive event passes;
        # the running application's loop spins them back to back.
        for _round in range(4):
            QApplication.processEvents()
        self.assertAlmostEqual(bar.value(), held, delta=40)

    def test_selecting_the_layer_points_the_surface_at_the_mask(self):
        page = self.masked_page()
        page._chose_layer(1)
        present = [item.control for item in page.controls
                   if not item.absent]
        self.assertEqual([item["op"] for item in present],
                         ["tone.exposure"])
        self.assertTrue(all(
            str(item.control["id"]).startswith("mask:1/")
            for item in page.controls))

    def test_the_mask_layer_offers_the_whole_instrument(self):
        page = self.masked_page()
        page._chose_layer(1)
        offered = {item.control["op"] for item in page.controls}
        self.assertEqual(offered, set(adjustments.LABELS))

    def test_touching_a_quiet_control_on_a_mask_layer_adds_its_effect(self):
        page = self.masked_page()
        page._chose_layer(1)
        page._control_wanted("tone.shadow", 20.0)
        placed = adjustments.masks(page.recipe)[0]
        self.assertIn("tone.shadow",
                      [item["op"] for item in placed["effects"]])
        self.assertEqual(
            page.changes["mask:1"]["add_effects"],
            [{"op": "tone.shadow", "value": 20.0}])

    def test_unticking_the_row_switches_the_mask_off(self):
        page = self.masked_page()
        row = page.layers.item(1)
        row.setCheckState(Qt.CheckState.Unchecked)
        self.assertIs(page.changes["mask:1"]["enabled"], False)
        self.assertFalse(adjustments.masks(page.recipe)[0]["enabled"])

    def test_the_geometry_rides_the_selected_layer(self):
        from opencull_qt.finetune import GeometrySlider

        page = self.masked_page()
        page._chose_layer(1)
        keys = [slider.key for slider in
                page.findChildren(GeometrySlider)]
        self.assertIn("radius", keys)
        page._chose_layer(0)
        from opencull_qt.finetune import HslPanel

        loose = [slider for slider in page.findChildren(GeometrySlider)
                 if not isinstance(slider.parent(), HslPanel)
                 and slider is not page._crop_angle
                 and slider.key != "preserve"]   # the curve's colour hold
        self.assertEqual(loose, [])

    def test_base_layer_resets_do_not_touch_the_masks_moves(self):
        page = self.masked_page()
        page._chose_layer(1)
        page._control_wanted("tone.shadow", 20.0)
        page._chose_layer(0)
        self.control(page, "tone.exposure").slider.setValue(900)
        page.reset_section("Tone")
        placed = adjustments.masks(page.recipe)[0]
        self.assertIn("tone.shadow",
                      [item["op"] for item in placed["effects"]])

    def test_a_mask_layers_section_reset_drops_only_its_own(self):
        page = self.masked_page()
        page._chose_layer(1)
        page._control_wanted("tone.shadow", 20.0)
        page._control_wanted("detail.dehaze", 8.0)
        page.reset_section("Tone")
        placed = adjustments.masks(page.recipe)[0]
        ops = [item["op"] for item in placed["effects"]]
        self.assertNotIn("tone.shadow", ops)
        self.assertIn("detail.dehaze", ops)



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


class SectionResetTests(FineTunePageTests):
    """One section back to asked, the others' moves left standing.

    The page-wide reset was all or nothing: a photographer who had
    tuned tone for ten minutes and then wandered colour into the weeds
    could only abandon both.
    """

    def reset_buttons(self, page) -> dict[str, object]:
        from PySide6.QtWidgets import QPushButton

        found = {}
        for button in page.findChildren(QPushButton):
            if button.text() == "reset":
                found[button.toolTip()] = button
        return found

    def test_an_untouched_page_offers_no_section_resets(self):
        page = self.page()
        self.assertEqual(self.reset_buttons(page), {})

    def test_every_control_is_on_the_page_not_folded_behind_a_count(self):
        page = self.page()
        offered = {item.control["op"] for item in page.controls}
        self.assertEqual(offered, set(adjustments.LABELS))
        compiled = [item for item in page.controls if not item.absent]
        quiet = [item for item in page.controls if item.absent]
        self.assertTrue(compiled and quiet)
        # The treatment's own controls lead their sections; the quiet
        # ones follow, unchecked and still until touched.
        self.assertFalse(quiet[0].enabled.isChecked())
        self.assertFalse(quiet[0].slider.isEnabled())

    def test_a_moved_section_grows_the_reset_word(self):
        page = self.page()
        moved = self.control(page, "tone.exposure")
        moved.slider.setValue(900)
        page._show_controls()
        self.assertEqual(page._touched_sections(), {"Tone"})
        self.assertEqual(len(self.reset_buttons(page)), 1)

    def test_resetting_one_section_leaves_the_others_moves_alone(self):
        page = self.page()
        self.control(page, "tone.exposure").slider.setValue(900)
        colour = next((item for item in page.controls
                       if item.control["section"] == "Colour"), None)
        if colour is None:
            page.changes["color.saturation"] = {"value": -30.0}
        else:
            colour.slider.setValue(100)
        page.reset_section("Tone")
        self.assertNotIn("Tone", page._touched_sections())
        self.assertIn("Colour", page._touched_sections())

    def test_an_inserted_control_is_undone_by_its_sections_reset(self):
        page = self.page()
        page._control_wanted("detail.dehaze", 12.0)
        self.assertIn("detail.dehaze",
                      [item["op"] for item in
                       page.recipe.get("operations", [])])
        page.reset_section("Detail")
        self.assertEqual(page.changes.get("+insert", []), [])
        self.assertNotIn("detail.dehaze",
                         [item["op"] for item in
                          page.recipe.get("operations", [])])

    def test_the_page_wide_reset_removes_an_added_mask(self):
        """Masks are layers now, not a section: the layer's own resets
        cover its moves, and the added layer itself falls to the
        page-wide reset like any other structure."""
        page = self.page()
        made = {"shape": "radial",
                "geometry": {"centre_x": 50.0, "centre_y": 50.0,
                             "radius": 30.0, "feather": 100},
                "effects": [{"op": "tone.exposure", "value": 0.3}]}
        page.changes.setdefault("+mask", []).append(made)
        page.recipe = adjustments.apply(page.recipe, {"+mask": [made]})
        before = len(adjustments.masks(page._pristine))
        self.assertEqual(len(adjustments.masks(page.recipe)), before + 1)
        page.reset()
        self.assertEqual(len(adjustments.masks(page.recipe)), before)
        self.assertNotIn("+mask", page.changes)

    def test_the_page_wide_reset_undoes_structure_too(self):
        page = self.page()
        page._control_wanted("detail.dehaze", 12.0)
        page.reset()
        self.assertEqual(page.changes, {})
        self.assertEqual(
            [item["op"] for item in page.recipe.get("operations", [])],
            [item["op"] for item in page._pristine.get("operations", [])])

    def test_after_a_section_reset_the_survivors_still_render(self):
        """The mirror is derived, never edited: pristine plus what
        remains must equal what the renderer will be sent."""
        page = self.page()
        self.control(page, "tone.exposure").slider.setValue(900)
        page._control_wanted("detail.dehaze", 12.0)
        page.reset_section("Tone")
        rendered = adjustments.apply(page._pristine, page.changes)
        self.assertEqual(
            [item["op"] for item in page.recipe["operations"]],
            [item["op"] for item in rendered["operations"]])


class ProofEdgeTests(FineTunePageTests):
    """Render what the pane can show, and not a pixel more.

    Fine tuning re-renders on every slider move, and it was paying for
    a 1600px proof to fill a pane that is usually far smaller.
    """

    def test_the_edge_is_the_frames_size_snapped_up(self):
        page = self.page()
        page.frame.resize(900, 600)
        # 900 physical pixels at ratio 1 snaps up to 1024.
        if float(page.frame.devicePixelRatioF()) == 1.0:
            self.assertEqual(page.proof_edge(), 1024)

    def test_nudging_the_window_does_not_orphan_the_cache(self):
        """870 and 900 wide must ask for the same render."""
        page = self.page()
        page.frame.resize(870, 580)
        first = page.proof_edge()
        page.frame.resize(900, 600)
        self.assertEqual(page.proof_edge(), first)

    def test_a_wall_sized_window_is_capped_at_the_full_proof(self):
        from opencull_qt.develop import PROOF_EDGE

        page = self.page()
        page.frame.resize(4000, 2600)
        self.assertEqual(page.proof_edge(), PROOF_EDGE)

    def test_a_pane_at_its_minimum_gets_the_floor_not_the_full_proof(self):
        """The label clamps itself to 160x120, so the smallest honest
        answer is the 512 floor -- which is exactly the waste being
        fixed: a 1600px render for a pane this size."""
        page = self.page()
        page.frame.resize(10, 10)   # clamped to the label's own minimum
        self.assertEqual(page.proof_edge(), 512)

    def test_the_render_is_asked_for_at_that_edge(self):
        page = self.page()
        page.frame.resize(900, 600)
        asked = []
        page.renderer.render = (
            lambda *args, **kwargs: asked.append(kwargs.get("maximum")))
        page.render()
        self.assertEqual(asked, [page.proof_edge()])


class AsShotStateTests(FineTunePageTests):
    """Two defaults, one page: the recipe's own, and nothing at all.

    "As suggested" existed; back-to-shot only existed as the hold-B
    peek or as unticking every operation by hand. This is the working
    state: everything off, buildable-up-from, keepable.
    """

    def test_as_shot_switches_every_operation_off(self):
        page = self.page()
        page.reset_to_shot()
        for operation in page.recipe.get("operations", []):
            if str(operation.get("op", "")).startswith("guardrail."):
                continue
            self.assertIs(operation.get("enabled"), False,
                          operation.get("op"))

    def test_masks_are_switched_off_too(self):
        page = self.page()
        made = {"shape": "radial",
                "geometry": {"centre_x": 50.0, "centre_y": 50.0,
                             "radius": 30.0, "feather": 100},
                "effects": [{"op": "tone.exposure", "value": 0.3}]}
        page.changes.setdefault("+mask", []).append(made)
        page.recipe = adjustments.apply(page.recipe, {"+mask": [made]})
        page._pristine = json.loads(json.dumps(page.recipe))
        page.reset_to_shot()
        self.assertFalse(adjustments.masks(page.recipe)[0]["enabled"])

    def test_it_is_a_working_state_not_an_erasure(self):
        """The switches stay on the page; one can be turned back on."""
        page = self.page()
        page.reset_to_shot()
        exposure = self.control(page, "tone.exposure")
        self.assertFalse(exposure.enabled.isChecked())
        exposure.enabled.setChecked(True)
        applied = adjustments.apply(page._pristine, page.changes)
        turned = next(item for item in applied["operations"]
                      if item.get("op") == "tone.exposure")
        self.assertIsNot(turned.get("enabled"), False)

    def test_as_suggested_restores_the_treatment_whole(self):
        page = self.page()
        page.reset_to_shot()
        page.reset()
        self.assertEqual(page.changes, {})
        for operation in page.recipe.get("operations", []):
            self.assertIsNot(operation.get("enabled"), False)

    def test_the_state_rides_the_changes_dict_to_the_renderer(self):
        page = self.page()
        page.frame.resize(900, 600)
        page.reset_to_shot()
        asked = []
        page.renderer.render = (
            lambda *args, **kwargs: asked.append(kwargs.get("adjustments")))
        page.render()
        self.assertTrue(asked and asked[0])
        self.assertTrue(all(
            change.get("enabled") is False for change in asked[0].values()))

    def test_both_defaults_sit_side_by_side(self):
        page = self.page()
        self.assertEqual(page.shot_button.text(), "As shot")
        self.assertEqual(page.reset_button.text(), "As suggested")


class HoldTests(FineTunePageTests):
    """Press-and-hold: the judgement happens in one place at one size."""

    def page_with_frames(self):
        from PySide6.QtGui import QPixmap

        page = self.page()
        adjusted = QPixmap(60, 40); adjusted.fill(Qt.GlobalColor.darkBlue)
        shot = QPixmap(60, 40); shot.fill(Qt.GlobalColor.darkRed)
        page._plain_pixmap = adjusted
        page._as_shot_pixmap = shot
        page.frame.set_source(adjusted)
        return page, adjusted, shot

    def test_holding_shows_as_shot_and_letting_go_returns(self):
        page, adjusted, shot = self.page_with_frames()
        page.hold(True)
        self.assertTrue(page._holding)
        self.assertEqual(page.caption.text(), "AS SHOT")
        page.hold(False)
        self.assertFalse(page._holding)
        self.assertIn(page.caption.text(), ("AS SUGGESTED", "AS ADJUSTED"))

    def test_the_b_key_is_the_hold(self):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        page, _adjusted, _shot = self.page_with_frames()
        press = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_B,
                          Qt.KeyboardModifier.NoModifier)
        release = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_B,
                            Qt.KeyboardModifier.NoModifier)
        page.keyPressEvent(press)
        self.assertTrue(page._holding)
        page.keyReleaseEvent(release)
        self.assertFalse(page._holding)

    def test_a_render_arriving_during_a_hold_does_not_steal_the_frame(self):
        from PySide6.QtGui import QPixmap

        page, _adjusted, shot = self.page_with_frames()
        page.current, page.treatment = page.current or "A.JPG", page.treatment or "t"
        page.hold(True)
        fresh = QPixmap(60, 40); fresh.fill(Qt.GlobalColor.black)
        page._rendered(page.current, page.treatment, fresh)
        # The new render is kept for the release, but the hold still
        # shows the frame as shot.
        self.assertIs(page._plain_pixmap, fresh)
        self.assertTrue(page._holding)

    def test_changing_photo_forgets_the_render_and_reloads_the_shot(self):
        """The old render must never be compared against a new frame --
        and the pane must not go blank either: the new photograph's own
        as-shot is on screen before the first render lands."""
        page, adjusted, shot = self.page_with_frames()
        page.show_photo(page.photos[0])
        self.assertIsNone(page._plain_pixmap)
        self.assertIsNotNone(page._as_shot_pixmap)
        self.assertIsNot(page._as_shot_pixmap, shot)


class FilmstripTests(FineTunePageTests):
    """The treatments under the picture, each with its own rendering."""

    def test_the_strip_is_horizontal_under_the_frame(self):
        page = self.page()
        self.assertEqual(page.treatment_list.flow(),
                         page.treatment_list.Flow.LeftToRight)
        # It lives in the stage column, not the control panel: the
        # picture's parent chain and the strip's converge before the
        # panel does.
        self.assertIs(page.treatment_list.parentWidget(),
                      page.frame.parentWidget())

    def test_a_thumbnail_lands_on_its_own_row(self):
        from PySide6.QtGui import QPixmap

        page = self.page()
        picture = QPixmap(60, 40)
        picture.fill(Qt.GlobalColor.darkGreen)
        treatment = str(page.treatments[0]["id"])
        page._thumb_ready(page.current, treatment, picture)
        self.assertFalse(page.treatment_list.item(0).icon().isNull())
        self.assertIn((page.current, treatment), page.previews)

    def test_a_stale_thumbnail_is_kept_but_not_painted(self):
        from PySide6.QtGui import QPixmap

        page = self.page()
        picture = QPixmap(60, 40)
        picture.fill(Qt.GlobalColor.darkGreen)
        page._thumb_ready("SOMEBODY-ELSE.JPG", "standard", picture)
        self.assertIn(("SOMEBODY-ELSE.JPG", "standard"), page.previews)
        self.assertTrue(page.treatment_list.item(0).icon().isNull())

    def test_the_pane_shows_the_frame_before_any_render(self):
        page = self.page()
        self.assertIsNotNone(page.frame._source)
        self.assertIn("AS SHOT", page.caption.text())


class RecipeFileTests(FineTunePageTests):
    """What leaves this machine must come back and render the same."""

    def test_the_file_round_trips_through_the_import(self):
        import tempfile as temporary_files

        from opencull_gui import adjustments

        page = self.page()
        page.changes = {"+insert": [{"op": "detail.dehaze", "value": 12.0}]}
        applied = adjustments.apply(page.recipe, page.changes)
        with temporary_files.TemporaryDirectory() as temporary:
            target = Path(temporary) / "look.recipe.json"
            with unittest.mock.patch(
                    "PySide6.QtWidgets.QFileDialog.getSaveFileName",
                    return_value=(str(target), "")):
                page.save_recipe_file()
            self.assertTrue(target.is_file())
            written = json.loads(target.read_text())
            self.assertEqual(written["format"],
                             "darkimiya-portable-recipe-v1")
            self.assertEqual(written["recipe"]["operations"],
                             applied["operations"])
            # And the develop page's import accepts it.
            kept = page.workspace.import_recipe(target)
            self.assertEqual(kept["recipe"]["recipe"]["operations"],
                             applied["operations"])

    def test_declining_the_dialog_writes_nothing(self):
        page = self.page()
        with unittest.mock.patch(
                "PySide6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=("", "")):
            page.save_recipe_file()   # must simply return


class KeyStepTests(unittest.TestCase):
    """One arrow key, one honest unit."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_an_ev_slider_steps_a_hundredth(self):
        from opencull_qt.finetune import Control

        made = Control({"id": "tone.exposure", "op": "tone.exposure",
                        "label": "Exposure", "section": "Tone",
                        "value": 0.0, "asked": 0.0, "unit": "EV",
                        "low": -5.0, "high": 5.0, "enabled": True,
                        "source": ""})
        self.assertEqual(made.slider.singleStep(), 1)      # 0.01 of 10 EV
        self.assertEqual(made.slider.pageStep(), 10)

    def test_a_percent_slider_steps_a_percent(self):
        from opencull_qt.finetune import Control

        made = Control({"id": "tone.shadow", "op": "tone.shadow",
                        "label": "Shadows", "section": "Tone",
                        "value": 0.0, "asked": 0.0, "unit": "percent",
                        "low": -100.0, "high": 100.0, "enabled": True,
                        "source": ""})
        self.assertEqual(made.slider.singleStep(), 5)      # 1% of 200
        self.assertEqual(made.slider.pageStep(), 50)


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
