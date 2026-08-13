"""Telling the models that a photograph is infrared.

A model shown a deep blue frame at eighteen levels out of 255, and asked
to weigh "light and tonality" and to separate defects from recoverable
white balance, will report the filter as three faults. It would be right
about a photograph nobody took. These hold down both halves of the fix:
what the model is shown, and what it is told.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edit_suggestion_kernel import edit_direction_prompt  # noqa: E402
from scan import (  # noqa: E402
    measure,
    neutralized,
    preview_cache_name,
    visible_photograph,
)
from shortlist_kernel import (  # noqa: E402
    build_professional_candidates,
    parse_professional_candidates,
    professional_assessment_prompt,
    professional_candidate_path,
)


def infrared_frame(path: Path, size=(900, 600)) -> Path:
    """A frame with the cast and the darkness a 760nm capture arrives with."""
    pixels = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    pixels[..., 0], pixels[..., 1], pixels[..., 2] = 10, 12, 32
    # Something to measure: a bright band, so the frame is not featureless.
    pixels[200:260, :, :] = (60, 64, 120)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels).save(path, quality=95)
    return path


class NeutralisingTests(unittest.TestCase):
    """What the model is shown."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def channels(self, image):
        return np.asarray(image.convert("RGB"),
                          dtype=np.float32).reshape(-1, 3).mean(axis=0)

    def linear_channels(self, image):
        """The averages the operation actually equalises.

        It is a gain per channel in linear light, so that is where the
        promise is: the averages agree. It cannot make every pixel
        neutral, and should not -- two regions of a photograph lit
        differently are allowed to stay different.
        """
        rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        return np.power(rgb, 2.2).reshape(-1, 3).mean(axis=0)

    def test_the_cast_goes_and_the_frame_becomes_visible(self):
        frame = Image.open(infrared_frame(self.root / "A.jpg"))
        before = self.linear_channels(frame)
        after = self.linear_channels(neutralized(frame))
        self.assertGreater(
            float(before.max() / max(before.min(), 1e-9)), 3.0,
            "the fixture must actually carry a cast")
        # Not exact, and it should not be claimed as exact: the result is
        # written back as eight-bit values and its brightest tones clip
        # against the ceiling, both of which move a channel average a
        # little. A cast of three-to-one becoming three percent is the
        # difference between unjudgeable and judgeable.
        self.assertLess(float(after.max() / max(after.min(), 1e-9)), 1.05)
        self.assertGreater(
            float(self.channels(neutralized(frame)).mean()),
            float(self.channels(frame).mean()),
            "a frame at eighteen levels has to be brought up to be judged")

    def test_it_keeps_what_is_in_the_photograph(self):
        """Neutralising must not flatten the picture into one tone."""
        frame = Image.open(infrared_frame(self.root / "A.jpg"))
        after = np.asarray(neutralized(frame).convert("L"), dtype=np.float32)
        self.assertGreater(float(after.std()), 5.0)

    def test_a_frame_that_is_already_neutral_survives(self):
        flat = Image.fromarray(
            np.full((40, 60, 3), 128, dtype=np.uint8))
        after = self.channels(neutralized(flat))
        self.assertLess(float(after.max() - after.min()), 1.0)


class WhatIsShownTests(unittest.TestCase):
    """The infrared rendering is its own file, and is served on request."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_the_two_renderings_are_filed_apart(self):
        plain = preview_cache_name("A.ARW", "deadbeefcafe")
        infrared = preview_cache_name("A.ARW", "deadbeefcafe", "infrared")
        self.assertNotEqual(plain, infrared)
        self.assertIn(".infrared.", infrared)

    def test_asking_for_the_camera_rendering_never_gets_the_infrared_one(self):
        previews = self.root / ".darkimiya" / "Previews"
        previews.mkdir(parents=True)
        (previews / "A.ARW.deadbeef.infrared.preview.jpg").write_bytes(b"x")
        (self.root / "A.ARW").write_bytes(b"raw")
        self.assertEqual(
            visible_photograph(self.root, "A.ARW").name, "A.ARW")

    def test_an_infrared_album_is_shown_its_own_rendering(self):
        previews = self.root / ".darkimiya" / "Previews"
        previews.mkdir(parents=True)
        (previews / "A.ARW.deadbeef.preview.jpg").write_bytes(b"x")
        (previews / "A.ARW.deadbeef.infrared.preview.jpg").write_bytes(b"y")
        (self.root / "A.ARW").write_bytes(b"raw")
        self.assertEqual(
            visible_photograph(self.root, "A.ARW", "infrared").name,
            "A.ARW.deadbeef.infrared.preview.jpg")

    def test_without_one_the_camera_rendering_still_stands(self):
        previews = self.root / ".darkimiya" / "Previews"
        previews.mkdir(parents=True)
        (previews / "A.ARW.deadbeef.preview.jpg").write_bytes(b"x")
        (self.root / "A.ARW").write_bytes(b"raw")
        self.assertEqual(
            visible_photograph(self.root, "A.ARW", "infrared").name,
            "A.ARW.deadbeef.preview.jpg")


class MeasuredEvidenceTests(unittest.TestCase):
    """The numbers handed to the model describe the photograph."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_infrared_evidence_is_taken_from_the_neutralised_frame(self):
        path = infrared_frame(self.root / "A.jpg")
        plain = measure(path, self.root).manifest_record()
        infrared = measure(path, self.root, "infrared").manifest_record()
        self.assertGreater(infrared["contrast"], plain["contrast"])
        self.assertGreater(infrared["exposure"], plain["exposure"])

    def test_an_ordinary_frame_is_measured_as_it_always_was(self):
        path = infrared_frame(self.root / "A.jpg")
        first = measure(path, self.root).manifest_record()
        second = measure(path, self.root, "visible").manifest_record()
        self.assertEqual(first["technical_score"], second["technical_score"])


