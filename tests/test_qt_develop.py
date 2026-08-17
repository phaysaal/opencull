"""The native develop page: comparison, treatments, and rendering on request."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
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


def written(offered):
    """The treatments written for the photograph, without the presets.

    Presets are offered for every frame whatever anybody has said about
    it, so a test asking what was *suggested* has to leave them out or it
    is asking a different question.
    """
    return [item for item in offered if item.get("kind") != "preset"]


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
            [item["id"] for item in written(self.workspace().treatments("A.JPG"))],
            ["calibrated", "as-shot"])

    def test_suggested_treatments_reach_the_develop_page(self):
        assess_and_suggest(self.root, self.report_path, self.photos_path)
        offered = written(self.workspace().treatments("A.JPG"))
        self.assertEqual(
            [item["id"] for item in offered],
            ["calibrated", "standard", "signature", "as-shot"])
        self.assertEqual(offered[1]["name"], "Standard treatment")
        self.assertEqual(offered[1]["intent"], "what standard is for")

    def test_a_frame_nobody_marked_gets_no_suggested_treatments(self):
        assess_and_suggest(
            self.root, self.report_path, self.photos_path, marked=("A.JPG",))
        self.assertEqual(
            [item["id"] for item in written(self.workspace().treatments("B.JPG"))],
            ["calibrated", "as-shot"])

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
            [item["id"] for item in written(self.workspace().treatments("A.JPG"))],
            ["calibrated", "as-shot"])

    def test_a_suggested_treatment_renders(self):
        assess_and_suggest(self.root, self.report_path, self.photos_path)
        rendered = self.workspace().recipe_preview(
            "A.JPG", "standard", "default", "markesteijn-3-pass", 80)
        self.assertTrue(rendered.is_file())

    def test_a_given_payload_is_not_recomputed_per_treatment(self):
        # The payload reads every recipe the shortlist has; a caller
        # sweeping the whole selection reads it once and passes it in. When
        # it does, treatments must not read it again -- that per-frame
        # re-read is what made opening the develop page slow.
        assess_and_suggest(self.root, self.report_path, self.photos_path)
        ws = self.workspace()
        shared = ws.payload()
        with unittest.mock.patch.object(
                ws, "payload", side_effect=AssertionError("recomputed")):
            offered = ws.treatments("A.JPG", payload=shared)
        self.assertIn("standard", [item["id"] for item in offered])

    def test_the_sweep_leaves_out_the_presets_it_would_never_render(self):
        # A preset is never rendered ahead of being asked for, and building
        # its list costs a recipe compile each. Excluding them drops every
        # preset and nothing else.
        assess_and_suggest(self.root, self.report_path, self.photos_path)
        ws = self.workspace()
        full = ws.treatments("A.JPG")
        lean = ws.treatments("A.JPG", include_presets=False)
        self.assertFalse(any(item.get("kind") == "preset" for item in lean))
        self.assertEqual(
            [item["id"] for item in lean],
            [item["id"] for item in full if item.get("kind") != "preset"])


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class TreatmentEntryTests(unittest.TestCase):
    """The door to a Kimiya Treatment, and where the result appears.

    Until now the treatment could only be launched from a terminal by
    somebody who knew the program's name; the photographer it was built
    for had no way to ask for it.
    """

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

    def page(self):
        from opencull_qt.develop import DevelopPage, workspace_for
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.photos)
        self.workspace = workspace_for(
            self.report, self.photos.root, decoders=set())
        page = DevelopPage(self.report, self.workspace, loader)
        page.resize(900, 600)
        page.show()
        self.addCleanup(page.shutdown)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def report_file(self, photo: str) -> Path:
        """A finished treatment on disk, as the queue would leave it."""
        from opencull_gui.project import ensure_project_layout

        layout = ensure_project_layout(self.photos.root)
        path = layout["Recipes"] / f"{Path(photo).stem}.treatment.json"
        path.write_text(json.dumps({
            "format": "darkimiya-treatment-v1",
            "strategy": "protect-then-reveal",
            "photo": photo,
            "evidence": {"baseline": {}},
            "chosen_round": 1,
            "rounds": [{
                "round": 1, "render": "round-1.jpg",
                "sections": "protect the sky",
                "measurements": {"subject_separation": 20.0},
                "recipe": {
                    "format": "opencull-development-recipe-v1",
                    "title": "Dusk, held back",
                    "operations": [
                        {"op": "tone.exposure", "value": -0.5,
                         "unit": "EV", "mode": "delta",
                         "source": "exposure -0.5"}],
                },
            }],
        }))
        return path

    def register(self, path: Path) -> None:
        from opencull_gui.project import register_job_output

        register_job_output(self.workspace.project_path, {
            "kind": "treatment", "id": "job1", "output": str(path)})

    def test_the_preview_sweep_runs_once_per_selection_not_per_row(self):
        # The sweep pre-renders every frame's treatments; it need only run
        # when the shown set changes. A row change with the same set must
        # not sweep the whole selection again -- doing so per row is what
        # made navigating the develop list slow.
        page = self.page()
        shown = page.shown_photos()
        if len(shown) < 2:
            self.skipTest("needs at least two shown frames")
        self.assertEqual(page._swept, tuple(shown))
        target = next(name for name in shown if name != page.current)
        with unittest.mock.patch.object(page.thumbs, "want") as want:
            page.show_photo(target)
        want.assert_not_called()

    def test_the_button_asks_and_then_emits_the_frame_and_the_budget(self):
        page = self.page()
        heard = []
        page.treatment_wanted.connect(
            lambda photo, rounds: heard.append((photo, rounds)))
        with unittest.mock.patch(
                "opencull_qt.develop.QInputDialog.getInt",
                return_value=(4, True)):
            page.treat_button.click()
        self.assertEqual(heard, [(page.current, 4)])

    def test_declining_the_dialog_asks_for_nothing(self):
        page = self.page()
        heard = []
        page.treatment_wanted.connect(
            lambda photo, rounds: heard.append((photo, rounds)))
        with unittest.mock.patch(
                "opencull_qt.develop.QInputDialog.getInt",
                return_value=(3, False)):
            page.treat_button.click()
        self.assertEqual(heard, [])

    def test_a_finished_treatment_is_offered_for_its_own_frame_only(self):
        self.page()
        self.register(self.report_file(NAMES[0]))
        offered = self.workspace.treatments(NAMES[0])
        mine = [item for item in offered if item["kind"] == "treatment"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["name"], "Dusk, held back")
        self.assertIn("round 1 of 1", mine[0]["intent"])
        others = self.workspace.treatments(NAMES[1])
        self.assertEqual(
            [item for item in others if item["kind"] == "treatment"], [])

    def test_the_offered_recipe_is_what_the_treatment_settled_on(self):
        self.page()
        self.register(self.report_file(NAMES[0]))
        entry = self.workspace.treatments(NAMES[0])
        chosen = next(item for item in entry if item["kind"] == "treatment")
        recipe = self.workspace.compiled_recipe(
            NAMES[0], chosen["id"], "darktable")
        self.assertEqual(
            [(item["op"], item["value"]) for item in recipe["operations"]],
            [("tone.exposure", -0.5)])
        self.assertEqual(recipe["source_photo"], NAMES[0])

    def test_a_report_whose_file_has_gone_is_not_offered(self):
        self.page()
        path = self.report_file(NAMES[0])
        self.register(path)
        path.unlink()
        offered = self.workspace.treatments(NAMES[0])
        self.assertEqual(
            [item for item in offered if item["kind"] == "treatment"], [])


class TreatmentRoundsTests(unittest.TestCase):
    """Every round choosable, and a refusal said rather than hidden.

    An abstention left nothing on the develop page at all: the rounds
    -- rendered, measured, paid for -- were only findable by someone
    reading .darkimiya/Treatments by hand, and the failure information
    lived in a text file nobody was told about.
    """

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

    def run_directory(self, photo: str, rounds: int = 2,
                      stamp: str = "20260814T154021Z") -> Path:
        run = (self.photos_path / ".darkimiya" / "Treatments"
               / f"{Path(photo).stem}.protect-then-reveal.{stamp}")
        run.mkdir(parents=True, exist_ok=True)
        for n in range(1, rounds + 1):
            render = run / f"round-{n}.jpg"
            Image.new("RGB", (60, 40), (30 * n, 20, 60)).save(render)
            (run / f"round-{n}.json").write_text(json.dumps({
                "round": n, "render": str(render),
                "measurements": {"subject_separation": 10.0 + n,
                                 "veil_percent": 30.0 - n},
                "critique": {"regressed": f"the veil rose in round {n}",
                             "finished": False},
                "recipe": {"format": "opencull-development-recipe-v1",
                           "title": f"attempt {n}",
                           "operations": [{"op": "tone.exposure",
                                           "value": -0.1 * n, "unit": "EV",
                                           "mode": "delta",
                                           "source": f"exposure {-0.1 * n}"}]},
            }))
        return run

    def page(self):
        from opencull_qt.develop import DevelopPage, workspace_for
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.photos)
        self.workspace = workspace_for(
            self.report, self.photos.root, decoders=set())
        page = DevelopPage(self.report, self.workspace, loader)
        page.resize(900, 600)
        page.show()
        self.addCleanup(page.shutdown)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def test_an_unwarranted_runs_rounds_are_still_offered(self):
        self.run_directory(NAMES[0])
        self.page()
        rounds = [item for item in self.workspace.treatments(NAMES[0])
                  if item["kind"] == "round"]
        self.assertEqual([item["name"] for item in rounds],
                         ["Round 1", "Round 2"])
        self.assertFalse(rounds[0]["warranted"])
        self.assertIn("declined to vouch", rounds[0]["run_status"])

    def test_each_round_carries_its_numbers_and_its_own_fault(self):
        self.run_directory(NAMES[0])
        self.page()
        rounds = [item for item in self.workspace.treatments(NAMES[0])
                  if item["kind"] == "round"]
        self.assertIn("separation 11.0", rounds[0]["intent"])
        self.assertIn("the veil rose in round 1", rounds[0]["intent"])
        self.assertIn("Not warranted", rounds[0]["intent"])
        # The full story -- including why the panel declined -- rides
        # the tooltip's text, not the intent panel.
        self.assertIn("declined to vouch", rounds[0]["story"])

    def test_choosing_a_round_renders_that_rounds_recipe(self):
        self.run_directory(NAMES[0])
        self.page()
        rounds = [item for item in self.workspace.treatments(NAMES[0])
                  if item["kind"] == "round"]
        recipe = self.workspace.compiled_recipe(
            NAMES[0], rounds[1]["id"], "darktable")
        self.assertEqual(recipe["operations"][0]["value"], -0.2)
        self.assertEqual(recipe["source_photo"], NAMES[0])

    def test_the_heading_names_the_refusal_and_folds_the_rounds(self):
        self.run_directory(NAMES[0])
        page = self.page()
        from opencull_qt.develop import ROUNDS_ROW

        for row in range(page.treatments.count()):
            item = page.treatments.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == ROUNDS_ROW:
                break
        else:
            self.fail("no rounds heading in the list")
        self.assertIn("not warranted", item.text())
        self.assertIn("TREATMENT ROUNDS · 2", item.text())
        before = page.treatments.count()
        page._clicked_treatment(item)
        self.assertEqual(page.treatments.count(), before + 2)

    def test_a_rounds_thumbnail_is_its_own_proof_not_a_new_render(self):
        self.run_directory(NAMES[0])
        page = self.page()
        rounds = [item for item in self.workspace.treatments(NAMES[0])
                  if item["kind"] == "round"]
        page.rounds_open = True
        page._fill_treatments()
        self.assertIn((NAMES[0], rounds[0]["id"]), page.previews)

    def test_only_the_newest_run_is_listed(self):
        self.run_directory(NAMES[0], rounds=1, stamp="20260814T100000Z")
        self.run_directory(NAMES[0], rounds=2, stamp="20260814T154021Z")
        self.page()
        rounds = [item for item in self.workspace.treatments(NAMES[0])
                  if item["kind"] == "round"]
        self.assertEqual(len(rounds), 2)
        self.assertIn("154021z", rounds[0]["id"])

    def test_a_run_with_nothing_rendered_shows_no_heading(self):
        run = (self.photos_path / ".darkimiya" / "Treatments"
               / f"{Path(NAMES[0]).stem}.protect-then-reveal.20260814T000001Z")
        run.mkdir(parents=True)
        (run / "plan-1.json").write_text("{}")
        self.page()
        self.assertEqual(
            [item for item in self.workspace.treatments(NAMES[0])
             if item["kind"] == "round"], [])


class FinetuneDoorTests(unittest.TestCase):
    """The primary action goes deeper, not sideways.

    Clicking a treatment already develops it into the comparison, so
    "Develop this frame" was a primary-coloured button that did nothing
    new. Its place goes to the door into the treatment's own controls.
    """

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

    def page(self):
        from opencull_qt.develop import DevelopPage, workspace_for
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.photos)
        page = DevelopPage(
            self.report,
            workspace_for(self.report, self.photos.root, decoders=set()),
            loader)
        page.resize(900, 600)
        page.show()
        self.addCleanup(page.shutdown)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def test_the_primary_button_opens_the_controls(self):
        page = self.page()
        heard = []
        page.finetune_wanted.connect(
            lambda photo, treatment: heard.append((photo, treatment)))
        self.assertEqual(page.finetune_button.text(), "Advanced fine-tune…")
        page.finetune_button.click()
        self.assertEqual(heard, [(page.current, page.treatment)])

    def test_there_is_no_develop_this_frame_button_left(self):
        page = self.page()
        self.assertFalse(hasattr(page, "develop_button"))

    def test_clicking_a_treatment_still_develops_it(self):
        """The behaviour the button duplicated survives it."""
        page = self.page()
        asked = []
        page.renderer.render = lambda *a, **k: asked.append(a)
        page.develop_current(arriving=True)
        self.assertEqual(len(asked), 1)


class TimelapseDoorTests(FinetuneDoorTests):
    """One button's worth of timelapse: the process fills the geeky parts."""

    def test_the_defaults_come_from_the_folder_itself(self):
        from timelapse_kernel import default_run

        told = default_run(str(self.photos_path))
        self.assertEqual(told["pattern"], "*.JPG")
        self.assertTrue(told["frames_dir"].endswith(
            ".darkimiya/Timelapse/frames"))
        self.assertTrue(told["output"].endswith(
            ".darkimiya/Timelapse/timelapse.json"))

    def test_the_sun_choice_asks_nothing_and_fills_everything(self):
        from opencull_qt.timelapse import TimelapseDialog

        dialog = TimelapseDialog(str(self.photos_path))
        request = dialog.run_request()
        self.assertEqual(request["program"], "eclipse_timelapse.kim")
        told = request["parameters"]
        self.assertEqual(told["photos"], str(self.photos_path))
        self.assertEqual(told["pattern"], "*.JPG")
        self.assertTrue(told["frames_dir"].endswith("/frames"))
        self.assertTrue(told["output"].endswith("/timelapse.json"))
        self.assertEqual(told["recipe"], "")

    def test_a_marked_subject_requires_its_four_numbers(self):
        from opencull_qt.timelapse import TimelapseDialog

        dialog = TimelapseDialog(str(self.photos_path))
        dialog.marked.setChecked(True)
        self.assertIsNone(dialog.run_request())
        self.assertIn("four numbers", dialog.status.text())
        dialog.box_field.setText("100, 90, 148, 138")
        request = dialog.run_request()
        self.assertEqual(request["program"], "subject_timelapse.kim")
        self.assertEqual(request["parameters"]["subject_box"],
                         "100,90,148,138")

    def test_a_named_subject_requires_its_name(self):
        from opencull_qt.timelapse import TimelapseDialog

        dialog = TimelapseDialog(str(self.photos_path))
        dialog.named.setChecked(True)
        self.assertIsNone(dialog.run_request())
        dialog.name_field.setText("the red kite")
        request = dialog.run_request()
        self.assertEqual(request["program"],
                         "named_subject_timelapse.kim")
        self.assertEqual(request["parameters"]["subject"], "the red kite")
        self.assertEqual(request["parameters"]["every"], "30")

    def test_the_look_speaks_preset_names_the_kernel_resolves(self):
        from opencull_qt.timelapse import TimelapseDialog

        dialog = TimelapseDialog(str(self.photos_path))
        names = [dialog.look.itemData(i)
                 for i in range(dialog.look.count())]
        self.assertIn("infrared-720-false-colour", names)
        dialog.look.setCurrentIndex(
            names.index("infrared-720-false-colour"))
        request = dialog.run_request()
        self.assertEqual(request["parameters"]["recipe"],
                         "infrared-720-false-colour")

    def test_the_look_opens_on_the_treatment_carried_from_development(self):
        # A preset chosen on the develop page arrives as its short id, and
        # the dialog opens with that look already selected rather than on
        # "As shot", so the timelapse wears the treatment being looked at.
        from opencull_qt.timelapse import TimelapseDialog

        dialog = TimelapseDialog(str(self.photos_path), look="infrared-850-mono")
        self.assertEqual(dialog.look.currentData(), "infrared-850-mono")
        self.assertEqual(dialog.run_request()["parameters"]["recipe"],
                         "infrared-850-mono")

    def test_an_unknown_look_falls_back_to_no_colour_change(self):
        from opencull_qt.timelapse import TimelapseDialog

        dialog = TimelapseDialog(str(self.photos_path), look="not-a-preset")
        self.assertEqual(dialog.look.currentData(), "")

    def test_the_button_emits_the_program_and_its_parameters(self):
        page = self.page()
        heard = []
        page.program_wanted.connect(
            lambda name, parameters: heard.append((name, parameters)))

        class Stub:
            DialogCode = type("D", (), {"Accepted": 1})

            def __init__(self, photos, parent=None, look=""):
                self.photos = photos
                self.look = look

            def adjustSize(self):
                pass

            def rect(self):
                from PySide6.QtCore import QRect

                return QRect(0, 0, 0, 0)

            def move(self, *args):
                pass

            def exec(self):
                return 1

            def run_request(self):
                return {"program": "eclipse_timelapse.kim",
                        "parameters": {"photos": self.photos}}

        import opencull_qt.timelapse as timelapse_module
        with unittest.mock.patch.object(
                timelapse_module, "TimelapseDialog", Stub):
            page.timelapse_current()
        self.assertEqual(heard[0][0], "eclipse_timelapse.kim")


