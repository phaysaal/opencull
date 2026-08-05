"""Delivering finished renderings, all of them at once.

Development exports one frame at a time, because that is where you are
standing when you decide a frame is finished. Delivering a shoot is a
different act: twenty renders, one folder, one decision.

Only a render the manifest knows about can leave, so everything offered
here has a recorded provenance -- which photograph, which treatment, which
recipe revision. Nothing is re-rendered: these files already exist, and a
delivery that quietly re-ran the renderer could hand over something other
than what was approved on screen.

An export never writes over a file. If the name is taken the copy takes the
next free one and the page says which, rather than a dialog promising to
replace something and then not doing it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.development import DevelopmentWorkspace

from . import theme
from .widgets import short_path

RENDER_ROW = 34

# The list and its controls stay one group at a readable width: a primary
# action flung to the far edge of a wide window is a long way from the thing
# it acts on.
MEASURE = 940


class _DeliverySignals(QObject):
    done = Signal(str, str, str)          # photograph, requested, written
    failed = Signal(str, str)             # photograph, reason


class _DeliveryJob(QRunnable):
    """One copy, off the interface thread.

    A render can be a full-size 16-bit TIFF, and a folder of them is enough
    copying to freeze a window that is doing nothing else useful.
    """

    def __init__(self, workspace: DevelopmentWorkspace, photo: str,
                 source: str, destination: str):
        super().__init__()
        self.workspace = workspace
        self.photo = photo
        self.source = source
        self.destination = destination
        self.signals = _DeliverySignals()

    def run(self) -> None:
        try:
            record = self.workspace.export_render(self.source, self.destination)
        except Exception as exc:                     # noqa: BLE001 - reported
            self.signals.failed.emit(self.photo, str(exc))
            return
        self.signals.done.emit(
            self.photo, str(record.get("requested_destination", "")),
            str(record.get("destination", "")))


class Deliverer(QObject):
    """Copies renders out, one job each, and counts what is outstanding."""

    done = Signal(str, str, str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, workspace: DevelopmentWorkspace,
                 pool: QThreadPool | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.workspace = workspace
        self.pool = pool or QThreadPool()
        self.pending = 0

    def deliver(self, photo: str, source: str, destination: str) -> None:
        job = _DeliveryJob(self.workspace, photo, source, destination)
        job.signals.done.connect(self._one_done)
        job.signals.failed.connect(self._one_failed)
        self.pending += 1
        self.pool.start(job)

    def _one_done(self, photo: str, requested: str, written: str) -> None:
        self.done.emit(photo, requested, written)
        self._settle()

    def _one_failed(self, photo: str, reason: str) -> None:
        self.failed.emit(photo, reason)
        self._settle()

    def _settle(self) -> None:
        self.pending = max(0, self.pending - 1)
        if self.pending == 0:
            self.finished.emit()

    def shutdown(self) -> None:
        self.pool.waitForDone(2000)


class ExportPage(QWidget):
    """Every finished rendering, and where it is going."""

    closed = Signal()

    def __init__(self, workspace: DevelopmentWorkspace,
                 pool: QThreadPool | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.workspace = workspace
        self.renders: list[dict] = []
        self.destination: Path | None = None
        self.deliverer = Deliverer(workspace, pool, self)
        self.deliverer.done.connect(self._delivered)
        self.deliverer.failed.connect(self._failed)
        self.deliverer.finished.connect(self._all_done)
        self._written: list[str] = []
        self._renamed = 0
        self._refused = 0
        self._build()
        self.refresh()

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.bar = self._bar()
        outer.addWidget(self.bar)

        column = QVBoxLayout()
        column.setContentsMargins(22, 16, 22, 14)
        column.setSpacing(12)

        self.lead = QLabel("")
        self.lead.setObjectName("hint")
        self.lead.setWordWrap(True)
        self.lead.setFont(theme.body(10))
        self.lead.setMaximumWidth(MEASURE)
        column.addWidget(self.lead)

        self.list = QListWidget()
        self.list.setObjectName("treatmentList")
        self.list.itemChanged.connect(lambda _item: self._count_chosen())
        self.list.setMaximumWidth(MEASURE)
        column.addWidget(self.list)

        where = QHBoxLayout()
        where.setSpacing(10)
        self.folder = QLabel("")
        self.folder.setObjectName("rowPath")
        self.folder.setFont(theme.mono(8))
        choose = QPushButton("Choose folder…")
        choose.setObjectName("ghost")
        choose.setFont(theme.body(10))
        choose.setCursor(Qt.CursorShape.PointingHandCursor)
        choose.clicked.connect(self.choose_folder)
        where.addWidget(choose)
        where.addWidget(self.folder, 1)
        column.addLayout(where)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.all_button = QPushButton("Select all")
        self.all_button.setObjectName("ghost")
        self.all_button.setFont(theme.body(10))
        self.all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_button.clicked.connect(self.select_all)
        actions.addWidget(self.all_button)

        self.deliver_button = QPushButton("Deliver")
        self.deliver_button.setObjectName("primary")
        self.deliver_button.setFont(theme.body(10))
        self.deliver_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.deliver_button.clicked.connect(self.deliver)
        actions.addWidget(self.deliver_button)
        actions.addStretch(1)
        column.addLayout(actions)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        self.status.setMaximumWidth(MEASURE)
        column.addWidget(self.status)
        column.addStretch(1)

        holder = QWidget()
        holder.setObjectName("page")
        holder.setLayout(column)
        outer.addWidget(holder, 1)

    def _bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("chrome")
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 0, 22, 0)
        layout.setSpacing(12)

        back = QPushButton("← Projects")
        back.setObjectName("ghost")
        back.setFont(theme.body(10))
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(self.closed)
        layout.addWidget(back)

        self.title = QLabel("EXPORT")
        self.title.setObjectName("chromeTitle")
        self.title.setFont(theme.display(11))
        layout.addWidget(self.title)
        layout.addStretch(1)

        self.progress = QLabel("")
        self.progress.setObjectName("hint")
        self.progress.setFont(theme.body(9))
        layout.addWidget(self.progress)
        self.indicator = self.progress
        return bar

    # --- state ------------------------------------------------------------

    def payload(self) -> dict:
        try:
            return self.workspace.export_payload()
        except Exception as exc:                     # noqa: BLE001 - reported
            self._report(f"The renders could not be read: {exc}", "alarm")
            return {}

    def refresh(self) -> None:
        payload = self.payload()
        self.renders = list(payload.get("renders") or [])
        delivered = {
            str(item.get("source", "")) for item in payload.get("exports") or []
            if isinstance(item, dict)}
        if self.destination is None:
            self.destination = Path(str(
                payload.get("default_export_directory") or Path.home()))
        self.folder.setText(short_path(str(self.destination)))

        self.list.blockSignals(True)
        self.list.clear()
        for render in self.renders:
            photo = str(render.get("source_photo") or "")
            variant = str(render.get("variant") or "render")
            already = str(Path(str(render.get("path", ""))).expanduser().resolve())
            mark = "   ·   delivered" if already in delivered else ""
            name = str(render.get("suggested_filename")
                       or Path(str(render.get("path", ""))).name)
            item = QListWidgetItem(
                f"  {photo}   ·   {variant}   →   {name}{mark}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # Already-delivered renders start unticked: delivering the shoot
            # again should not silently make a second copy of everything.
            item.setCheckState(
                Qt.CheckState.Unchecked if already in delivered
                else Qt.CheckState.Checked)
            item.setToolTip(short_path(str(render.get("path", ""))))
            item.setSizeHint(QSize(0, RENDER_ROW))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.list.setFixedHeight(
            min(max(self.list.count(), 1), 12) * RENDER_ROW + 10)

        hidden = int(payload.get("hidden_render_revisions") or 0)
        self.lead.setText(
            "Nothing has been rendered yet. A frame becomes deliverable once "
            "it has been developed."
            if not self.renders else
            f"{len(self.renders)} rendering"
            f"{'' if len(self.renders) == 1 else 's'} ready to deliver."
            + (f" {hidden} earlier revision{'' if hidden == 1 else 's'} "
               "stayed in the manifest and are not offered." if hidden else ""))
        self.all_button.setEnabled(bool(self.renders))
        self._count_chosen()

    def chosen(self) -> list[dict]:
        return [
            render for row, render in enumerate(self.renders)
            if self.list.item(row) is not None
            and self.list.item(row).checkState() == Qt.CheckState.Checked]

    def _count_chosen(self) -> None:
        count = len(self.chosen())
        self.deliver_button.setEnabled(bool(count) and self.deliverer.pending == 0)
        self.deliver_button.setText(
            "Deliver" if not count else
            f"Deliver {count} render{'' if count == 1 else 's'}")
        self.progress.setText(
            f"{count} of {len(self.renders)} chosen" if self.renders else "")

    def select_all(self) -> None:
        every = all(
            self.list.item(row).checkState() == Qt.CheckState.Checked
            for row in range(self.list.count())) and self.list.count()
        self.list.blockSignals(True)
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(
                Qt.CheckState.Unchecked if every else Qt.CheckState.Checked)
        self.list.blockSignals(False)
        self.all_button.setText("Select all" if every else "Select none")
        self._count_chosen()

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Deliver renderings to", str(self.destination))
        if chosen:
            self.destination = Path(chosen)
            self.folder.setText(short_path(chosen))

    # --- delivering -------------------------------------------------------

    def deliver(self) -> None:
        renders = self.chosen()
        if not renders:
            return
        self._written = []
        self._renamed = 0
        self._refused = 0
        self.deliver_button.setEnabled(False)
        self._report(
            f"Delivering {len(renders)} rendering"
            f"{'' if len(renders) == 1 else 's'} to "
            f"{short_path(str(self.destination))}.")
        for render in renders:
            name = str(render.get("suggested_filename")
                       or Path(str(render.get("path", ""))).name)
            self.deliverer.deliver(
                str(render.get("source_photo") or name),
                str(render.get("path", "")),
                str(self.destination / name))

    def _delivered(self, photo: str, requested: str, written: str) -> None:
        self._written.append(written)
        if written != str(Path(requested).expanduser().resolve()):
            self._renamed += 1

    def _failed(self, photo: str, reason: str) -> None:
        self._refused += 1
        self._report(f"{photo} could not be delivered: {reason}", "alarm")

    def _all_done(self) -> None:
        written = len(self._written)
        if not written:
            self._count_chosen()
            return
        message = (
            f"{written} rendering{'' if written == 1 else 's'} delivered to "
            f"{short_path(str(self.destination))}.")
        if self._renamed:
            # Said here rather than swallowed: a photographer who counts the
            # files must be able to account for a name they did not choose.
            message += (
                f" {self._renamed} took a new name, because a file of that "
                "name was already there and Darkimiya does not write over one.")
        self._report(message, "alarm" if self._refused else "ok")
        self.refresh()

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def shutdown(self) -> None:
        self.deliverer.shutdown()
