"""The develop page: see a photograph rendered, beside the one you shot.

Development is a comparison. A treatment is only worth anything relative to
what was already there, so the two are always on screen together rather than
one replacing the other.

Rendering is slow enough to be felt -- a demosaic and a recipe over a whole
frame -- so it happens on a worker thread and only when asked for. Nothing
renders because a photograph was selected; the photographer says when.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.development import DevelopmentWorkspace, suggested_filename

from . import theme
from .previews import PreviewLoader, scaled
from .widgets import short_path, workspace_title

# A proof, not a delivery. Big enough to judge a treatment on a laptop
# screen, small enough that a demosaic finishes while you are still looking
# at the frame.
PROOF_EDGE = 1600

# Set on the items themselves. A row's painted height comes from the
# stylesheet, which sizeHintForRow does not know about, so asking it how tall
# the list should be returns a box that clips its own contents.
TREATMENT_ROW = 34


class _Signals(QObject):
    done = Signal(int, str, str, str)     # generation, photo, treatment, path
    failed = Signal(int, str, str)        # generation, photo, reason


class _RenderJob(QRunnable):
    def __init__(self, workspace: DevelopmentWorkspace, photo: str,
                 treatment: str, engine: str, demosaic: str,
                 signals: _Signals, generation: int):
        super().__init__()
        self.workspace = workspace
        self.photo = photo
        self.treatment = treatment
        self.engine = engine
        self.demosaic = demosaic
        self.signals = signals
        self.generation = generation
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            path = self.workspace.recipe_preview(
                self.photo, self.treatment, self.engine, self.demosaic,
                PROOF_EDGE)
        except Exception as exc:
            self.signals.failed.emit(self.generation, self.photo, str(exc))
            return
        self.signals.done.emit(
            self.generation, self.photo, self.treatment, str(path))


class _ExportSignals(QObject):
    done = Signal(str, str, str)          # photo, requested, written
    failed = Signal(str, str)             # photo, reason


class _ExportJob(QRunnable):
    def __init__(self, workspace: DevelopmentWorkspace, photo: str,
                 treatment: str, engine: str, demosaic: str,
                 destination: str, signals: _ExportSignals):
        super().__init__()
        self.workspace = workspace
        self.photo = photo
        self.treatment = treatment
        self.engine = engine
        self.demosaic = demosaic
        self.destination = destination
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            # Full size, and registered in the manifest before it leaves, so
            # what was delivered has a recorded provenance.
            result = self.workspace.render_full(
                self.photo, self.treatment, self.engine, self.demosaic)
            record = self.workspace.export_render(
                str(result["render"]["path"]), self.destination)
        except Exception as exc:
            self.signals.failed.emit(self.photo, str(exc))
            return
        self.signals.done.emit(
            self.photo, self.destination, str(record["destination"]))


class Exporter(QObject):
    """Deliveries in flight.

    Unlike a proof, an export is never abandoned. It was asked for
    deliberately, so moving to another frame does not cancel it, and several
    queue behind each other rather than competing for the machine.
    """

    done = Signal(str, str, str)
    failed = Signal(str, str)

    def __init__(self, workspace: DevelopmentWorkspace,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.workspace = workspace
        self.pending = 0
        self._signals = _ExportSignals()
        self._signals.done.connect(self._finished)
        self._signals.failed.connect(self._failed)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def export(self, photo: str, treatment: str, engine: str, demosaic: str,
               destination: str) -> None:
        self.pending += 1
        self._pool.start(_ExportJob(
            self.workspace, photo, treatment, engine, demosaic, destination,
            self._signals))

    def _finished(self, photo: str, requested: str, written: str) -> None:
        self.pending = max(0, self.pending - 1)
        self.done.emit(photo, requested, written)

    def _failed(self, photo: str, reason: str) -> None:
        self.pending = max(0, self.pending - 1)
        self.failed.emit(photo, reason)

    def shutdown(self) -> None:
        self._pool.clear()
        # An export already writing is left to finish its copy rather than
        # abandoned partway to a file somebody is expecting.
        self._pool.waitForDone(20000)


class Renderer(QObject):
    """One render at a time, and never a stale one."""

    done = Signal(str, str, QPixmap)
    failed = Signal(str, str)

    def __init__(self, workspace: DevelopmentWorkspace,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.workspace = workspace
        self._generation = 0
        self._signals = _Signals()
        self._signals.done.connect(self._finished)
        self._signals.failed.connect(self._failed)
        self._pool = QThreadPool(self)
        # A render saturates the machine on its own. Two at once makes both
        # slower and neither useful sooner.
        self._pool.setMaxThreadCount(1)

    def render(self, photo: str, treatment: str, engine: str,
               demosaic: str) -> None:
        self._generation += 1
        self._pool.start(_RenderJob(
            self.workspace, photo, treatment, engine, demosaic,
            self._signals, self._generation))

    def abandon(self) -> None:
        """Stop caring about a render whose frame is no longer on screen."""
        self._generation += 1

    def _finished(self, generation: int, photo: str, treatment: str,
                  path: str) -> None:
        if generation != self._generation:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.failed.emit(photo, "the render could not be read back")
            return
        self.done.emit(photo, treatment, pixmap)

    def _failed(self, generation: int, photo: str, reason: str) -> None:
        if generation == self._generation:
            self.failed.emit(photo, reason)

    def shutdown(self) -> None:
        self._generation += 1
        self._pool.clear()
        self._pool.waitForDone(5000)


class PhotoLabel(QLabel):
    """A photograph that redraws itself at whatever size it is given.

    The scaling has to happen here rather than in the parent's resizeEvent:
    when the parent is resized its children have not been laid out yet, so
    asking this label how big it is then answers with its previous size and
    the photograph is drawn to fit a box that no longer exists.
    """

    def __init__(self):
        super().__init__("")
        self.setObjectName("paneImage")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._source: QPixmap | None = None

    def set_source(self, pixmap: QPixmap | None) -> None:
        self._source = pixmap
        if pixmap is None:
            self.setPixmap(QPixmap())
            return
        self.setText("")
        self._redraw()

    def set_message(self, text: str) -> None:
        self._source = None
        self.setPixmap(QPixmap())
        self.setText(text)

    def source(self) -> QPixmap | None:
        return self._source

    def _redraw(self) -> None:
        if self._source is None:
            return
        self.setPixmap(scaled(
            self._source, max(self.width(), 1), max(self.height(), 1)))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._redraw()


class Stage(QFrame):
    """One photograph, as large as the window allows, in two states.

    A treatment is judged by flicking between it and the frame it came
    from: same place, same size, same instant. Two pictures side by side
    are each half the size and the eye still has to travel between them,
    so the comparison happens here in one place and time instead.
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("pane")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._as_shot: QPixmap | None = None
        self._treated: QPixmap | None = None
        self._treatment = ""
        self._holding = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(12)
        self.caption = QLabel("AS SHOT")
        self.caption.setObjectName("paneCaption")
        self.caption.setFont(theme.display(8))
        bar.addWidget(self.caption)
        bar.addStretch(1)

        self.hint = QLabel("")
        self.hint.setObjectName("paneHint")
        self.hint.setFont(theme.body(9))
        bar.addWidget(self.hint)
        layout.addLayout(bar)

        self.image = PhotoLabel()
        layout.addWidget(self.image, 1)

    # --- what there is to show ------------------------------------------

    def set_as_shot(self, pixmap: QPixmap | None) -> None:
        self._as_shot = pixmap
        self._paint()

    def set_treated(self, pixmap: QPixmap | None, treatment: str = "") -> None:
        self._treated = pixmap
        if treatment:
            self._treatment = treatment
        self._paint()

    def set_treatment(self, treatment: str) -> None:
        self._treatment = treatment
        self._paint()

    def set_message(self, text: str) -> None:
        self._as_shot = None
        self._treated = None
        self.image.set_message(text)

    def holding(self) -> bool:
        return self._holding

    def hold(self, holding: bool) -> None:
        """Show the frame as it was shot, for as long as the key is down."""
        if holding == self._holding:
            return
        self._holding = holding
        self._paint()

    def showing(self) -> str:
        """Which of the two is on screen: what the caption has to agree with."""
        if self._treated is not None and not self._holding:
            return "treated"
        return "as shot"

    def _paint(self) -> None:
        treated = self._treated is not None and not self._holding
        pixmap = self._treated if treated else self._as_shot
        if pixmap is None:
            self.image.set_message(
                "Developing…" if self._holding else "Not developed yet.")
        else:
            self.image.set_source(pixmap)
        self.caption.setText(
            (self._treatment or "Developed").upper() if treated else "AS SHOT")
        self.caption.setProperty("state", "treated" if treated else "shot")
        self.caption.style().unpolish(self.caption)
        self.caption.style().polish(self.caption)
        self.hint.setText(
            "" if self._treated is None else
            "Release to return" if self._holding else
            "Hold space to see it as shot")


