"""The other road: taste before physics, asked for by name."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_superimpose import night  # noqa: E402

from developed_stack_pipeline import run_developed_stack  # noqa: E402


def exposure_recipe(photo: str, stops: float) -> dict:
    return {
        "format": "opencull-development-recipe-v1",
        "photo": photo, "style": "stacksrc-test",
        "source_kind": "jpeg", "title": "brighter",
        "operations": [{
            "op": "tone.exposure", "unit": "EV", "mode": "delta",
            "value": stops, "enabled": True}],
    }


class DevelopedStackTests(unittest.TestCase):
    """Each frame developed with its look, then stacked at 16 bits."""

    def test_the_look_reaches_the_stack(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            night(root, [(0.0, 0.0), (4.0, 1.0), (8.0, 2.0)])
            plan = root / "plan.json"
            plan.write_text(json.dumps({"frames": {
                name: exposure_recipe(name, 1.0)
                for name in ("N000.jpg", "N001.jpg", "N002.jpg")}}))
            out = root / "out"
            result = run_developed_stack(
                root, plan, out, "clipped", 24.0, 23.5,
                "default", "markesteijn-1-pass")
            self.assertEqual(result.get("frames_used"), 3)
            proof = np.asarray(Image.open(
                result["proof"]).convert("L"), np.float32) / 255.0
            plain = np.asarray(Image.open(
                root / "N000.jpg").convert("L"), np.float32) / 255.0
            # A stop of exposure on every frame reaches the stack: its
            # sky sits clearly above the undeveloped frame's.
            self.assertGreater(float(np.median(proof)),
                               float(np.median(plain)) * 1.5)
            receipt = json.loads(
                (out / "developed-stack.json").read_text())
            self.assertEqual(receipt["frames"],
                             ["N000.jpg", "N001.jpg", "N002.jpg"])
            # And the staging TIFFs were temporary: only the stack
            # remains.
            self.assertFalse(list(out.glob(".developed-frames-*")))

    def test_one_frame_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            night(root, [(0.0, 0.0)])
            plan = root / "plan.json"
            plan.write_text(json.dumps({"frames": {
                "N000.jpg": exposure_recipe("N000.jpg", 1.0)}}))
            with self.assertRaises(ValueError):
                run_developed_stack(
                    root, plan, root / "out", "clipped", 24.0, 23.5,
                    "default", "markesteijn-1-pass")


if __name__ == "__main__":                           # pragma: no cover
    unittest.main()
