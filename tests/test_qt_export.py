"""The export phase: delivering finished renderings, all of them at once."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt, QThreadPool
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:  # pragma: no cover - exercised only without PySide6
    QApplication = None

from opencull_gui.project import register_render  # noqa: E402
from opencull_gui.report import load_report  # noqa: E402
from tests.test_qt_develop import build_shoot  # noqa: E402


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class ExportPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.report_path, self.photos = build_shoot(self.root)
        self.report = load_report(self.report_path)
        self.destination = self.root / "delivered"
        self.destination.mkdir()
        self.addCleanup(self._temporary.cleanup)

    def workspace(self):
        from opencull_qt.develop import workspace_for

        return workspace_for(self.report, self.photos, decoders=set())

    def render(self, workspace, photo: str, variant: str) -> Path:
        """Register a rendering the way the develop page registers one."""
        path = workspace.project_layout["Developments"] / f"{photo}-{variant}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{photo}/{variant}".encode())
        register_render(workspace.project_path, {
            "recipe": {"style": variant, "source_photo": photo, "revision": 1},
            "output": {"path": str(path), "sha256": f"{photo}-{variant}"},
            "created_at": "2026-01-01T00:00:00+00:00"})
        return path

    def page(self, renders=(("A.JPG", "standard"),)):
        from opencull_qt.export import ExportPage

        workspace = self.workspace()
        for photo, variant in renders:
            self.render(workspace, photo, variant)
        pool = QThreadPool()
        pool.setMaxThreadCount(1)
        page = ExportPage(workspace, pool)
        page.destination = self.destination
        self.addCleanup(page.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def settle(self, page) -> None:
        page.deliverer.pool.waitForDone(5000)
        self.application.processEvents()

    def text(self, page) -> str:
        return "\n".join(label.text() for label in page.findChildren(QLabel))

    def rows(self, page) -> list[str]:
        return [page.list.item(row).text() for row in range(page.list.count())]

    # --- what is offered --------------------------------------------------

    def test_every_registered_rendering_is_offered(self):
        page = self.page(renders=(("A.JPG", "standard"), ("B.JPG", "creative")))
        self.assertEqual(len(self.rows(page)), 2)
        self.assertIn("A.JPG", self.rows(page)[0])
        self.assertIn("creative", self.rows(page)[1])

    def test_with_nothing_rendered_it_says_what_is_missing(self):
        page = self.page(renders=())
        self.assertIn("Nothing has been rendered yet", self.text(page))
        self.assertFalse(page.deliver_button.isEnabled())

    def test_the_button_says_how_many_it_will_write(self):
        page = self.page(renders=(("A.JPG", "standard"), ("B.JPG", "creative")))
        self.assertEqual(page.deliver_button.text(), "Deliver 2 renders")

    def test_choosing_none_disables_delivering(self):
        page = self.page()
        page.select_all()  # everything starts chosen, so this clears it
        self.assertFalse(page.deliver_button.isEnabled())

    # --- delivering -------------------------------------------------------

    def test_delivering_copies_the_chosen_renderings(self):
        page = self.page(renders=(("A.JPG", "standard"), ("B.JPG", "creative")))
        page.deliver()
        self.settle(page)
        self.assertEqual(len(list(self.destination.iterdir())), 2)
        self.assertIn("2 renderings delivered", page.status.text())

    def test_a_delivery_never_writes_over_a_file(self):
        page = self.page()
        page.deliver()
        self.settle(page)
        first = next(self.destination.iterdir())
        page.refresh()
        page.list.item(0).setCheckState(Qt.CheckState.Checked)
        page.deliver()
        self.settle(page)
        names = sorted(item.name for item in self.destination.iterdir())
        self.assertEqual(len(names), 2)
        self.assertIn(first.name, names)
        self.assertIn("took a new name", page.status.text())

    def test_what_has_been_delivered_is_not_offered_again_by_default(self):
        page = self.page()
        page.deliver()
        self.settle(page)
        page.refresh()
        self.assertIn("delivered", self.rows(page)[0])
        self.assertEqual(page.list.item(0).checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(page.chosen(), [])

    def test_a_delivery_is_recorded_in_the_manifest(self):
        page = self.page()
        page.deliver()
        self.settle(page)
        exports = page.workspace.export_payload()["exports"]
        self.assertEqual(len(exports), 1)
        self.assertTrue(exports[0]["sha256"])

    def test_a_render_that_is_not_the_projects_is_refused(self):
        page = self.page()
        outsider = self.root / "not-ours.jpg"
        outsider.write_bytes(b"elsewhere")
        page.deliverer.deliver(
            "X.JPG", str(outsider), str(self.destination / "x.jpg"))
        self.settle(page)
        self.assertIn("could not be delivered", page.status.text())
        self.assertFalse((self.destination / "x.jpg").exists())

    def test_nothing_is_re_rendered_to_deliver_it(self):
        # The bytes handed over are the bytes that were approved on screen.
        page = self.page()
        source = Path(page.renders[0]["path"])
        before = source.read_bytes()
        page.deliver()
        self.settle(page)
        written = next(self.destination.iterdir())
        self.assertEqual(written.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(QApplication is not None, "PySide6 is not installed")
class DeliveryOrderTests(ExportPageTests):
    """The delivery's order is a decision, and it is recorded."""

    def test_the_order_delivered_is_the_order_recorded(self):
        page = self.page(renders=(("A.JPG", "standard"), ("B.JPG", "creative")))
        # Drag B above A: same rows, reversed.
        first = page.list.takeItem(0)
        page.list.insertItem(1, first)
        first.setCheckState(Qt.CheckState.Checked)
        page.deliver()
        self.settle(page)
        exports = page.workspace.export_payload()["exports"]
        ordered = sorted(exports, key=lambda item: item.get("sequence", 0))
        self.assertEqual(
            [Path(item["source"]).name.split("-")[0] for item in ordered],
            ["B.JPG", "A.JPG"])
        self.assertEqual([item["sequence"] for item in ordered], [1, 2])

    def test_a_proof_sheet_is_written_when_asked(self):
        page = self.page()
        page.proof.setChecked(True)
        page.deliver()
        self.settle(page)
        sheets = list(self.destination.glob("*proof sheet*.html"))
        self.assertEqual(len(sheets), 1)
        body = sheets[0].read_text(encoding="utf-8")
        self.assertIn("A.JPG", body)
        self.assertIn("data:image/jpeg;base64,", body)
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)
        self.assertIn("Proof sheet:", page.status.text())

    def test_no_sheet_appears_unasked(self):
        page = self.page()
        page.deliver()
        self.settle(page)
        self.assertEqual(list(self.destination.glob("*.html")), [])

    def test_a_second_sheet_never_overwrites_the_first(self):
        page = self.page()
        page.proof.setChecked(True)
        page.deliver()
        self.settle(page)
        page.refresh()
        page.list.item(0).setCheckState(Qt.CheckState.Checked)
        page.deliver()
        self.settle(page)
        self.assertEqual(
            len(list(self.destination.glob("*proof sheet*.html"))), 2)
