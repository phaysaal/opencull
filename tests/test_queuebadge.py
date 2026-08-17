"""The queue badge: labels, completion actions, and the ring's state."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QPushButton
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

if QApplication is not None:
    from opencull_qt.queuebadge import (
        QueueBadge,
        QueuePopover,
        completion,
        job_label,
    )


def _job(**kw):
    base = {"id": "x", "status": "queued", "progress": {}}
    base.update(kw)
    return base


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class LabelTests(unittest.TestCase):
    def test_a_timelapse_program_is_named_timelapse(self):
        self.assertEqual(job_label(_job(
            kind="kimiya_program", program="eclipse_timelapse.kim")),
            "Timelapse")

    def test_a_known_kind_reads_in_plain_words(self):
        self.assertEqual(job_label(_job(kind="delivery_export")), "Export")
        self.assertEqual(job_label(_job(kind="professional_shortlist")),
                         "Assessment")

    def test_an_unknown_program_is_titlecased_from_its_name(self):
        self.assertEqual(job_label(_job(
            kind="kimiya_program", program="my_program.kim")), "My program")


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class CompletionTests(unittest.TestCase):
    def test_a_finished_timelapse_offers_its_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "timelapse.mp4"
            video.write_bytes(b"film")
            report = root / "timelapse.json"
            report.write_text(json.dumps({"video": str(video)}))
            done = completion(_job(
                kind="kimiya_program", program="eclipse_timelapse.kim",
                status="completed", output=str(report)))
            self.assertEqual(done, ("Show the video", "reveal", str(video)))

    def test_a_finished_timelapse_without_a_video_offers_nothing(self):
        done = completion(_job(
            kind="kimiya_program", program="eclipse_timelapse.kim",
            status="completed", output="/does/not/exist.json"))
        self.assertIsNone(done)

    def test_an_export_offers_its_folder(self):
        done = completion(_job(
            kind="delivery_export", status="completed", output="/tmp/out"))
        self.assertEqual(done, ("Open folder", "reveal", "/tmp/out"))

    def test_a_failure_offers_its_log(self):
        done = completion(_job(status="failed", kind="treatment",
                               log="/tmp/run.log"))
        self.assertEqual(done, ("Log", "log", "/tmp/run.log"))

    def test_a_running_job_offers_no_completion(self):
        self.assertIsNone(completion(_job(status="running")))


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class BadgeStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_ring_shows_the_running_fraction_and_queued_count(self):
        badge = QueueBadge()
        self.addCleanup(badge.deleteLater)
        badge.set_snapshot({"jobs": [
            _job(id="a", status="running", progress={"fraction": 0.4}),
            _job(id="b", status="queued"),
            _job(id="c", status="queued"),
        ]})
        self.assertAlmostEqual(badge._ring._fraction, 0.4)
        self.assertEqual(badge._ring._count, 2)
        self.assertIn("1 running", badge.toolTip())

    def test_a_running_job_with_no_fraction_is_indeterminate(self):
        badge = QueueBadge()
        self.addCleanup(badge.deleteLater)
        badge.set_snapshot({"jobs": [
            _job(id="a", status="running", progress={"fraction": 0.0})]})
        self.assertIsNone(badge._ring._fraction)  # spins rather than 0%

    def test_idle_is_an_empty_ring(self):
        badge = QueueBadge()
        self.addCleanup(badge.deleteLater)
        badge.set_snapshot({"jobs": [
            _job(id="a", status="completed", progress={"fraction": 1.0})]})
        # No active work: not busy, not a filled arc.
        self.assertEqual(badge._ring._count, 0)


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class PopoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cancel_on_a_running_row_emits_the_job_id(self):
        pop = QueuePopover()
        self.addCleanup(pop.deleteLater)
        pop.rebuild({"jobs": [
            _job(id="run-1", kind="kimiya_program",
                 program="eclipse_timelapse.kim", status="running",
                 progress={"fraction": 0.5, "stage": "rendering"})]})
        heard = []
        pop.cancel_wanted.connect(heard.append)
        cancel = next(b for b in pop.findChildren(QPushButton)
                      if b.text() == "Cancel")
        cancel.click()
        self.assertEqual(heard, ["run-1"])

    def test_a_poll_that_changes_only_numbers_keeps_the_rows(self):
        """The open panel must not be torn down by every poll. Rebuilt
        whole each 1.5s it collapsed and regrew -- which read as closing
        and reopening continuously while a run was watched. Same jobs,
        same statuses: the rows stay and their moving parts move."""
        from PySide6.QtWidgets import QLabel

        pop = QueuePopover()
        self.addCleanup(pop.deleteLater)
        first = _job(id="run-1", kind="kimiya_program",
                     program="eclipse_timelapse.kim", status="running",
                     progress={"fraction": 0.4,
                               "stage": "rendering frame 100 of 274"})
        self.assertTrue(pop.rebuild({"jobs": [first]}))
        stage = pop._live["run-1"]["stage"]
        self.assertIsInstance(stage, QLabel)
        moved = dict(first, progress={"fraction": 0.6,
                                      "stage": "rendering frame 170 of 274"})
        self.assertFalse(pop.rebuild({"jobs": [moved]}))
        # The very same label, its words moved -- nothing was deleted.
        self.assertIs(pop._live["run-1"]["stage"], stage)
        self.assertIn("170", stage.text())
        # A status change is structural: now it rebuilds.
        done = dict(first, status="completed", output="/nope.json")
        self.assertTrue(pop.rebuild({"jobs": [done]}))

    def test_the_click_that_closed_the_popup_does_not_reopen_it(self):
        import time as time_module

        from opencull_qt.queuebadge import QueueBadge

        badge = QueueBadge()
        self.addCleanup(badge.deleteLater)
        badge.set_snapshot({"jobs": []})
        # The popup just auto-closed from this very click landing outside
        # it; the badge's press must not immediately reopen it.
        badge._popover.closed_at = time_module.monotonic()
        badge.mousePressEvent(None)
        self.assertFalse(badge._popover.isVisible())

    def test_a_finished_timelapse_row_reveals_its_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "timelapse.mp4"
            video.write_bytes(b"film")
            report = root / "timelapse.json"
            report.write_text(json.dumps({"video": str(video)}))
            pop = QueuePopover()
            self.addCleanup(pop.deleteLater)
            pop.rebuild({"jobs": [
                _job(id="d", kind="kimiya_program",
                     program="eclipse_timelapse.kim", status="completed",
                     output=str(report))]})
            heard = []
            pop.reveal_wanted.connect(heard.append)
            button = next(b for b in pop.findChildren(QPushButton)
                          if b.text() == "Show the video")
            button.click()
            self.assertEqual(heard, [str(video)])


if __name__ == "__main__":
    unittest.main()