class DevelopPage(QWidget):
    """Photographs on the left, the comparison in the middle, treatments right."""

    closed = Signal()

    def __init__(self, report, workspace: DevelopmentWorkspace,
                 loader: PreviewLoader, parent: QWidget | None = None):
        super().__init__(parent)
        self.report = report
        self.workspace = workspace
        self.loader = loader
        self.photo_names = list(report.photo_names)
        self.current = self.photo_names[0] if self.photo_names else ""
        self.treatment = ""
        self.available: list[dict] = []
        self.rendered: dict[tuple[str, str], QPixmap] = {}

        self.renderer = Renderer(workspace, self)
        self.renderer.done.connect(self._rendered)
        self.renderer.failed.connect(self._render_failed)
        self.exporter = Exporter(workspace, self)
        self.exporter.done.connect(self._exported)
        self.exporter.failed.connect(self._export_failed)
        self.loader.ready.connect(self._original_ready)

        self._build()
        self._fill_photos()
        if self.current:
            self.show_photo(self.current)

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._bar())

        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)

        self.photos = QListWidget()
        self.photos.setObjectName("clusterList")
        self.photos.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.photos.setFixedWidth(238)
        self.photos.currentRowChanged.connect(self._chose_row)
        split.addWidget(self.photos)

        stage = QWidget()
        stage.setObjectName("page")
        column = QVBoxLayout(stage)
        column.setContentsMargins(18, 16, 18, 16)
        column.setSpacing(12)

        self.stage = Stage()
        column.addWidget(self.stage, 1)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        column.addWidget(self.status)
        split.addWidget(stage, 1)

        split.addWidget(self._panel())
        outer.addLayout(split, 1)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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

        self.title = QLabel(
            workspace_title(self.report, self.workspace.project.get(
                "source_folder")).upper())
        self.title.setObjectName("chromeTitle")
        self.title.setFont(theme.display(11))
        layout.addWidget(self.title)
        layout.addStretch(1)

        self.counter = QLabel("")
        self.counter.setObjectName("hint")
        self.counter.setFont(theme.body(9))
        layout.addWidget(self.counter)
        return bar

    def _panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setFixedWidth(272)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        heading = QLabel("TREATMENT")
        heading.setObjectName("bandTitle")
        heading.setFont(theme.display(8))
        layout.addWidget(heading)

        self.treatments = QListWidget()
        self.treatments.setObjectName("treatmentList")
        # Neither list takes focus: the space bar belongs to the comparison,
        # and a focused list would eat it to toggle its own selection.
        self.treatments.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.treatments.currentRowChanged.connect(self._chose_treatment)
        layout.addWidget(self.treatments)

        self.intent = QLabel("")
        self.intent.setObjectName("hint")
        self.intent.setWordWrap(True)
        self.intent.setFont(theme.body(9))
        layout.addWidget(self.intent)

        self.engine_note = QLabel("")
        self.engine_note.setObjectName("hint")
        self.engine_note.setWordWrap(True)
        self.engine_note.setFont(theme.body(9))
        layout.addWidget(self.engine_note)
        layout.addStretch(1)

        self.develop_button = QPushButton("Develop this frame")
        self.develop_button.setObjectName("primary")
        self.develop_button.setFont(theme.body(10))
        self.develop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.develop_button.clicked.connect(self.develop_current)
        layout.addWidget(self.develop_button)

        self.export_button = QPushButton("Export…")
        self.export_button.setObjectName("ghost")
        self.export_button.setFont(theme.body(10))
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.setToolTip(
            "Render this treatment at the photograph's own size and write a "
            "copy where you choose.")
        self.export_button.clicked.connect(self.export_current)
        layout.addWidget(self.export_button)
        return panel

    # --- photographs -----------------------------------------------------

    def _fill_photos(self) -> None:
        self.photos.blockSignals(True)
        self.photos.clear()
        for name in self.photo_names:
            item = QListWidgetItem(f"  {name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.photos.addItem(item)
        self.photos.blockSignals(False)
        self.counter.setText(
            f"{len(self.photo_names)} photograph"
            f"{'' if len(self.photo_names) == 1 else 's'}")

    def _chose_row(self, row: int) -> None:
        if 0 <= row < len(self.photo_names):
            self.show_photo(self.photo_names[row])
        self.setFocus()

    def show_photo(self, name: str) -> None:
        self.current = name
        # Whatever is still rendering belongs to the frame we just left.
        self.renderer.abandon()
        self.stage.hold(False)
        self.stage.set_message("…")
        pixmap = self.loader.request(name, "detail")
        if pixmap is not None:
            self.stage.set_as_shot(pixmap)

        row = self.photo_names.index(name)
        if self.photos.currentRow() != row:
            self.photos.blockSignals(True)
            self.photos.setCurrentRow(row)
            self.photos.blockSignals(False)
        self._fill_treatments()
        self._show_treated()

    def _original_ready(self, name: str, size: str, pixmap) -> None:
        if name == self.current and size == "detail":
            self.stage.set_as_shot(pixmap)

    # --- treatments ------------------------------------------------------

    def _fill_treatments(self) -> None:
        try:
            available = self.workspace.treatments(self.current)
        except Exception as exc:
            available = []
            self._report(str(exc), "alarm")
        self.available = available
        previous = self.treatment
        self.treatments.blockSignals(True)
        self.treatments.clear()
        for item in available:
            entry = QListWidgetItem(f"  {item['name']}")
            entry.setData(Qt.ItemDataRole.UserRole, item["id"])
            entry.setToolTip(item.get("intent", ""))
            entry.setSizeHint(QSize(0, TREATMENT_ROW))
            self.treatments.addItem(entry)
        self.treatments.blockSignals(False)
        self._size_treatments()
        ids = [item["id"] for item in available]
        row = ids.index(previous) if previous in ids else 0
        if available:
            self.treatments.setCurrentRow(row)
            self._chose_treatment(row)
        else:
            self.treatment = ""
            self.intent.setText(
                "This photograph has no treatment available.")
            self.develop_button.setEnabled(False)

    def _size_treatments(self) -> None:
        """Let the list be as tall as its contents and no taller.

        Two treatments in a panel-high box reads as a list that failed to
        load the rest.
        """
        rows = max(self.treatments.count(), 1)
        # The list's own 4px padding top and bottom, and its 1px border.
        self.treatments.setFixedHeight(rows * TREATMENT_ROW + 10)

    def _chose_treatment(self, row: int) -> None:
        if not (0 <= row < len(self.available)):
            return
        chosen = self.available[row]
        self.treatment = str(chosen["id"])
        self.intent.setText(str(chosen.get("intent") or ""))
        self.develop_button.setEnabled(True)
        self.engine_note.setText(self._engine_note())
        self._show_treated()

    def decoder_for(self, photo: str) -> dict:
        # The workspace decides, so what this says and what the renderer then
        # does are the same answer rather than two detections that can differ.
        return self.workspace.decoder_for(photo)

    def engine_for(self, photo: str) -> str:
        return str(self.decoder_for(photo)["engine"])

    def _engine_note(self) -> str:
        """Say which rendering this actually is. They are not equivalent."""
        return str(self.decoder_for(self.current)["note"])

    # --- rendering -------------------------------------------------------

    def _show_treated(self) -> None:
        if not self.current or not self.treatment:
            self.stage.set_treated(None)
            return
        cached = self.rendered.get((self.current, self.treatment))
        self.stage.set_treated(cached, self._treatment_name())
        if cached is not None:
            self._report("")

    def _treatment_name(self) -> str:
        return next(
            (str(item["name"]) for item in self.available
             if item["id"] == self.treatment), "Developed")

    def _demosaic(self) -> str:
        return str((self.workspace.payload().get("rendering") or {}).get(
            "demosaic") or "markesteijn-3-pass")

    def develop_current(self) -> None:
        if not self.current or not self.treatment:
            return
        if (self.current, self.treatment) in self.rendered:
            self._show_treated()
            return
        self.develop_button.setEnabled(False)
        self._report(f"Developing {self.current}. This takes a moment.")
        self.renderer.render(
            self.current, self.treatment, self.engine_for(self.current),
            self._demosaic())

    def _rendered(self, photo: str, treatment: str, pixmap) -> None:
        self.rendered[(photo, treatment)] = pixmap
        self.develop_button.setEnabled(True)
        if photo == self.current and treatment == self.treatment:
            self.stage.set_treated(pixmap, self._treatment_name())
            self._report(
                "Developed. Hold space to see it as shot; the photograph "
                "itself is unchanged.", "ok")

    def _render_failed(self, photo: str, reason: str) -> None:
        self.develop_button.setEnabled(True)
        if photo == self.current:
            self._report(reason, "alarm")

    # --- export ---------------------------------------------------------

    def export_directory(self) -> Path:
        return Path(str(
            self.workspace.payload().get("default_export_directory") or
            Path.home()))

    def export_current(self) -> None:
        """Deliver this treatment at the photograph's own size."""
        if not self.current or not self.treatment:
            return
        suggested = suggested_filename(self.current, self._variant())
        directory = self.export_directory()
        directory.mkdir(parents=True, exist_ok=True)
        chosen, _filter = QFileDialog.getSaveFileName(
            self, "Export this frame", str(directory / suggested),
            "JPEG image (*.jpg *.jpeg)",
            # Qt's own prompt promises to replace an existing file, and that
            # is a promise this does not keep: an export never writes over
            # something already there, it takes the next free name and says
            # which one it used.
            options=QFileDialog.Option.DontConfirmOverwrite)
        if not chosen:
            return
        self.export_button.setEnabled(False)
        self._report(
            f"Exporting {self.current} at full size. This is a longer job "
            "than the proof on screen.")
        self.exporter.export(
            self.current, self.treatment, self.engine_for(self.current),
            self._demosaic(), chosen)

    def _variant(self) -> str:
        engine = self.engine_for(self.current)
        return (self.treatment if engine == "default"
                else f"{self.treatment}-darktable-guided")

    def _exported(self, photo: str, requested: str, written: str) -> None:
        self.export_button.setEnabled(self.exporter.pending == 0)
        where = Path(written)
        if written != str(Path(requested).expanduser().resolve()):
            self._report(
                f"{photo} was written as {where.name}, because "
                f"{Path(requested).name} was already there and Darkimiya "
                "does not write over a file.", "ok")
            return
        self._report(f"{photo} was exported to {short_path(str(where))}.", "ok")

    def _export_failed(self, photo: str, reason: str) -> None:
        self.export_button.setEnabled(self.exporter.pending == 0)
        self._report(f"{photo} could not be exported: {reason}", "alarm")

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def step(self, delta: int) -> None:
        if not self.photo_names:
            return
        row = self.photo_names.index(self.current) + delta
        if 0 <= row < len(self.photo_names):
            self.show_photo(self.photo_names[row])

    def choose_treatment(self, index: int) -> None:
        if 0 <= index < len(self.available):
            self.treatments.setCurrentRow(index)
            self._chose_treatment(index)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key == Qt.Key.Key_Space:
            # A held key repeats; the first press is the only one that means
            # anything, and the repeats must not be read as releases.
            if not event.isAutoRepeat():
                self.stage.hold(True)
        elif Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            self.choose_treatment(key - Qt.Key.Key_1)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.step(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.step(-1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_D):
            self.develop_current()
        elif key == Qt.Key.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.stage.hold(False)
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # A key released while another window has the keyboard never reaches
        # here, and the comparison would stay held down for good.
        self.stage.hold(False)
        super().focusOutEvent(event)

    def shutdown(self) -> None:
        self.renderer.shutdown()
        self.exporter.shutdown()




def shortlist_path_for(report, layout: dict) -> Path:
    """Where this report's assessment lives, by convention."""
    return layout["Reports"] / f"{report.path.stem}.professional-shortlist.json"


def directions_for(report, photos_root: Path, layout: dict,
                   project_path: Path):
    """The edit directions for this folder, if it has been through assessment.

    A folder that has only been culled has no shortlist, so it has no
    directions and no treatments beyond the baseline. That is the truth
    rather than an error, so this answers None and the workspace carries on.
    """
    from opencull_gui.directions import DirectionsIndex
    from opencull_gui.project import load_project
    from opencull_gui.shortlist import load_shortlist
    from opencull_gui.shortlist_reviews import (
        ShortlistReviewStore,
        default_shortlist_review_path,
    )

    path = shortlist_path_for(report, layout)
    if not path.is_file():
        return None
    try:
        shortlist = load_shortlist(path, report, photos_root)
        reviews = ShortlistReviewStore(
            default_shortlist_review_path(path), shortlist)
    except Exception:
        # A shortlist belonging to a different cull is not this folder's
        # assessment. Treating it as absent is right; refusing to open the
        # develop page over it is not.
        return None
    index = DirectionsIndex(
        shortlist, reviews, layout["Recipes"],
        style_profile=lambda: str(
            load_project(project_path).get("active_style_profile") or ""))
    return index.payload


def workspace_for(report, photos_root: Path,
                  decoders: set[str] | None = None) -> DevelopmentWorkspace:
    """Build a develop stage for one folder, the way the server builds one."""
    from opencull_gui.project import (
        ensure_project_layout,
        load_or_create_folder_project,
    )
    from opencull_gui.raw_sources import RawSourceStore

    project_path, _ = load_or_create_folder_project(
        photos_root, report.path.stem,
        report.path.with_suffix(".opencull-project.json"))
    layout = ensure_project_layout(photos_root)
    raw_sources = RawSourceStore(
        layout["Reports"] / f"{report.path.stem}.raw-source.json", report)
    return DevelopmentWorkspace(
        project_path, layout, raw_sources, decoders=decoders,
        directions=directions_for(report, photos_root, layout, project_path))