class WhatIsSaidTests(unittest.TestCase):
    """The prompts, which are the other half."""

    def test_the_assessor_is_told_and_told_the_filter(self):
        prompt = professional_assessment_prompt(
            {"photo": "A.ARW", "spectrum": "infrared", "cutoff_nm": 760},
            "landscape")
        self.assertIn("INFRARED PHOTOGRAPH", prompt)
        self.assertIn("760nm", prompt)
        self.assertIn("Colour is not evidence", prompt)

    def test_an_unstated_filter_still_says_infrared(self):
        prompt = professional_assessment_prompt(
            {"photo": "A.ARW", "spectrum": "infrared"}, "landscape")
        self.assertIn("INFRARED PHOTOGRAPH", prompt)
        self.assertNotIn("0nm", prompt)

    def test_an_ordinary_frame_is_told_nothing_new(self):
        prompt = professional_assessment_prompt({"photo": "A.JPG"}, "family")
        self.assertNotIn("INFRARED", prompt)

    def test_the_editing_model_is_told_too(self):
        prompt = edit_direction_prompt(
            {"photo": "A.ARW", "raw_files": ["x"], "spectrum": "infrared",
             "cutoff_nm": 720}, "landscape")
        self.assertIn("INFRARED PHOTOGRAPH", prompt)
        self.assertIn("720nm", prompt)
        self.assertIn("tonal separation", prompt)

    def test_the_editing_model_is_warned_off_correcting_the_colour(self):
        prompt = edit_direction_prompt(
            {"photo": "A.ARW", "raw_files": ["x"], "spectrum": "infrared"},
            "landscape")
        self.assertIn("no white balance to", " ".join(prompt.split()))


class BundleTests(unittest.TestCase):
    """The spectrum reaches the prompt from the album that set it."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.photos = self.root / "photos"
        self.photos.mkdir(parents=True)
        infrared_frame(self.photos / "A.JPG")
        report = self.root / "shoot-results.json"
        report.write_text(json.dumps({
            "format": "opencull-report-v2", "manifest_sha256": "x",
            "clusters": [{"cluster_id": "g1", "photos": ["A.JPG"]}],
            "keep": [{"cluster_id": "g1", "photos": ["A.JPG"],
                      "rationale": "kept", "confidence": 0.9, "warning": "",
                      "fallback": False, "photographic_assessment": []}],
            "warnings": [], "adaptive_clustering": {"enabled": False},
            "notice": "read only",
        }), encoding="utf-8")
        self.report = report
        self.addCleanup(self._temporary.cleanup)

    def bundle(self, spectrum="visible", cutoff=0):
        return build_professional_candidates(
            str(self.report), str(self.photos), "", "all", 100,
            spectrum, cutoff)

    def test_every_candidate_carries_the_albums_spectrum(self):
        candidates = parse_professional_candidates(
            self.bundle("infrared", 850))
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertEqual(candidate["spectrum"], "infrared")
            self.assertEqual(candidate["cutoff_nm"], 850.0)

    def test_the_prompt_built_from_it_says_so(self):
        candidate = parse_professional_candidates(
            self.bundle("infrared", 850))[0]
        self.assertIn(
            "850nm", professional_assessment_prompt(candidate, "landscape"))

    def test_an_ordinary_album_records_no_filter(self):
        candidate = parse_professional_candidates(self.bundle())[0]
        self.assertEqual(candidate["spectrum"], "visible")
        self.assertEqual(candidate["cutoff_nm"], 0.0)

    def test_the_path_offered_follows_the_spectrum(self):
        bundle = self.bundle("infrared", 760)
        candidate = parse_professional_candidates(bundle)[0]
        # A JPEG frame is itself, whatever the spectrum; what matters is
        # that asking does not fail and does not point somewhere absent.
        self.assertTrue(
            Path(professional_candidate_path(bundle, candidate)).is_file())

    def test_a_spectrum_nobody_recognises_is_refused(self):
        with self.assertRaises(ValueError):
            self.bundle("ultraviolet", 0)


if __name__ == "__main__":
    unittest.main()
