"""The job that places the sliders' advice bands for one frame.

One model call, a light panel, and a file beside the recipes that the
fine-tune page already reads. Advice, not walls -- so the validation is
about honesty: bands clamped inside the executable range, safe inside
artistic, and nothing written at all when the model answered the format
rather than the photograph.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zones_kernel  # noqa: E402
from opencull_gui.jobs import kimiya_arguments  # noqa: E402
from opencull_gui.zones import FORMAT  # noqa: E402
from recipe_compiler import RANGES  # noqa: E402


def answer(**overrides):
    stated = {
        "tone.exposure": {"safe": [-0.5, 0.3], "artistic": [-1.8, 0.9]},
        "tone.contrast": {"safe": [-10, 15], "artistic": [-30, 40]},
        "tone.highlight": {"safe": [-50, 5], "artistic": [-90, 20]},
        "tone.shadow": {"safe": [-10, 30], "artistic": [-30, 70]},
        "color.saturation": {"safe": [-40, 10], "artistic": [-100, 30]},
        "detail.clarity": {"safe": [-5, 10], "artistic": [-20, 30]},
        "detail.dehaze": {"safe": [-3, 10], "artistic": [-15, 35]},
        "detail.structure": {"safe": [-5, 10], "artistic": [-20, 30]},
        "tone.black": {"safe": [-10, 15], "artistic": [-30, 40]},
    }
    stated.update(overrides)
    return {"zones": json.dumps(stated),
            "rationale": "A veiled infrared frame; highlights are gone."}


class ZonesFileTests(unittest.TestCase):
    """From a model's answer to the file the page paints from."""

    def test_a_full_answer_becomes_the_file_the_page_reads(self):
        text = zones_kernel.zones_file("DSC00703.ARW", answer())
        value = json.loads(text)
        self.assertEqual(value["format"], FORMAT)
        self.assertEqual(value["photo"], "DSC00703.ARW")
        self.assertEqual(value["zones"]["tone.exposure"]["safe"],
                         [-0.5, 0.3])
        self.assertIn("veiled infrared", value["rationale"])
        self.assertTrue(zones_kernel.zones_valid(text))

    def test_a_band_past_the_executable_range_is_clamped_not_kept(self):
        text = zones_kernel.zones_file("A.ARW", answer(**{
            "tone.exposure": {"safe": [-90, 90], "artistic": [-90, 90]}}))
        value = json.loads(text)
        self.assertEqual(value["zones"]["tone.exposure"]["artistic"],
                         [-5.0, 5.0])

    def test_a_control_nobody_recognises_is_dropped(self):
        text = zones_kernel.zones_file("A.ARW", answer(**{
            "tone.nostalgia": {"safe": [0, 1], "artistic": [0, 2]}}))
        self.assertNotIn("tone.nostalgia", json.loads(text)["zones"])

    def test_too_few_placements_write_nothing_at_all(self):
        """Three controls is a model answering the format, not the frame."""
        thin = {"zones": json.dumps({
            "tone.exposure": {"safe": [-0.5, 0.3], "artistic": [-1, 1]},
            "tone.contrast": {"safe": [-10, 15], "artistic": [-30, 40]},
        }), "rationale": "x"}
        self.assertEqual(zones_kernel.zones_file("A.ARW", thin), "")

    def test_an_unreadable_answer_writes_nothing(self):
        self.assertEqual(zones_kernel.zones_file("A.ARW", None), "")
        self.assertEqual(zones_kernel.zones_file(
            "A.ARW", {"zones": "not json", "rationale": ""}), "")

    def test_the_file_never_claims_a_control_the_model_did_not_place(self):
        text = zones_kernel.zones_file("A.ARW", answer())
        self.assertNotIn("finish.vignette", json.loads(text)["zones"])

    def test_validity_rejects_a_hand_broken_file(self):
        text = zones_kernel.zones_file("A.ARW", answer())
        broken = json.loads(text)
        broken["zones"]["tone.exposure"]["safe"] = [3.0, -3.0]
        self.assertFalse(zones_kernel.zones_valid(json.dumps(broken)))


class ZonesPromptTests(unittest.TestCase):
    """What the model is told, and what the panel is asked."""

    def evidence(self, spectrum="visible"):
        return json.dumps({
            "photo": "A.ARW", "spectrum": spectrum, "cutoff_nm": 760.0,
            "about": "a partial solar eclipse",
            "measured": {"mean": 51.7, "veil_percent": 58.0}})

    def test_the_prompt_carries_the_frame_and_the_catalogue(self):
        built = zones_kernel.zones_prompt(self.evidence())
        self.assertIn("veil_percent", built)
        self.assertIn("tone.exposure", built)
        self.assertIn("default_safe", built)
        self.assertIn("advice, not walls", built)

    def test_an_infrared_frame_is_said_to_be_one(self):
        built = zones_kernel.zones_prompt(self.evidence("infrared"))
        self.assertIn("INFRARED", built)
        self.assertIn("760nm", built)

    def test_the_catalogue_covers_every_executable_control(self):
        catalogue = json.loads(zones_kernel.controls_catalogue())
        self.assertEqual(set(catalogue), set(RANGES))

    def test_the_warrant_puts_the_numbers_beside_the_bands(self):
        file_text = zones_kernel.zones_file("A.ARW", answer())
        warrant = zones_kernel.zones_warrant(self.evidence(), file_text)
        value = json.loads(warrant)
        self.assertEqual(value["measured"]["veil_percent"], 58.0)
        self.assertIn("tone.exposure", value["bands"])

    def test_the_policy_is_short_enough_for_a_judge_to_finish(self):
        self.assertLess(len(zones_kernel.zones_policy().split()), 80)


class ZonesJobTests(unittest.TestCase):
    """The queue speaks the same sentence for this kind as for the rest."""

    def test_the_command_carries_frame_spectrum_and_subject(self):
        program, arguments = kimiya_arguments({
            "kind": "control_zones", "photos": "/p", "photo": "A.ARW",
            "output": "o.json", "spectrum": "infrared",
            "cutoff_nm": 760.0, "about": "not the moon"})
        self.assertEqual(program, "control_zones.kim")
        said = dict(item.split("=", 1) for item in arguments)
        self.assertEqual(said["photo"], "A.ARW")
        self.assertEqual(said["spectrum"], "infrared")
        self.assertIn("not the moon", said["about"])


if __name__ == "__main__":
    unittest.main()
