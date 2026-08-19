"""What the photographer knows and the model cannot see.

An assessment of eight partial-eclipse frames called the crescent a moon
and the light nocturnal, and then judged those photographs carefully and
about the wrong subject. Nothing was wrong with the looking. The subject
was a fact only the photographer had.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from edit_suggestion_kernel import edit_direction_prompt  # noqa: E402
from opencull_gui.project import (  # noqa: E402
    load_or_create_folder_project,
    load_project,
    update_project,
)
from shortlist_kernel import (  # noqa: E402
    build_professional_candidates,
    parse_professional_candidates,
    professional_assessment_prompt,
)

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

ECLIPSE = ("A partial solar eclipse through a 760nm infrared filter. "
           "The crescent is the sun, not the moon.")


class RememberedTests(unittest.TestCase):
    """The album keeps it, so nobody types it twice."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.photos = Path(self._temporary.name).resolve() / "photos"
        self.photos.mkdir(parents=True)
        self.manifest, _ = load_or_create_folder_project(self.photos, "Shoot")
        self.addCleanup(self._temporary.cleanup)

    def test_a_new_album_has_nothing_to_say(self):
        self.assertEqual(load_project(self.manifest)["about"], "")

    def test_what_is_written_is_read_back(self):
        update_project(self.manifest, about=ECLIPSE)
        self.assertEqual(load_project(self.manifest)["about"], ECLIPSE)

    def test_line_breaks_become_one_sentence(self):
        update_project(self.manifest, about="  a shoot\n\nof   two lines  ")
        self.assertEqual(load_project(self.manifest)["about"],
                         "a shoot of two lines")

    def test_an_essay_is_cut_rather_than_carried(self):
        update_project(self.manifest, about="word " * 400)
        self.assertLessEqual(len(load_project(self.manifest)["about"]), 600)

    def test_leaving_it_alone_does_not_erase_it(self):
        update_project(self.manifest, about=ECLIPSE)
        update_project(self.manifest, stage="assessment")
        self.assertEqual(load_project(self.manifest)["about"], ECLIPSE)


class ToldToTheModelsTests(unittest.TestCase):
    """Both models that read a photograph are told what it is of."""

    def test_the_assessor_is_told_in_the_photographers_words(self):
        prompt = professional_assessment_prompt(
            {"photo": "A.ARW", "about": ECLIPSE}, "landscape")
        self.assertIn(ECLIPSE, prompt)
        self.assertIn("WHAT THE PHOTOGRAPHER SAYS THIS SHOOT IS", prompt)

    def test_the_assessor_is_told_it_is_not_a_reason_to_rate_higher(self):
        """Context about the subject, not a thumb on the scale."""
        prompt = professional_assessment_prompt(
            {"photo": "A.ARW", "about": "the best photographs ever taken"},
            "landscape")
        self.assertIn("not a reason to rate it higher", prompt)

    def test_the_editing_model_is_told_too(self):
        prompt = edit_direction_prompt(
            {"photo": "A.ARW", "raw_files": ["x"], "about": ECLIPSE},
            "landscape")
        self.assertIn("crescent is the sun", prompt)
        self.assertIn("does not tell you what to do to it",
                      " ".join(prompt.split()))

    def test_saying_nothing_changes_neither_prompt(self):
        for prompt in (
            professional_assessment_prompt({"photo": "A.JPG"}, "family"),
            edit_direction_prompt({"photo": "A.JPG"}, "family"),
        ):
            self.assertNotIn("WHAT THE PHOTOGRAPHER SAYS", prompt)

    def test_whitespace_alone_is_saying_nothing(self):
        prompt = professional_assessment_prompt(
            {"photo": "A.JPG", "about": "   \n  "}, "family")
        self.assertNotIn("WHAT THE PHOTOGRAPHER SAYS", prompt)


class ReachesTheRunTests(unittest.TestCase):
    """From the album, through the bundle, into the prompt."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.photos = self.root / "photos"
        self.photos.mkdir(parents=True)
        Image.fromarray(
            np.full((600, 900, 3), 90, dtype=np.uint8)).save(
                self.photos / "A.JPG")
        self.report = self.root / "shoot-results.json"
        self.report.write_text(json.dumps({
            "format": "opencull-report-v2", "manifest_sha256": "x",
            "clusters": [{"cluster_id": "g1", "photos": ["A.JPG"]}],
            "keep": [{"cluster_id": "g1", "photos": ["A.JPG"],
                      "rationale": "kept", "confidence": 0.9, "warning": "",
                      "fallback": False, "photographic_assessment": []}],
            "warnings": [], "adaptive_clustering": {"enabled": False},
            "notice": "read only",
        }), encoding="utf-8")
        self.addCleanup(self._temporary.cleanup)

    def test_every_candidate_carries_it_and_the_prompt_says_it(self):
        bundle = build_professional_candidates(
            str(self.report), str(self.photos), "", "all", 100,
            "visible", 0, ECLIPSE)
        candidate = parse_professional_candidates(bundle)[0]
        self.assertEqual(candidate["about"], ECLIPSE)
        self.assertIn(
            "crescent is the sun",
            professional_assessment_prompt(candidate, "landscape"))


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class AskedForTests(unittest.TestCase):
    """There is somewhere to write it, before the run that needs it."""

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def invitation(self, asks=None):
        from opencull_qt.shell import Invitation

        page = Invitation(
            "This folder has not been assessed", "body", "Assess these 8",
            lambda: None, asks=asks)
        self.addCleanup(page.deleteLater)
        return page

    def test_a_phase_that_asks_nothing_reads_back_nothing(self):
        self.assertEqual(self.invitation().said(), "")

    def test_what_is_typed_is_what_comes_back(self):
        page = self.invitation(asks=("for instance", ""))
        page.asks.setPlainText("  a partial\n eclipse  ")
        self.assertEqual(page.said(), "a partial eclipse")

    def test_what_the_album_already_said_is_shown_again(self):
        page = self.invitation(asks=("for instance", ECLIPSE))
        self.assertEqual(page.said(), ECLIPSE)


if __name__ == "__main__":
    unittest.main()