class TreatmentMarkerTests(unittest.TestCase):
    """The page says a frame is being treated, and notices the arrival."""

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

    def page(self):
        from opencull_qt.develop import DevelopPage, workspace_for
        from opencull_qt.previews import PreviewLoader

        loader = PreviewLoader(self.photos)
        self.workspace = workspace_for(
            self.report, self.photos.root, decoders=set())
        page = DevelopPage(self.report, self.workspace, loader)
        page.resize(900, 600)
        page.show()
        self.addCleanup(page.shutdown)
        self.addCleanup(loader.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def job(self, photo, status):
        return {"kind": "treatment", "photo": photo, "status": status}

    def test_the_button_sits_under_the_list_it_feeds(self):
        page = self.page()
        column = page.treat_button.parentWidget().layout()
        positions = {column.itemAt(i).widget(): i
                     for i in range(column.count())
                     if column.itemAt(i).widget() is not None}
        self.assertEqual(positions[page.treat_button],
                         positions[page.treatments] + 1)
        self.assertLess(positions[page.treat_button],
                        positions[page.finetune_button])

    def test_a_running_treatment_marks_the_frame_and_holds_the_button(self):
        page = self.page()
        page.treatment_jobs([self.job(page.current, "running")])
        self.assertFalse(page.treat_button.isEnabled())
        self.assertTrue(page.treating.isVisible())
        self.assertIn("Treating this frame now", page.treating.text())
        self.assertTrue(page.treating_bar.isVisible())

    def test_the_marker_says_which_round_and_what_it_is_doing(self):
        page = self.page()
        job = self.job(page.current, "running")
        job["rounds"] = 3
        job["progress"] = {"completed_items": 1,
                           "stage": "round 2: planning and rendering"}
        page.treatment_jobs([job])
        self.assertIn("round 2: planning and rendering",
                      page.treating.text())
        self.assertIn("1 of 3 rounds rendered", page.treating.text())

    def test_a_queued_treatment_shows_no_busy_bar(self):
        page = self.page()
        page.treatment_jobs([self.job(page.current, "queued")])
        self.assertFalse(page.treating_bar.isVisible())

    def test_an_abstention_ends_the_bar_and_says_how_it_ended(self):
        """The bar finished and the page went silent; the outcome had
        gone to the queue page, where nobody was looking."""
        page = self.page()
        page.treatment_jobs([self.job(page.current, "running")])
        declined = self.job(page.current, "failed")
        declined["message"] = "The panel declined to vouch for the treatment."
        page.treatment_jobs([declined])
        self.assertTrue(page.treat_button.isEnabled())
        self.assertFalse(page.treating_bar.isVisible())
        self.assertTrue(page.treating.isVisible())
        self.assertIn("panel declined", page.treating.text())

    def test_a_warranted_run_says_it_joined_the_list(self):
        page = self.page()
        page.treatment_jobs([self.job(page.current, "running")])
        page.treatment_jobs([self.job(page.current, "completed")])
        self.assertIn("joined the list above", page.treating.text())
        self.assertTrue(page.treating.isVisible())

    def test_the_outcome_note_clears_when_the_frame_is_asked_again(self):
        page = self.page()
        page.treatment_jobs([self.job(page.current, "running")])
        page.treatment_jobs([self.job(page.current, "failed")])
        with unittest.mock.patch(
                "opencull_qt.develop.QInputDialog.getInt",
                return_value=(3, True)):
            page.treat_button.click()
        self.assertNotIn(page.current, page._treatment_outcome)

    def test_a_run_nobody_watched_start_is_not_an_outcome(self):
        """Only a transition this page saw becomes a note; history from
        before the page opened stays on the queue page."""
        page = self.page()
        page.treatment_jobs([self.job(page.current, "failed")])
        self.assertFalse(page.treating.isVisible())

    def test_a_queued_treatment_says_it_is_waiting(self):
        page = self.page()
        page.treatment_jobs([self.job(page.current, "queued")])
        self.assertIn("waiting in the queue", page.treating.text())

    def test_someone_elses_treatment_does_not_hold_this_frames_button(self):
        page = self.page()
        page.treatment_jobs([self.job(NAMES[2], "running")])
        self.assertTrue(page.treat_button.isEnabled())
        self.assertFalse(page.treating.isVisible())

    def test_the_moment_a_run_finishes_the_list_is_rebuilt(self):
        page = self.page()
        page.treatment_jobs([self.job(page.current, "running")])
        heard = []
        page._fill_treatments = lambda: heard.append(True)
        page.treatment_jobs([self.job(page.current, "completed")])
        self.assertEqual(heard, [True])
        self.assertTrue(page.treat_button.isEnabled())
        # The label stays, carrying the outcome; only the bar goes.
        self.assertFalse(page.treating_bar.isVisible())

    def test_a_run_that_was_already_over_is_not_an_event(self):
        page = self.page()
        heard = []
        page._fill_treatments = lambda: heard.append(True)
        page.treatment_jobs([self.job(page.current, "completed")])
        self.assertEqual(heard, [])


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
        # Tooltips are set in a column, so the sentence a test is looking
        # for may fall across two lines of it.
        self.assertIn("no claim to check",
                      " ".join(page.verify_button.toolTip().split()))

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

    def rows_with_a_treatment(self, page):
        """The rows that stand for something renderable.

        The presets heading is a row too, and it has no treatment behind
        it -- it is the control that folds the ones below it away.
        """
        from opencull_qt.develop import PRESETS_ROW

        return [page.treatments.item(row)
                for row in range(page.treatments.count())
                if page.treatments.item(row).data(
                    Qt.ItemDataRole.UserRole) != PRESETS_ROW]

    def test_every_treatment_shows_what_it_does_to_this_frame(self):
        page = self.suggested_page(marked=(NAMES[0],))
        rows = self.rows_with_a_treatment(page)
        self.assertGreater(len(rows), 1)
        self.assertTrue(
            self.wait_for(lambda: all(
                not item.icon().isNull()
                for item in self.rows_with_a_treatment(page))),
            "the treatments were never given their previews")
        # Rendered once and kept: coming back to a frame costs nothing.
        self.assertEqual(len(page.previews), len(rows))
        before = dict(page.previews)
        page._request_previews()
        self.assertEqual(page.previews, before)

    def test_no_tooltip_is_drawn_wider_than_the_window(self):
        """A tooltip is a paragraph, and a paragraph needs a column.

        Qt sets plain text on one line however long the sentence is, so a
        treatment's stated intent -- three hundred characters of it --
        was drawn as a single strip wider than the window it belonged to.
        """
        from PySide6.QtGui import QFontMetrics
        from PySide6.QtWidgets import QToolTip, QWidget

        page = self.suggested_page(marked=(NAMES[0],))
        # An intent the length a model actually writes. The fixture's own
        # are a handful of words, which would fit however they were set
        # and would let this test pass over the defect it is here for.
        written = page.workspace.treatments
        page.workspace.treatments = lambda photo: [
            {**item, "intent": item["intent"] and (
                "A professionally refined adaptation of the learned "
                "Saturated Coastal Monumentalism profile: use its bold blue "
                "atmosphere, luminous cloud structure, selective green "
                "intensity, and crisp environmental separation for this "
                "lake-and-mountain overlook, while avoiding its "
                "inappropriate cliff-and-surf exaggeration.")}
            for item in written(photo)]
        page._fill_treatments()

        metrics = QFontMetrics(QToolTip.font())
        tips = [
            (widget.__class__.__name__, widget.toolTip())
            for widget in page.findChildren(QWidget) if widget.toolTip()]
        tips += [("treatment", page.treatments.item(row).toolTip())
                 for row in range(page.treatments.count())]
        self.assertGreater(len(tips), 5, "no tooltips were found to measure")
        self.assertGreater(
            max(len(text) for _where, text in tips), 300,
            "the longest tooltip must be long enough to overflow unwrapped")
        for where, text in tips:
            widest = max(metrics.horizontalAdvance(line)
                         for line in text.split("\n"))
            self.assertLessEqual(
                widest, page.width(),
                f"{where} is drawn {widest}px wide in a {page.width()}px "
                f"window: {text.splitlines()[0][:60]}...")

    def test_a_treatment_tooltip_keeps_its_name_on_the_first_line(self):
        page = self.suggested_page(marked=(NAMES[0],))
        named = [item.toolTip().split("\n")[0]
                 for item in self.rows_with_a_treatment(page)]
        self.assertEqual(
            named, [item["name"] for item in written(page.available)])

    # --- presets ---------------------------------------------------------

    def preset_rows(self, page):
        from opencull_qt.develop import PRESETS_ROW

        return [page.treatments.item(row)
                for row in range(page.treatments.count())
                if str(page.treatments.item(row).data(
                    Qt.ItemDataRole.UserRole)).startswith(("preset-", "saved-"))
                and page.treatments.item(row).data(
                    Qt.ItemDataRole.UserRole) != PRESETS_ROW]

    def heading_row(self, page):
        from opencull_qt.develop import PRESETS_ROW

        return next(
            (page.treatments.item(row)
             for row in range(page.treatments.count())
             if page.treatments.item(row).data(
                 Qt.ItemDataRole.UserRole) == PRESETS_ROW), None)

    def test_presets_are_offered_on_a_folder_nobody_has_assessed(self):
        """They belong to the photographer, not to the assessment."""
        page = self.page()
        offered = [item for item in page.available
                   if item.get("kind") == "preset"]
        self.assertGreater(len(offered), 4)

    def test_presets_are_folded_away_until_they_are_asked_for(self):
        page = self.page()
        self.assertEqual(self.preset_rows(page), [])
        heading = self.heading_row(page)
        self.assertIsNotNone(heading)
        self.assertIn("PRESETS", heading.text())

    def test_clicking_the_heading_opens_and_shuts_the_presets(self):
        page = self.page()
        page._clicked_treatment(self.heading_row(page))
        self.assertEqual(
            len(self.preset_rows(page)),
            len([item for item in page.available
                 if item.get("kind") == "preset"]))
        page._clicked_treatment(self.heading_row(page))
        self.assertEqual(self.preset_rows(page), [])

    def test_the_heading_cannot_become_the_treatment_that_renders(self):
        page = self.page()
        before = page.treatment
        heading = self.heading_row(page)
        page.treatments.setCurrentItem(heading)
        self.assertEqual(page.treatment, before)
        self.assertNotEqual(page.treatment, "")

    def test_the_list_grows_only_by_what_it_shows(self):
        page = self.page()
        shut = page.treatments.height()
        page.toggle_presets()
        self.assertGreater(page.treatments.height(), shut)
        page.toggle_presets()
        self.assertEqual(page.treatments.height(), shut)

    def test_a_taller_window_shows_more_treatments(self):
        """The panel's spare room belongs to the list, not to nothing.

        A fixed ceiling of five tiles is most of a laptop panel and a
        third of a tall one: the same list either crowded the frame or
        scrolled with half the panel empty beneath it.
        """
        page = self.page()
        page.show()
        page.toggle_presets()
        heights = []
        for height in (700, 1000, 1400):
            page.resize(1280, height)
            self.application.processEvents()
            heights.append(page.treatments.height())
        self.assertEqual(heights, sorted(heights))
        self.assertGreater(heights[-1], heights[0] + 200)

    def test_it_never_grows_past_what_it_has_to_show(self):
        """Room to spare is not a reason to draw an empty list."""
        page = self.page()          # baseline and as-shot, presets folded
        page.resize(1280, 1400)
        page.show()
        self.application.processEvents()
        rows = sum(page.treatments.item(row).sizeHint().height()
                   for row in range(page.treatments.count()))
        self.assertLessEqual(page.treatments.height(), rows + 10)

    def test_a_short_window_still_leaves_the_primary_button_reachable(self):
        page = self.page()
        page.resize(1280, 620)
        page.show()
        page.toggle_presets()
        self.application.processEvents()
        self.assertLess(page.treatments.height(), page.height())
        self.assertGreater(
            page.finetune_button.visibleRegion().boundingRect().height(), 0,
            "the develop button was pushed out of a short window")

    def test_opening_the_presets_does_not_push_the_page_off_the_window(self):
        """Thirteen tiles is taller than the window they are shown in.

        Below the list is the button that develops what has been chosen.
        A list that grows without limit takes it off the bottom of the
        screen, and the photographer can select a preset and not reach
        the control that renders it.
        """
        page = self.page()
        page.resize(1200, 820)
        page.show()
        page.toggle_presets()
        self.application.processEvents()
        self.assertLess(page.treatments.height(), page.height())
        self.assertGreater(
            page.finetune_button.visibleRegion().boundingRect().height(), 0,
            "the develop button was pushed out of the window")

    def test_no_preset_is_rendered_before_anybody_asks_for_one(self):
        """Nine looks across a selection is the sweep several times over."""
        page = self.suggested_page(marked=(NAMES[0], NAMES[1]))
        page.toggle_presets()
        asked = {treatment for _photo, treatment in page.thumbs._asked}
        self.assertTrue(asked, "nothing was queued at all")
        self.assertEqual(
            [item for item in asked if item.startswith("preset-")], [])

    def test_choosing_a_preset_asks_for_its_picture(self):
        page = self.page()
        page.toggle_presets()
        chosen = self.preset_rows(page)[0]
        page.treatments.setCurrentItem(chosen)
        wanted = chosen.data(Qt.ItemDataRole.UserRole)
        self.assertEqual(page.treatment, wanted)
        self.assertIn((page.current, wanted), page.thumbs._asked)

    def test_a_preset_renders_and_is_not_the_baseline(self):
        page = self.page()
        page.toggle_presets()
        page.treatments.setCurrentItem(self.preset_rows(page)[0])
        page.develop_current()
        self.assertTrue(
            self.wait_for(lambda: (page.current, page.treatment)
                          in page.rendered),
            "the preset never rendered")
        treated = page.rendered[(page.current, page.treatment)]
        base = self.workspace_of(page).recipe_preview(
            page.current, "calibrated", "default", "markesteijn-3-pass",
            treated.width())
        self.assertTrue(base.is_file())

    def test_a_preset_makes_no_claim_so_nothing_verifies_it(self):
        page = self.suggested_page(marked=(NAMES[0],))
        page.toggle_presets()
        page.treatments.setCurrentItem(self.preset_rows(page)[0])
        self.assertEqual(page.suggestion(), "")
        self.assertFalse(page.verify_button.isEnabled())

    def test_a_preset_the_photographer_kept_is_offered_everywhere(self):
        from unittest import mock

        from opencull_gui import presets

        # Its own folder: a preset saved here must not still be in the
        # list when the next test asks what is offered.
        mine = Path(self._temporary.name) / "my-presets"
        self.enterContext(mock.patch.dict(
            "os.environ", {presets.PRESETS_ENVIRONMENT: str(mine)}))
        presets.save("My own look", [{
            "op": "tone.contrast", "value": 9.0, "unit": "percent",
            "mode": "delta"}])
        page = self.page()
        page.toggle_presets()
        names = [item.text().strip() for item in self.preset_rows(page)]
        self.assertIn("My own look", names)
        # The photographer's own come first, before what shipped.
        self.assertEqual(names[0], "My own look")

    def workspace_of(self, page):
        return page.workspace

    def test_a_full_render_uses_the_raw_not_its_embedded_preview(self):
        """A full-size export must be the size of the photograph.

        The camera's embedded preview stood in for the frame's own
        dimensions. Fujifilm embeds a near-full-size JPEG so it was
        nearly right; Sony embeds 1616 pixels against a 4608-pixel
        sensor, and every full-size ARW export was a third of the frame.
        """
        from unittest import mock

        from opencull_gui.development import full_size

        raw = Path(self._temporary.name) / "DSC00001.ARW"
        raw.write_bytes(b"not really a raw")
        small = Path(self._temporary.name) / "preview.jpg"
        Image.new("RGB", (1616, 1080), (30, 30, 40)).save(small)

        class Sizes:
            width, height = 4608, 3072

        class Raw:
            sizes = Sizes()
            def __enter__(self): return self
            def __exit__(self, *_): return False

        with mock.patch.dict(
            "sys.modules", {"rawpy": mock.Mock(imread=lambda _p: Raw())}
        ):
            self.assertEqual(full_size(raw, small), 4608)

    def test_a_raw_that_cannot_be_read_falls_back_rather_than_failing(self):
        from unittest import mock

        from opencull_gui.development import full_size

        raw = Path(self._temporary.name) / "DSC00002.ARW"
        raw.write_bytes(b"damaged")
        small = Path(self._temporary.name) / "preview2.jpg"
        Image.new("RGB", (1616, 1080), (30, 30, 40)).save(small)
        with mock.patch.dict(
            "sys.modules",
            {"rawpy": mock.Mock(imread=mock.Mock(side_effect=OSError))}
        ):
            self.assertEqual(full_size(raw, small), 1616)

    def test_an_ordinary_photograph_is_its_own_size(self):
        from opencull_gui.development import full_size

        path = Path(self._temporary.name) / "B.JPG"
        Image.new("RGB", (900, 600), (60, 60, 60)).save(path)
        self.assertEqual(full_size(path, path), 900)

    # --- infrared --------------------------------------------------------

    def test_an_album_is_ordinary_light_until_somebody_says_otherwise(self):
        page = self.page()
        self.assertFalse(page.workspace.infrared())
        self.assertFalse(page.infrared.isChecked())

    def test_marking_the_album_infrared_is_remembered(self):
        page = self.page()
        page.infrared.setChecked(True)
        self.assertTrue(page.workspace.infrared())
        # Read back from disk rather than from the object that set it.
        from opencull_gui.project import load_project
        stored = load_project(page.workspace.project_path)
        self.assertEqual(stored["rendering"]["spectrum"], "infrared")

    def test_an_infrared_frame_is_not_matched_to_the_camera(self):
        """The whole point: the camera's guess must not be copied.

        Past an infrared filter the camera has no idea what it is looking
        at, and the baseline's job -- reproduce the picture the camera
        made -- becomes the one thing worth refusing. Asked of the
        workspace rather than of the page, because the page also renders
        previews in the background and they would land in the middle of
        the count.
        """
        from unittest import mock

        import opencull_gui.development as development

        assess_and_suggest(
            Path(self._temporary.name), self.report_path, self.photos_path)
        from opencull_qt.develop import workspace_for
        workspace = workspace_for(self.report, self.photos.root, decoders=set())

        seen = []
        real_render = development.render_recipe

        def watching(*args, **kwargs):
            seen.append(kwargs.get("reference_jpeg"))
            return real_render(*args, **kwargs)

        with mock.patch.object(development, "render_recipe", watching):
            workspace.recipe_preview(
                NAMES[0], "standard", "default", "markesteijn-3-pass", 64)
            workspace.set_spectrum("infrared")
            workspace.recipe_preview(
                NAMES[0], "standard", "default", "markesteijn-3-pass", 64)
        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(seen[0], "ordinary light should match the camera")
        self.assertIsNone(seen[1], "infrared should not match the camera")

    def test_the_proofs_from_before_are_not_served_afterwards(self):
        """A proof made from the other base is a proof of something else.

        Leaving the spectrum out of the identity is how three earlier
        renderer fixes stayed invisible behind cached files.
        """
        page = self.suggested_page(marked=(NAMES[0],))
        before = page.workspace.recipe_preview(
            NAMES[0], "standard", "default", "markesteijn-3-pass", 64)
        page.infrared.setChecked(True)
        after = page.workspace.recipe_preview(
            NAMES[0], "standard", "default", "markesteijn-3-pass", 64)
        self.assertNotEqual(before.name, after.name)
        # And the earlier one is still there: changing your mind back
        # costs nothing.
        self.assertTrue(before.is_file())

    def test_turning_it_on_drops_the_pictures_made_from_the_old_base(self):
        page = self.suggested_page(marked=(NAMES[0],))
        self.assertTrue(
            self.wait_for(lambda: bool(page.previews)),
            "no previews were made to begin with")
        page.infrared.setChecked(True)
        self.assertEqual(page.previews, {})
        self.assertEqual(page.rendered, {})

    def test_the_infrared_presets_are_offered_like_any_other(self):
        page = self.page()
        offered = [item["id"] for item in page.available]
        for name in ("760-mono", "850-mono", "720-false-colour", "720-mono"):
            self.assertIn(f"preset-infrared-{name}", offered)

    def test_previews_are_rendered_for_every_treated_frame_not_only_one(self):
        """The twentieth frame should not be rendered while you look at it."""
        page = self.suggested_page(marked=(NAMES[0], NAMES[1]))
        listed = page.shown_photos()
        self.assertEqual(len(listed), 2)
        self.assertTrue(
            self.wait_for(lambda: all(
                (photo, "standard") in page.previews for photo in listed)),
            "the frames behind the open one never got their previews")
        # And the open frame is served first, whatever the list order.
        self.assertIn((page.current, "standard"), page.previews)

    def test_a_treatment_arrives_over_the_frame_it_was_made_from(self):
        from PySide6.QtGui import QPixmap

        from opencull_qt.develop import Stage

        stage = Stage()
        self.addCleanup(stage.deleteLater)
        stage.resize(400, 300)
        shot = QPixmap(200, 150)
        shot.fill(Qt.GlobalColor.darkGreen)
        treated = QPixmap(200, 150)
        treated.fill(Qt.GlobalColor.darkBlue)
        stage.set_as_shot(shot)

        stage.set_treated(treated, "Bold", arriving=True)
        self.assertIsNotNone(stage.image._sweep)
        # Mid-wipe both pictures are on screen; the caption already reads
        # as the treatment, because that is what is arriving.
        stage.image._sweep_tick(0.5)
        self.assertEqual(stage.caption.text(), "BOLD")
        self.assertTrue(self.wait_for(lambda: stage.image._sweep is None))
        self.assertIs(stage.image.source(), treated)

    def test_a_treatment_restored_without_asking_does_not_animate(self):
        from PySide6.QtGui import QPixmap

        from opencull_qt.develop import Stage

        stage = Stage()
        self.addCleanup(stage.deleteLater)
        stage.resize(400, 300)
        shot = QPixmap(200, 150)
        shot.fill(Qt.GlobalColor.darkGreen)
        stage.set_as_shot(shot)
        treated = QPixmap(200, 150)
        treated.fill(Qt.GlobalColor.darkBlue)
        stage.set_treated(treated, "Bold")
        self.assertIsNone(stage.image._sweep)

    def test_selecting_a_preview_does_not_repaint_the_photograph(self):
        """A highlight over a photograph is a colour the photograph is not."""
        from PySide6.QtGui import QColor, QIcon, QPixmap

        from opencull_qt.previews import plain_icon

        picture = QPixmap(40, 30)
        picture.fill(QColor(120, 160, 90))
        icon = plain_icon(picture)
        wash = QIcon(picture)

        def hue(pixmap):
            colour = pixmap.toImage().pixelColor(20, 15)
            return (colour.red(), colour.green(), colour.blue())

        plain = hue(icon.pixmap(40, 30, QIcon.Mode.Normal))
        chosen = hue(icon.pixmap(40, 30, QIcon.Mode.Selected))
        self.assertEqual(plain, chosen)
        # And that this is worth guarding: Qt's own icon does tint.
        self.assertNotEqual(
            hue(wash.pixmap(40, 30, QIcon.Mode.Normal)),
            hue(wash.pixmap(40, 30, QIcon.Mode.Selected)))

    def test_clicking_a_preview_asks_for_it_at_proof_size(self):
        page = self.suggested_page(marked=(NAMES[0],))
        asked = []
        page.develop_current = lambda arriving=False: asked.append(arriving)
        page.treatments.itemClicked.emit(page.treatments.item(1))
        # And it arrives as an animation over the frame as shot, which is
        # what makes it legible as an edit rather than a swap.
        self.assertEqual(asked, [True])

    def test_moving_between_frames_does_not_ask_for_a_proof(self):
        # Only a click does. Selecting a frame must not spend a full
        # render nobody asked for.
        page = self.suggested_page(marked=(NAMES[0], NAMES[1]))
        asked = []
        page.renderer.render = lambda *a, **k: asked.append(a)
        page.show_photo(NAMES[1])
        self.assertEqual(asked, [])

    def test_an_export_says_how_far_through_the_batch_it_is(self):
        """A full-size render is ninety seconds; a still button is not news."""
        page = self.page()
        self.assertFalse(page.delivery_meter.isVisibleTo(page))

        page.exporter.pending = 2
        page._delivery_progress(0, 2)
        self.assertTrue(page.delivery_meter.isVisibleTo(page))
        self.assertIn("1 of 2", page.delivery_note.text())
        self.assertEqual(page.delivery_meter.value(), 0)

        page.exporter.pending = 1
        page._delivery_progress(1, 2)
        self.assertIn("2 of 2", page.delivery_note.text())
        self.assertEqual(page.delivery_meter.value(), 50)

        page.exporter.pending = 0
        page._delivery_progress(2, 2)
        self.assertFalse(page.delivery_meter.isVisibleTo(page))

    def test_the_bar_moves_through_the_adjustments_of_one_photograph(self):
        """Twenty adjustments is twenty things to say, not one long wait."""
        page = self.page()
        page.exporter.pending = 1
        page.exporter.asked = 1

        page._delivery_step("A.JPG", 0, 1, "developing the raw")
        self.assertIn("developing the raw", page.delivery_note.text())

        page._delivery_step("A.JPG", 5, 20, "tone.contrast")
        self.assertEqual(page.delivery_meter.value(), 25)
        self.assertIn("contrast", page.delivery_note.text())

        page._delivery_step("A.JPG", 19, 20, "writing the photograph")
        self.assertEqual(page.delivery_meter.value(), 95)
        self.assertIn("writing the photograph", page.delivery_note.text())

    def test_within_a_batch_the_step_says_which_photograph(self):
        page = self.page()
        page.exporter.pending = 2
        page.exporter.asked = 3
        page._delivery_step("B.JPG", 4, 20, "color.hsl_range:blue/teal:saturation")
        said = page.delivery_note.text()
        self.assertIn("B.JPG", said)
        self.assertIn("2 of 3", said)
        # The colour family is the part worth reading, not the index.
        self.assertIn("blue and teal saturation", said)
        self.assertNotIn("adjustment 4", said)

    def test_every_adjustment_has_a_name_a_photographer_would_use(self):
        from opencull_qt.develop import ADJUSTMENT_NAMES, _adjustment_name
        from recipe_compiler import RANGES

        for op in RANGES:
            said = _adjustment_name(op)
            self.assertNotIn("_", said, f"{op} reads as code")
            self.assertNotEqual(said, op, f"{op} has no plain name")
        self.assertEqual(_adjustment_name("mask:luma"), "a luminance mask")
        self.assertIn("detail.sharpen_amount", ADJUSTMENT_NAMES)

    def test_a_step_after_the_batch_is_finished_says_nothing(self):
        page = self.page()
        page.exporter.pending = 0
        page._delivery_step("A.JPG", 5, 20, "tone.contrast")
        self.assertFalse(page.delivery_meter.isVisibleTo(page))

    def test_the_exporter_counts_the_batch_not_the_backlog(self):
        page = self.page()
        seen = []
        page.exporter.progressed.connect(lambda d, t: seen.append((d, t)))
        page.exporter._pool.setMaxThreadCount(1)
        for index in range(3):
            page.exporter.asked += 1
            page.exporter.pending += 1
            page.exporter.progressed.emit(
                page.exporter.asked - page.exporter.pending,
                page.exporter.asked)
        self.assertEqual([total for _done, total in seen], [1, 2, 3])
        page.exporter.pending = 0
        page.exporter._settle()
        self.assertEqual(page.exporter.asked, 0, "the batch should reset")

    def test_the_camera_s_own_frame_can_be_delivered_unchanged(self):
        """"As shot" has to be the camera's picture, not a thumbnail of it."""
        page = self.page()
        offered = [item["id"] for item in page.available]
        self.assertIn("as-shot", offered)
        self.assertEqual(offered[-1], "as-shot",
                         "the reference belongs at the foot of the list")

        # It renders as a proof like anything else, so the page can show it.
        proof = page.workspace.recipe_preview(
            NAMES[0], "as-shot", "default", "markesteijn-3-pass", 200)
        self.assertTrue(Path(proof).is_file())

    def test_a_delivered_as_shot_frame_claims_no_operation(self):
        page = self.page()
        result = page.workspace.render_full(
            NAMES[0], "as-shot", "default", "markesteijn-3-pass")
        render = result["render"]
        self.assertEqual(render["variant"], "as-shot")
        self.assertEqual(render["adjustments"], [])
        self.assertEqual(render["recipe_revision"], 0)
        self.assertIn("No", render["notice"])
        # And it is registered, so what leaves can always be traced.
        self.assertTrue(render["sha256"])

    def test_a_culled_folder_offers_the_baseline_without_any_suggestion(self):
        # No shortlist, no edit directions, no provider call: the ordinary
        # state of a folder that has just been culled.
        page = self.page()
        self.assertEqual(
            [item["id"] for item in written(page.available)],
            ["calibrated", "as-shot"])
        self.assertTrue(page.finetune_button.isEnabled())

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
        self.assertEqual(page.stage.caption.text(), "CAMERA-MATCHED BASELINE")
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
        self.assertTrue(page.finetune_button.isEnabled())
        self.assertIn("unavailable", page.status.text())
        self.assertEqual(page.status.property("tone"), "alarm")


class RendererCoalescingTests(unittest.TestCase):
    """A slider drag is many requests but must not be many renders.

    Fine tuning re-renders on every slider tick. Queueing each request had
    a drag pile up dozens of full renders that ran serially and were all
    discarded as stale -- the preview sat frozen while the machine ground
    through them. The renderer now holds one waiting request, newest wins.
    """

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_a_burst_of_requests_runs_two_renders_and_ends_on_the_last(self):
        import time

        from opencull_qt.develop import Renderer

        rendered = []

        class Workspace:
            def recipe_preview(self, photo, style, engine, demosaic,
                               maximum, adjustments=None):
                time.sleep(0.05)
                rendered.append(adjustments)
                return Path("/nonexistent.jpg")   # unreadable: fails cleanly

        renderer = Renderer(Workspace())
        self.addCleanup(renderer.shutdown)
        outcomes = []
        renderer.failed.connect(lambda *a: outcomes.append(a))
        for tick in range(12):
            renderer.render("A.JPG", "standard", "auto", "half",
                            adjustments={"tone.exposure": {"value": tick}})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
                renderer._inflight or renderer._waiting):
            self.application.processEvents()
            time.sleep(0.01)
        self.application.processEvents()
        # The first request ran at once; the other eleven collapsed into
        # one waiting render carrying the newest value.
        self.assertEqual(len(rendered), 2)
        self.assertEqual(rendered[-1], {"tone.exposure": {"value": 11}})

    def test_abandon_clears_the_waiting_request(self):
        import time

        from opencull_qt.develop import Renderer

        rendered = []

        class Workspace:
            def recipe_preview(self, photo, style, engine, demosaic,
                               maximum, adjustments=None):
                time.sleep(0.05)
                rendered.append(photo)
                return Path("/nonexistent.jpg")

        renderer = Renderer(Workspace())
        self.addCleanup(renderer.shutdown)
        renderer.render("OLD.JPG", "standard", "auto", "half")
        renderer.render("OLD.JPG", "standard", "auto", "half")
        renderer.abandon()                    # switched to another frame
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and renderer._inflight:
            self.application.processEvents()
            time.sleep(0.01)
        self.application.processEvents()
        # The running render finished; the waiting one never started.
        self.assertEqual(rendered, ["OLD.JPG"])


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
