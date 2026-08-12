"""The native develop page: comparison, treatments, and rendering on request."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFocusEvent, QKeyEvent
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui.photos import PhotoStore  # noqa: E402
from opencull_gui.report import load_report  # noqa: E402

NAMES = ["A.JPG", "B.JPG", "C.JPG"]


def build_shoot(root: Path) -> tuple[Path, Path]:
    photos = root / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(NAMES):
        Image.new("RGB", (160, 120), (60 + index * 20, 100, 90)).save(
            photos / name)
    report = root / "shoot-results.json"
    report.write_text(json.dumps({
        "format": "opencull-report-v2", "manifest_sha256": "x",
        "clusters": [{"cluster_id": "group-0001", "photos": NAMES}],
        "keep": [{"cluster_id": "group-0001", "photos": [NAMES[0]],
                  "rationale": "sharpest", "confidence": 0.8, "warning": "",
                  "fallback": False, "photographic_assessment": []}],
        "warnings": [], "adaptive_clustering": {"enabled": False},
        "notice": "read only",
    }), encoding="utf-8")
    return report, photos


def assess_and_suggest(root: Path, report_path: Path, photos: Path,
                       marked=("A.JPG",), styles=("standard", "signature")):
    """Put a shoot through assessment and suggestions, on disk.

    The develop page finds these by convention rather than by being told,
    so the test writes them where the convention says they live.
    """
    import hashlib

    from opencull_gui.project import ensure_project_layout
    from opencull_gui.shortlist import ASSESSMENT_FIELDS

    layout = ensure_project_layout(photos)
    shortlist_path = (
        layout["Reports"] / f"{report_path.stem}.professional-shortlist.json")
    shortlist_path.write_text(json.dumps({
        "format": "opencull-professional-shortlist-v1",
        "source_report_sha256": hashlib.sha256(
            report_path.read_bytes()).hexdigest(),
        "candidate_policy": "effective", "candidate_signature": "sig",
        "entries": [
            {"rank": index + 1, "photo": name, "cluster_id": "group-0001",
             "tier": "strong", "score": 80, "confidence": 0.8,
             "rationale": "worth a look", "raw_files": [],
             "assessment": dict.fromkeys(ASSESSMENT_FIELDS, "seen"),
             "warnings": []}
            for index, name in enumerate(NAMES)
        ],
    }), encoding="utf-8")

    review_path = shortlist_path.with_suffix(".review.json")
    review_path.write_text(json.dumps({
        "format": "opencull-professional-shortlist-review-v1",
        "shortlist_path": str(shortlist_path),
        "source_report_sha256": hashlib.sha256(
            report_path.read_bytes()).hexdigest(),
        "candidate_signature": "sig", "revision": 1,
        "entries": {
            photo: {"tier": "strong", "edit_raw": False, "interesting": True,
                    "reviewed": True, "note": "", "updated_at": "now"}
            for photo in marked},
        "history": [], "migrations": [],
    }), encoding="utf-8")

    # A recipe is sections of plain instructions, carried as JSON text, and
    # the compiler turns each line into a typed operation.
    recipe = json.dumps({
        "tone": ["increase exposure by 0.2 stops", "add 4 contrast"],
        "color": ["lift saturation by 6"],
    })
    treatments: dict = {}
    for style in styles:
        treatments[f"{style}_title"] = f"{style.title()} treatment"
        treatments[f"{style}_intent"] = f"what {style} is for"
        treatments[f"{style}_recipe"] = recipe
    (layout["Recipes"] /
     f"{shortlist_path.stem}.edit-directions-r1.json").write_text(json.dumps({
        "format": "opencull-edit-directions-v1",
        "shortlist_sha256": hashlib.sha256(
            shortlist_path.read_bytes()).hexdigest(),
        "review_revision": 1,
        "entries": [
            {"photo": photo, "guardrails": "keep skin believable",
             **treatments}
            for photo in marked
        ],
    }), encoding="utf-8")
    return shortlist_path


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class SuggestedTreatmentTests(unittest.TestCase):
    """What the suggestion pass produced has to reach the develop page."""

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

    def workspace(self):
        from opencull_qt.develop import workspace_for

        return workspace_for(self.report, self.photos.root, decoders=set())

    def test_without_an_assessment_only_the_baseline_is_offered(self):
        self.assertEqual(
            [item["id"] for item in self.workspace().treatments("A.JPG")],
            ["calibrated"])

    def test_suggested_treatments_reach_the_develop_page(self):
        assess_and_suggest(self.root, self.report_path, self.photos_path)
        offered = self.workspace().treatments("A.JPG")
        self.assertEqual(
            [item["id"] for item in offered],
            ["calibrated", "standard", "signature"])
        self.assertEqual(offered[1]["name"], "Standard treatment")
        self.assertEqual(offered[1]["intent"], "what standard is for")

    def test_a_frame_nobody_marked_gets_no_suggested_treatments(self):
        assess_and_suggest(
            self.root, self.report_path, self.photos_path, marked=("A.JPG",))
        self.assertEqual(
            [item["id"] for item in self.workspace().treatments("B.JPG")],
            ["calibrated"])

    def test_a_shortlist_from_a_different_cull_is_treated_as_absent(self):
        assess_and_suggest(self.root, self.report_path, self.photos_path)
        from opencull_gui.project import ensure_project_layout

        layout = ensure_project_layout(self.photos_path)
        shortlist = (
            layout["Reports"] /
            f"{self.report_path.stem}.professional-shortlist.json")
        value = json.loads(shortlist.read_text(encoding="utf-8"))
        value["source_report_sha256"] = "a different cull entirely"
        shortlist.write_text(json.dumps(value), encoding="utf-8")
        # The develop page still opens; it simply has no suggestions.
        self.assertEqual(
            [item["id"] for item in self.workspace().treatments("A.JPG")],
            ["calibrated"])

    def test_a_suggested_treatment_renders(self):
        assess_and_suggest(self.root, self.report_path, self.photos_path)
        rendered = self.workspace().recipe_preview(
            "A.JPG", "standard", "default", "markesteijn-3-pass", 80)
        self.assertTrue(rendered.is_file())


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class VerificationTests(unittest.TestCase):
    """Checking that a rendering did what its treatment said it would."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.report_path, self.photos_path = build_shoot(self.root)
        self.report = load_report(self.report_path)
        self.photos = PhotoStore(self.photos_path, self.root / "cache")
        assess_and_suggest(self.root, self.report_path, self.photos_path)
        self.addCleanup(self._temporary.cleanup)

    def workspace(self):
        from opencull_qt.develop import workspace_for

        return workspace_for(self.report, self.photos.root, decoders=set())

    def page(self):
        from opencull_qt.develop import DevelopPage
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.photos)
        page = DevelopPage(self.report, self.workspace(), loader)
        page.show()
        self.addCleanup(page.shutdown)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def wait_for(self, condition, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def choose(self, page, treatment):
        ids = [item["id"] for item in page.available]
        page.choose_treatment(ids.index(treatment))

    def test_the_baseline_has_no_claim_to_check(self):
        # It is asked to interpret nothing, so there is nothing to verify
        # against and the control says why rather than being offered.
        page = self.page()
        self.choose(page, "calibrated")
        self.assertEqual(page.suggestion(), "")
        self.assertFalse(page.verify_button.isEnabled())
        self.assertIn("no claim to check", page.verify_button.toolTip())

    def test_a_suggested_treatment_carries_what_it_promised(self):
        page = self.page()
        self.choose(page, "standard")
        suggestion = page.suggestion()
        self.assertIn("what standard is for", suggestion)
        self.assertIn("keep skin believable", suggestion)
        self.assertTrue(page.verify_button.isEnabled())

    def test_verifying_asks_about_the_full_render_not_the_proof(self):
        page = self.page()
        self.choose(page, "standard")
        asked = []
        page.verification_wanted.connect(lambda request: asked.append(request))
        page.verify_current()
        self.assertTrue(
            self.wait_for(lambda: asked), "the verification was never asked for")
        request = asked[0]
        self.assertEqual(request["photo"], "A.JPG")
        developed = Path(request["developed"])
        self.assertTrue(developed.is_file())
        with Image.open(developed) as rendered:
            # The proof is bounded; a delivery is the photograph's own size.
            self.assertEqual(max(rendered.size), 160)
        self.assertEqual(
            Path(request["original"]).name, "A.JPG")
        self.assertIn("what standard is for", request["suggestion"])

    def test_a_certificate_is_bound_to_the_file_it_judged(self):
        workspace = self.workspace()
        result = workspace.render_full(
            "A.JPG", "standard", "default", "markesteijn-3-pass")
        developed = str(result["render"]["path"])
        self.assertIsNone(workspace.verification_for(developed))

        certificate = self.root / "A.semantic-verification.json"
        certificate.write_text(json.dumps({
            "format": "opencull-semantic-verification-v1",
            "evidence": {"developed": {"path": developed, "sha256": "x"}},
            "suggestion": "what standard is for",
            "judgment": {"satisfactory": True, "confidence": 0.9,
                         "concerns": [], "reasoning": "It did what it said."},
        }), encoding="utf-8")
        from opencull_gui.project import register_file_artifact

        register_file_artifact(
            workspace.project_path, "verifications", certificate,
            stage="verify")

        found = workspace.verification_for(developed)
        self.assertIsNotNone(found)
        self.assertTrue(found["judgment"]["satisfactory"])
        # A different rendering is not covered by it.
        self.assertIsNone(
            workspace.verification_for(self.root / "somewhere-else.jpg"))

    def test_the_verdict_is_shown_beside_the_treatment(self):
        page = self.page()
        self.choose(page, "standard")
        self.assertFalse(page.verdict.isVisible())

        workspace = page.workspace
        result = workspace.render_full(
            "A.JPG", "standard", "default", "markesteijn-3-pass")
        certificate = self.root / "A.semantic-verification.json"
        certificate.write_text(json.dumps({
            "format": "opencull-semantic-verification-v1",
            "evidence": {"developed": {
                "path": str(result["render"]["path"]), "sha256": "x"}},
            "suggestion": "s",
            "judgment": {"satisfactory": False, "confidence": 0.4,
                         "concerns": ["the sky has gone cyan"],
                         "reasoning": "The tone moved further than asked."},
        }), encoding="utf-8")
        from opencull_gui.project import register_file_artifact

        register_file_artifact(
            workspace.project_path, "verifications", certificate,
            stage="verify")

        page._show_verdict()
        self.assertTrue(page.verdict.isVisible())
        self.assertIn("Not satisfied", page.verdict.text())
        self.assertIn("the sky has gone cyan", page.verdict.text())
        self.assertEqual(page.verdict.property("tone"), "alarm")

    def test_a_failed_verification_says_why_and_frees_the_control(self):
        page = self.page()
        self.choose(page, "standard")
        page._verify_failed("A.JPG", "the reference photograph is unavailable")
        self.assertTrue(page.verify_button.isEnabled())
        self.assertIn("unavailable", page.status.text())
        self.assertEqual(page.status.property("tone"), "alarm")


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class DevelopPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.report_path, self.photos_path = build_shoot(root)
        self.report = load_report(self.report_path)
        self.photos = PhotoStore(self.photos_path, root / "cache")
        self.addCleanup(self._temporary.cleanup)

    def page(self, engines=None):
        from opencull_qt.develop import DevelopPage, workspace_for
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.photos)
        page = DevelopPage(
            self.report,
            workspace_for(self.report, self.photos.root,
                          decoders=set() if engines is None else engines),
            loader)
        page.resize(900, 600)
        page.show()
        self.addCleanup(page.shutdown)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def suggested_page(self, marked=(NAMES[0],)):
        """A develop page over a folder that has been through suggestions."""
        from opencull_qt.develop import DevelopPage, workspace_for
        from opencull_qt.previews import PreviewLoader

        assess_and_suggest(
            Path(self._temporary.name), self.report_path, self.photos_path,
            marked=marked)
        loader = PreviewLoader(self.photos)
        page = DevelopPage(
            self.report, workspace_for(self.report, self.photos.root,
                                       decoders=set()), loader)
        page.resize(900, 600)
        page.show()
        self.addCleanup(page.shutdown)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def wait_for(self, condition, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_every_photograph_in_the_selection_is_listed(self):
        page = self.page()
        self.assertEqual(
            [page.photos.item(row).data(Qt.ItemDataRole.UserRole)
             for row in range(page.photos.count())],
            NAMES)

    def test_a_folder_with_no_suggestions_keeps_every_frame_listed(self):
        # Nothing has been suggested, so narrowing to treated frames would
        # be an empty page. The filter does not even appear.
        page = self.page()
        self.assertEqual(page.treated_photos(), [])
        self.assertEqual(page.photos.count(), len(NAMES))
        self.assertFalse(page.scope_row.isVisibleTo(page))

    def test_the_list_opens_on_the_frames_that_have_treatments(self):
        page = self.suggested_page(marked=(NAMES[0], NAMES[1]))
        listed = [page.photos.item(row).data(Qt.ItemDataRole.UserRole)
                  for row in range(page.photos.count())]
        self.assertEqual(listed, sorted([NAMES[0], NAMES[1]]))
        self.assertTrue(page.scope_row.isVisibleTo(page))
        self.assertEqual(page.scope, "treated")
        self.assertTrue(page.scope_buttons["treated"].isChecked())
        # And it opens on one of them rather than on a frame it is hiding.
        self.assertIn(page.current, listed)
        self.assertEqual(page.photos.currentRow(), 0)
        self.assertIn("of", page.counter.text())

    def test_every_frame_is_one_click_away_because_the_baseline_is_free(self):
        page = self.suggested_page(marked=(NAMES[0],))
        page.scope_buttons["all"].click()
        listed = [page.photos.item(row).data(Qt.ItemDataRole.UserRole)
                  for row in range(page.photos.count())]
        self.assertEqual(listed, NAMES)
        # The frame being looked at survives the widening.
        self.assertEqual(page.current, NAMES[0])
        self.assertEqual(
            listed[page.photos.currentRow()], NAMES[0])

    def test_each_frame_is_listed_as_its_picture_not_only_its_name(self):
        page = self.page()
        self.assertTrue(self.wait_for(
            lambda: not page.photos.item(0).icon().isNull()))
        for row in range(page.photos.count()):
            item = page.photos.item(row)
            self.assertIn(
                item.data(Qt.ItemDataRole.UserRole), item.text())

    def test_every_treatment_shows_what_it_does_to_this_frame(self):
        page = self.suggested_page(marked=(NAMES[0],))
        self.assertGreater(page.treatments.count(), 1)
        self.assertTrue(
            self.wait_for(lambda: all(
                not page.treatments.item(row).icon().isNull()
                for row in range(page.treatments.count()))),
            "the treatments were never given their previews")
        # Rendered once and kept: coming back to a frame costs nothing.
        self.assertEqual(
            len(page.previews), page.treatments.count())
        before = dict(page.previews)
        page._request_previews()
        self.assertEqual(page.previews, before)

    def test_clicking_a_preview_asks_for_it_at_proof_size(self):
        page = self.suggested_page(marked=(NAMES[0],))
        asked = []
        page.develop_current = lambda: asked.append(page.treatment)
        page.treatments.itemClicked.emit(page.treatments.item(1))
        self.assertEqual(len(asked), 1)

    def test_moving_between_frames_does_not_ask_for_a_proof(self):
        # Only a click does. Selecting a frame must not spend a full
        # render nobody asked for.
        page = self.suggested_page(marked=(NAMES[0], NAMES[1]))
        asked = []
        page.renderer.render = lambda *a, **k: asked.append(a)
        page.show_photo(NAMES[1])
        self.assertEqual(asked, [])

    def test_a_culled_folder_offers_the_baseline_without_any_suggestion(self):
        # No shortlist, no edit directions, no provider call: the ordinary
        # state of a folder that has just been culled.
        page = self.page()
        self.assertEqual(
            [item["id"] for item in page.available], ["calibrated"])
        self.assertTrue(page.develop_button.isEnabled())

    def test_nothing_is_rendered_until_it_is_asked_for(self):
        page = self.page()
        self.assertEqual(page.rendered, {})
        self.assertIn("Not developed", page.stage.image.text())
        # Moving through the frames must not start rendering either. A
        # render costs real time, so it happens when a person asks.
        page.step(1)
        page.step(1)
        self.assertEqual(page.rendered, {})

    def test_developing_a_frame_produces_a_picture_and_keeps_the_original(self):
        page = self.page()
        before = (self.photos_path / "A.JPG").read_bytes()
        page.develop_current()
        self.assertTrue(
            self.wait_for(lambda: ("A.JPG", "calibrated") in page.rendered),
            "the render never arrived")
        self.assertEqual(page.stage.showing(), "treated")
        self.assertFalse(page.stage.image.source().isNull())
        self.assertEqual((self.photos_path / "A.JPG").read_bytes(), before)

    def test_the_second_look_at_a_render_does_not_render_again(self):
        page = self.page()
        page.develop_current()
        self.assertTrue(
            self.wait_for(lambda: ("A.JPG", "calibrated") in page.rendered))
        generation = page.renderer._generation
        page.develop_current()
        self.assertEqual(page.renderer._generation, generation)

    def test_moving_frames_abandons_a_render_that_is_no_longer_wanted(self):
        page = self.page()
        page.develop_current()
        generation = page.renderer._generation
        page.step(1)
        self.assertNotEqual(page.renderer._generation, generation)

    def test_the_page_says_which_rendering_it_is_about_to_make(self):
        # No RAW is matched here, so the note must say so rather than imply a
        # demosaic that is not happening.
        page = self.page()
        self.assertIn("No RAW", page.engine_note.text())

    def match_a_raw(self, page):
        """Point the first frame at a real RAW file beside the shoot."""
        raw = self.photos_path.parent / "A.ARW"
        raw.write_bytes(b"raw")
        page.workspace.raw_files = lambda photo: [str(raw)]

    def test_a_raw_falls_back_to_libraw_when_darktable_is_absent(self):
        # OpenCull's own renderer, not the camera's rendering. It is the
        # deterministic fallback, and it is a real decode.
        page = self.page(engines={"libraw"})
        self.match_a_raw(page)
        page._chose_treatment(0)
        self.assertIn("LibRaw", page.engine_note.text())
        self.assertEqual(page.engine_for("A.JPG"), "default")

    def test_a_raw_with_neither_decoder_is_named_as_the_camera_rendering(self):
        page = self.page(engines=set())
        self.match_a_raw(page)
        page._chose_treatment(0)
        note = page.engine_note.text()
        self.assertIn("camera's own embedded rendering", note)
        self.assertIn("not a development", note)
        self.assertEqual(page.engine_for("A.JPG"), "default")

    def test_a_raw_with_darktable_is_demosaiced(self):
        page = self.page(engines={"darktable", "libraw"})
        self.match_a_raw(page)
        page._chose_treatment(0)
        self.assertIn("demosaiced by darktable", page.engine_note.text())
        self.assertEqual(page.engine_for("A.JPG"), "darktable")

    def test_exporting_renders_at_full_size_and_writes_the_file(self):
        from unittest import mock

        page = self.page()
        target = self.photos_path.parent / "delivery" / "A.jpg"
        with mock.patch(
            "opencull_qt.develop.QFileDialog.getSaveFileName",
            return_value=(str(target), ""),
        ):
            page.export_current()
        self.assertTrue(
            self.wait_for(lambda: page.exporter.pending == 0),
            "the export never finished")
        self.assertTrue(target.is_file())
        with Image.open(target) as written:
            # The proof on screen is bounded to PROOF_EDGE; a delivery is not.
            self.assertEqual(max(written.size), 160)
        self.assertIn("exported to", page.status.text())
        # And the photograph it came from is untouched.
        self.assertTrue((self.photos_path / "A.JPG").is_file())

    def test_choosing_a_name_that_exists_reports_the_name_actually_written(self):
        from unittest import mock

        page = self.page()
        target = self.photos_path.parent / "A.jpg"
        target.write_bytes(b"already here")
        with mock.patch(
            "opencull_qt.develop.QFileDialog.getSaveFileName",
            return_value=(str(target), ""),
        ):
            page.export_current()
        self.assertTrue(self.wait_for(lambda: page.exporter.pending == 0))
        self.assertEqual(target.read_bytes(), b"already here")
        self.assertIn("A-2.jpg", page.status.text())
        self.assertIn("does not write over a file", page.status.text())

    def test_cancelling_the_chooser_exports_nothing(self):
        from unittest import mock

        page = self.page()
        with mock.patch(
            "opencull_qt.develop.QFileDialog.getSaveFileName",
            return_value=("", ""),
        ):
            page.export_current()
        self.assertEqual(page.exporter.pending, 0)
        self.assertTrue(page.export_button.isEnabled())

    def test_the_offered_name_says_the_frame_and_the_treatment(self):
        from unittest import mock

        page = self.page()
        seen = {}

        def remember(_parent, _title, path, *args, **kwargs):
            seen["path"] = path
            return ("", "")

        with mock.patch(
            "opencull_qt.develop.QFileDialog.getSaveFileName", remember
        ):
            page.export_current()
        self.assertTrue(seen["path"].endswith("A-calibrated.jpg"))
        # And it lands in the project's own Exports directory by default.
        self.assertIn("Exports", seen["path"])

    def test_a_failed_export_says_why_and_leaves_the_button_usable(self):
        page = self.page()
        page._export_failed("A.JPG", "the destination is not writable")
        self.assertTrue(page.export_button.isEnabled())
        self.assertIn("not writable", page.status.text())
        self.assertEqual(page.status.property("tone"), "alarm")

    def hold(self, page, down: bool, repeat: bool = False):
        # The autorepeat flag can only be set at construction, through the
        # longer constructor: there is no setter for it.
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress if down else QKeyEvent.Type.KeyRelease,
            Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ", repeat)
        if down:
            page.keyPressEvent(event)
        else:
            page.keyReleaseEvent(event)

    def develop(self, page):
        page.develop_current()
        self.assertTrue(
            self.wait_for(lambda: (page.current, page.treatment) in page.rendered),
            "the render never arrived")

    def test_holding_space_shows_the_frame_as_it_was_shot(self):
        page = self.page()
        self.develop(page)
        self.assertEqual(page.stage.showing(), "treated")
        self.hold(page, True)
        self.assertEqual(page.stage.showing(), "as shot")
        self.assertEqual(page.stage.caption.text(), "AS SHOT")
        self.hold(page, False)
        self.assertEqual(page.stage.showing(), "treated")

    def test_the_caption_always_says_which_one_is_on_screen(self):
        page = self.page()
        self.develop(page)
        self.assertEqual(page.stage.caption.text(), "CALIBRATED BASELINE")
        self.hold(page, True)
        self.assertEqual(page.stage.caption.text(), "AS SHOT")

    def test_a_repeating_key_is_not_read_as_a_release(self):
        # A held key repeats. Treating a repeat as a fresh press or release
        # would make the comparison flicker while the key is simply down.
        page = self.page()
        self.develop(page)
        self.hold(page, True)
        self.hold(page, True, repeat=True)
        self.hold(page, True, repeat=True)
        self.assertEqual(page.stage.showing(), "as shot")
        self.hold(page, False, repeat=True)
        self.assertEqual(page.stage.showing(), "as shot")
        self.hold(page, False)
        self.assertEqual(page.stage.showing(), "treated")

    def test_losing_the_keyboard_does_not_leave_it_held_down(self):
        page = self.page()
        self.develop(page)
        self.hold(page, True)
        page.focusOutEvent(QFocusEvent(QKeyEvent.Type.FocusOut))
        self.assertEqual(page.stage.showing(), "treated")

    def test_holding_before_developing_changes_nothing(self):
        page = self.page()
        self.assertEqual(page.stage.showing(), "as shot")
        self.hold(page, True)
        self.assertEqual(page.stage.showing(), "as shot")
        self.hold(page, False)

    def test_moving_frames_releases_the_comparison(self):
        page = self.page()
        self.develop(page)
        self.hold(page, True)
        page.step(1)
        self.assertFalse(page.stage.holding())

    def test_the_page_says_how_to_compare_once_there_is_something_to(self):
        page = self.page()
        self.assertEqual(page.stage.hint.text(), "")
        self.develop(page)
        self.assertIn("Hold space", page.stage.hint.text())

    def test_number_keys_choose_a_treatment(self):
        page = self.page()
        page.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_1,
            Qt.KeyboardModifier.NoModifier))
        self.assertEqual(page.treatment, "calibrated")

    def test_the_lists_never_take_the_keyboard_from_the_comparison(self):
        # A focused list would swallow the space bar to toggle its own row.
        page = self.page()
        for widget in (page.photos, page.treatments):
            self.assertEqual(widget.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_arrow_keys_walk_the_selection(self):
        page = self.page()
        page.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(page.current, "B.JPG")
        page.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(page.current, "A.JPG")

    def test_escape_leaves_the_page(self):
        page = self.page()
        closed = []
        page.closed.connect(lambda: closed.append(True))
        page.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
        self.assertEqual(closed, [True])

    def test_a_failed_render_says_why_and_leaves_the_button_usable(self):
        page = self.page()
        page._render_failed("A.JPG", "the reference photograph is unavailable")
        self.assertTrue(page.develop_button.isEnabled())
        self.assertIn("unavailable", page.status.text())
        self.assertEqual(page.status.property("tone"), "alarm")


class DecoderDetectionTests(unittest.TestCase):
    def test_nothing_is_assumed_to_be_installed(self):
        from opencull_gui.development import available_decoders

        self.assertLessEqual(available_decoders(), {"darktable", "libraw"})

    def test_libraw_needs_every_tool_it_shells_out_to(self):
        from unittest import mock

        from opencull_gui.development import raw_baseline_tools_present

        with mock.patch("shutil.which", lambda name: None):
            self.assertFalse(raw_baseline_tools_present())
        with mock.patch("shutil.which", lambda name: "/usr/bin/" + name):
            self.assertTrue(raw_baseline_tools_present())
        # ImageMagick 6 installs `convert`, not `magick`, and that is what
        # most Linux distributions still package.
        with mock.patch(
            "shutil.which",
            lambda name: None if name == "magick" else "/usr/bin/" + name,
        ):
            self.assertTrue(raw_baseline_tools_present())
        with mock.patch(
            "shutil.which",
            lambda name: None if name == "dcraw_emu" else "/usr/bin/" + name,
        ):
            self.assertFalse(raw_baseline_tools_present())


if __name__ == "__main__":
    unittest.main()
