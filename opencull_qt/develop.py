"""The develop page: see a photograph rendered, beside the one you shot.

Development is a comparison. A treatment is only worth anything relative to
what was already there, so the two are always on screen together rather than
one replacing the other.

Rendering is slow enough to be felt -- a demosaic and a recipe over a whole
frame -- so it happens on a worker thread. A full proof is still only made
when the photographer asks for one; what opening a frame buys is a set of
small previews, one per treatment, because choosing between four written
arguments about a photograph is not the same as choosing between four
pictures of it. They are cached on disk at their own size, so a frame
already looked at costs nothing to look at again.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
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

from opencull_gui.development import (
    BUILTIN_STYLES,
    DevelopmentWorkspace,
    suggested_filename,
)

from . import theme
from .previews import PreviewLoader, plain_icon, scaled
from .widgets import short_path, workspace_title

# A proof, not a delivery. Big enough to judge a treatment on a laptop
# screen, small enough that a demosaic finishes while you are still looking
# at the frame.
PROOF_EDGE = 1600

# Set on the items themselves. A row's painted height comes from the
# stylesheet, which sizeHintForRow does not know about, so asking it how tall
# the list should be returns a box that clips its own contents.
TREATMENT_ROW = 34

# Room for a thumbnail and the name beside it, in both lists.
PHOTO_ROW = 74
TREATMENT_TILE = 84

# Small enough that five of them for one frame is a wait somebody will
# sit through, large enough to tell two treatments apart. Cached on disk
# by the workspace under this exact size, so a frame revisited is instant.
THUMB_EDGE = 320

# Long enough to read as the edit being applied, short enough
# that somebody comparing ten treatments is not waiting on it.
SWEEP_MS = 620


class _Signals(QObject):
    done = Signal(int, str, str, str)     # generation, photo, treatment, path
    failed = Signal(int, str, str)        # generation, photo, reason


class _RenderJob(QRunnable):
    def __init__(self, workspace: DevelopmentWorkspace, photo: str,
                 treatment: str, engine: str, demosaic: str,
                 signals: _Signals, generation: int,
                 maximum: int = 0, adjustments: dict | None = None):
        super().__init__()
        self.workspace = workspace
        self.photo = photo
        self.treatment = treatment
        self.engine = engine
        self.demosaic = demosaic
        self.signals = signals
        self.generation = generation
        self.maximum = maximum or PROOF_EDGE
        self.adjustments = adjustments
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            path = self.workspace.recipe_preview(
                self.photo, self.treatment, self.engine, self.demosaic,
                self.maximum, self.adjustments)
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


class _VerifySignals(QObject):
    ready = Signal(str, str, str)         # photo, variant, render path
    failed = Signal(str, str)             # photo, reason


class _VerifyJob(QRunnable):
    def __init__(self, workspace: DevelopmentWorkspace, photo: str,
                 treatment: str, engine: str, demosaic: str,
                 signals: _VerifySignals):
        super().__init__()
        self.workspace = workspace
        self.photo = photo
        self.treatment = treatment
        self.engine = engine
        self.demosaic = demosaic
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            # The certificate is bound to the bytes it judged, so what is
            # verified has to be the file that would be delivered rather
            # than the bounded proof on screen.
            result = self.workspace.render_full(
                self.photo, self.treatment, self.engine, self.demosaic)
        except Exception as exc:
            self.signals.failed.emit(self.photo, str(exc))
            return
        self.signals.ready.emit(
            self.photo, str(result["render"]["variant"]),
            str(result["render"]["path"]))


class Verifier(QObject):
    """Prepare the file a verification will judge."""

    ready = Signal(str, str, str)
    failed = Signal(str, str)

    def __init__(self, workspace: DevelopmentWorkspace,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.workspace = workspace
        self.pending = 0
        self._signals = _VerifySignals()
        self._signals.ready.connect(self._done)
        self._signals.failed.connect(self._failed)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def prepare(self, photo: str, treatment: str, engine: str,
                demosaic: str) -> None:
        self.pending += 1
        self._pool.start(_VerifyJob(
            self.workspace, photo, treatment, engine, demosaic,
            self._signals))

    def _done(self, photo: str, variant: str, path: str) -> None:
        self.pending = max(0, self.pending - 1)
        self.ready.emit(photo, variant, path)

    def _failed(self, photo: str, reason: str) -> None:
        self.pending = max(0, self.pending - 1)
        self.failed.emit(photo, reason)

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(20000)


class Renderer(QObject):
    """One render at a time, and never a stale one."""

    done = Signal(str, str, QPixmap)
    failed = Signal(str, str)

    def __init__(self, workspace: DevelopmentWorkspace,
                 maximum: int = PROOF_EDGE,
                 pool: QThreadPool | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.workspace = workspace
        self.maximum = maximum
        self._generation = 0
        self._signals = _Signals()
        self._signals.done.connect(self._finished)
        self._signals.failed.connect(self._failed)
        self._pool = pool or QThreadPool(self)
        # A render saturates the machine on its own. Two at once makes both
        # slower and neither useful sooner.
        self._pool.setMaxThreadCount(1)

    def render(self, photo: str, treatment: str, engine: str,
               demosaic: str, adjustments: dict | None = None) -> None:
        self._generation += 1
        self._pool.start(_RenderJob(
            self.workspace, photo, treatment, engine, demosaic,
            self._signals, self._generation, self.maximum, adjustments))

    def render_many(self, photo: str, treatments: list[str], engine: str,
                    demosaic: str) -> None:
        """Every treatment of one frame, as one batch of work.

        The single-render path treats each new request as the only one that
        matters, which is right for a proof and wrong for a set of
        thumbnails: they belong to the same frame and all of them are
        wanted. One generation covers the batch, so moving to another frame
        still abandons the lot.
        """
        self._generation += 1
        for treatment in treatments:
            self._pool.start(_RenderJob(
                self.workspace, photo, treatment, engine, demosaic,
                self._signals, self._generation, self.maximum))

    def abandon(self) -> None:
        """Stop caring about a render whose frame is no longer on screen."""
        self._generation += 1

    def drop_queued(self) -> None:
        """Give up the work not yet started, and the right to its results.

        A render already running is left alone -- killing a decoder halfway
        wastes what it has done and leaves its working files behind.
        """
        self._generation += 1
        self._pool.clear()

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


class PreviewQueue(QObject):
    """Every treatment of every frame, rendered quietly, one at a time.

    A photographer picking a treatment for the twentieth frame should not
    wait for it to be rendered while looking at it. The queue works ahead
    through the whole selection, but only ever holds one job in the pool:
    a proof asked for now must not queue behind a hundred thumbnails, and
    a pool cleared of them would forget the work anyway.

    Nothing here is abandoned. A thumbnail is worth having whenever it
    arrives, whatever frame is on screen by then.
    """

    ready = Signal(str, str, QPixmap)     # photo, treatment, picture
    progressed = Signal(int, int)         # done, total

    def __init__(self, workspace: DevelopmentWorkspace, maximum: int,
                 pool: QThreadPool, parent: QObject | None = None):
        super().__init__(parent)
        self.workspace = workspace
        self.maximum = maximum
        self._pool = pool
        self._pending: list[tuple[str, str]] = []
        self._asked: set[tuple[str, str]] = set()
        self._running = False
        self._done = 0
        self._signals = _Signals()
        self._signals.done.connect(self._finished)
        self._signals.failed.connect(self._failed)
        self._settings: dict[str, tuple[str, str]] = {}
        self._stopped = False

    def want(self, pairs, settings) -> None:
        """Add work, skipping anything already asked for."""
        self._settings.update(settings)
        for pair in pairs:
            if pair in self._asked:
                continue
            self._asked.add(pair)
            self._pending.append(pair)
        self._pump()

    def prefer(self, photo: str) -> None:
        """Put one frame's remaining previews at the front of the queue."""
        self._pending.sort(key=lambda pair: pair[0] != photo)

    def outstanding(self) -> int:
        return len(self._pending)

    def _pump(self) -> None:
        if self._stopped or self._running or not self._pending:
            return
        photo, treatment = self._pending.pop(0)
        engine, demosaic = self._settings.get(photo, ("", ""))
        if not engine:
            self._pump()
            return
        self._running = True
        self._pool.start(_RenderJob(
            self.workspace, photo, treatment, engine, demosaic,
            self._signals, 0, self.maximum))

    def _step(self) -> None:
        self._running = False
        self._done += 1
        self.progressed.emit(self._done, self._done + len(self._pending))
        self._pump()

    def _finished(self, _generation: int, photo: str, treatment: str,
                  path: str) -> None:
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            self.ready.emit(photo, treatment, pixmap)
        self._step()

    def _failed(self, _generation: int, _photo: str, _reason: str) -> None:
        # A preview that will not render is not worth stopping the sweep
        # for; asking for its proof will report the same failure where it
        # can actually be acted on.
        self._step()

    def stop(self) -> None:
        self._stopped = True
        self._pending.clear()


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
        self._sweep: QVariantAnimation | None = None
        self._before: QPixmap | None = None
        self._after: QPixmap | None = None

    def set_source(self, pixmap: QPixmap | None) -> None:
        self._source = pixmap
        if pixmap is None:
            self.setPixmap(QPixmap())
            return
        self.setText("")
        self._redraw()

    def sweep_to(self, pixmap: QPixmap | None) -> None:
        """Show the treatment arriving across the frame it was made from.

        Not decoration: the two pictures in the wipe are the real ones, the
        same before and after the animation ends, and the edge between them
        is where a photographer looks to see what actually changed. A frame
        with nothing to wipe from just appears.
        """
        previous = self._source
        if pixmap is None or previous is None or self.width() < 2:
            self.set_source(pixmap)
            return
        self._before = scaled(previous, max(self.width(), 1),
                              max(self.height(), 1))
        self._after = scaled(pixmap, max(self.width(), 1),
                             max(self.height(), 1))
        if self._before.size() != self._after.size():
            # Different shapes cannot be wiped honestly between.
            self.set_source(pixmap)
            return
        self._source = pixmap
        self.setText("")
        if self._sweep is not None:
            self._sweep.stop()
        self._sweep = QVariantAnimation(self)
        self._sweep.setDuration(SWEEP_MS)
        self._sweep.setStartValue(0.0)
        self._sweep.setEndValue(1.0)
        self._sweep.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._sweep.valueChanged.connect(self._sweep_tick)
        self._sweep.finished.connect(self._sweep_done)
        self._sweep.start()

    def _sweep_tick(self, value) -> None:
        if self._before is None or self._after is None:
            return
        frame = QPixmap(self._after.size())
        frame.setDevicePixelRatio(self._after.devicePixelRatio())
        painter = QPainter(frame)
        painter.drawPixmap(0, 0, self._before)
        edge = int(frame.width() / frame.devicePixelRatio() * float(value))
        painter.setClipRect(0, 0, edge, frame.height())
        painter.drawPixmap(0, 0, self._after)
        painter.setClipping(False)
        if 0 < edge < frame.width() / frame.devicePixelRatio():
            pen = QPen(QColor(theme.SAFELIGHT))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(
                edge, 0, edge, int(frame.height() / frame.devicePixelRatio()))
        painter.end()
        self.setPixmap(frame)

    def _sweep_done(self) -> None:
        self._sweep = None
        self._before = None
        self._after = None
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

    def set_treated(self, pixmap: QPixmap | None, treatment: str = "",
                    arriving: bool = False) -> None:
        """Show a treatment. ``arriving`` wipes it over the frame as shot."""
        was = self._treated
        self._treated = pixmap
        if treatment:
            self._treatment = treatment
        if (arriving and pixmap is not None and not self._holding
                and was is not pixmap):
            self._caption()
            self.image.sweep_to(pixmap)
            return
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
        self._caption()

    def _caption(self) -> None:
        treated = self._treated is not None and not self._holding
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
    verification_wanted = Signal(dict)
    why_wanted = Signal(str)        # the frame whose story is asked for

    def __init__(self, report, workspace: DevelopmentWorkspace,
                 loader: PreviewLoader, parent: QWidget | None = None):
        super().__init__(parent)
        self.report = report
        self.workspace = workspace
        self.loader = loader
        self.photo_names = list(report.photo_names)
        # The list holds the frames there is something to decide about
        # until asked to hold them all.
        self.scope = "treated"
        self.current = self.photo_names[0] if self.photo_names else ""
        self.treatment = ""
        self.available: list[dict] = []
        self.rendered: dict[tuple[str, str], QPixmap] = {}

        # One pool for both renderers. A render saturates the machine on
        # its own, and two writing previews into the same folder at once
        # was a race: proofs and thumbnails queue rather than compete.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self.renderer = Renderer(workspace, pool=self._pool, parent=self)
        self.renderer.done.connect(self._rendered)
        self.renderer.failed.connect(self._render_failed)
        # The smaller renderer: what each treatment does to each frame,
        # shown rather than described, worked through in the background.
        self.thumbs = PreviewQueue(
            workspace, THUMB_EDGE, self._pool, parent=self)
        self.thumbs.ready.connect(self._thumb_ready)
        self.thumbs.progressed.connect(self._thumb_progress)
        self.previews: dict[tuple[str, str], QPixmap] = {}
        self.exporter = Exporter(workspace, self)
        self.exporter.done.connect(self._exported)
        self.exporter.failed.connect(self._export_failed)
        self.verifier = Verifier(workspace, self)
        self.verifier.ready.connect(self._verification_ready)
        self.verifier.failed.connect(self._verify_failed)
        self.loader.ready.connect(self._original_ready)

        self._build()
        self._fill_photos()
        # Open on a frame the list is actually holding: the first treated
        # one, or the first of the folder when nothing has been suggested.
        shown = self.shown_photos()
        if shown:
            self.show_photo(shown[0])

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.bar = self._bar()
        outer.addWidget(self.bar)

        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)

        # A folder of two hundred frames, of which thirty-nine have
        # treatments, is a list where the work is hidden among frames that
        # only have the baseline. The frames with something to choose
        # between come first and alone; the rest stay one click away,
        # because the baseline is theirs to render whenever they want it.
        rail = QWidget()
        rail.setObjectName("page")
        rail.setFixedWidth(258)
        rail_column = QVBoxLayout(rail)
        rail_column.setContentsMargins(0, 0, 0, 0)
        rail_column.setSpacing(0)

        # Two buttons rather than a dropdown: a choice between two things
        # is not worth a menu, and a combo box is drawn by the platform
        # rather than by this application, so it arrives wearing somebody
        # else's colours.
        self.scope_row = QWidget()
        self.scope_row.setObjectName("chrome")
        scope_layout = QHBoxLayout(self.scope_row)
        scope_layout.setContentsMargins(10, 7, 10, 7)
        scope_layout.setSpacing(6)
        self.scope_buttons: dict[str, QPushButton] = {}
        for key, tip in (
            ("treated", "The frames a treatment was written for."),
            ("all", "Every frame in the folder. All of them can be "
                    "rendered from the calibrated baseline."),
        ):
            button = QPushButton("")
            button.setObjectName("tier")
            button.setCheckable(True)
            button.setFont(theme.body(9))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tip)
            button.clicked.connect(
                lambda _=False, value=key: self.set_scope(value))
            self.scope_buttons[key] = button
            scope_layout.addWidget(button)
        scope_layout.addStretch(1)
        rail_column.addWidget(self.scope_row)

        self.photos = QListWidget()
        self.photos.setObjectName("clusterList")
        self.photos.setIconSize(QSize(96, 64))
        self.photos.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.photos.currentRowChanged.connect(self._chose_row)
        rail_column.addWidget(self.photos, 1)
        split.addWidget(rail)

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
        self.indicator = self.counter
        return bar

    def _panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setFixedWidth(344)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        heading = QLabel("TREATMENT")
        heading.setObjectName("bandTitle")
        heading.setFont(theme.display(8))
        head.addWidget(heading)
        head.addStretch(1)
        why = QPushButton("Why?")
        why.setObjectName("ghost")
        why.setFont(theme.body(9))
        why.setCursor(Qt.CursorShape.PointingHandCursor)
        why.setToolTip("The recorded story of this frame.")
        why.clicked.connect(lambda: self.why_wanted.emit(self.current))
        head.addWidget(why)
        layout.addLayout(head)

        self.treatments = QListWidget()
        self.treatments.setObjectName("treatmentList")
        self.treatments.setIconSize(QSize(112, 72))
        # Neither list takes focus: the space bar belongs to the comparison,
        # and a focused list would eat it to toggle its own selection.
        self.treatments.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.treatments.currentRowChanged.connect(self._chose_treatment)
        # Choosing a treatment by hand is asking to see it at proof size.
        # Moving between frames selects one too, and that must not spend a
        # full render nobody asked for, so only the click renders.
        self.treatments.itemClicked.connect(
            lambda _item: self.develop_current(arriving=True))
        layout.addWidget(self.treatments)

        self.intent = QLabel("")
        self.intent.setObjectName("hint")
        self.intent.setWordWrap(True)
        self.intent.setFont(theme.body(9))
        layout.addWidget(self.intent)

        self.verdict = QLabel("")
        self.verdict.setObjectName("verdict")
        self.verdict.setWordWrap(True)
        self.verdict.setFont(theme.body(9))
        self.verdict.setVisible(False)
        layout.addWidget(self.verdict)

        self.engine_note = QLabel("")
        self.engine_note.setObjectName("hint")
        self.engine_note.setWordWrap(True)
        self.engine_note.setFont(theme.body(9))
        layout.addWidget(self.engine_note)

        self.sweep_note = QLabel("")
        self.sweep_note.setObjectName("hint")
        self.sweep_note.setWordWrap(True)
        self.sweep_note.setFont(theme.body(9))
        self.sweep_note.setVisible(False)
        layout.addWidget(self.sweep_note)
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

        self.verify_button = QPushButton("Verify…")
        self.verify_button.setObjectName("ghost")
        self.verify_button.setFont(theme.body(10))
        self.verify_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.verify_button.setToolTip(
            "Ask a model whether this rendering did what the treatment said "
            "it would, without losing the subject.")
        self.verify_button.clicked.connect(self.verify_current)
        layout.addWidget(self.verify_button)
        return panel

    # --- photographs -----------------------------------------------------

    def treated_photos(self) -> list[str]:
        """The frames a treatment was actually written for, in shot order."""
        try:
            candidates = self.workspace.payload().get("candidates", [])
        except Exception:                            # noqa: BLE001 - absent
            return []
        named = {
            str(item.get("photo")) for item in candidates
            if isinstance(item, dict) and any(
                str(item.get(f"{style}_recipe") or "").strip()
                for style in BUILTIN_STYLES if style != "calibrated")}
        return [name for name in self.photo_names if name in named]

    def shown_photos(self) -> list[str]:
        treated = self.treated_photos()
        if not treated:
            # Nothing has been suggested yet, so "with treatments" would be
            # an empty page offering a filter to escape itself.
            return list(self.photo_names)
        return list(self.photo_names) if self.scope == "all" else treated

    def _sync_row(self) -> None:
        """Keep the list's highlight on the frame being shown, if it is here.

        The frame stays open when the list stops holding it -- narrowing to
        the treated frames should not throw away the one being looked at.
        """
        shown = self.shown_photos()
        row = shown.index(self.current) if self.current in shown else -1
        if self.photos.currentRow() != row:
            self.photos.blockSignals(True)
            self.photos.setCurrentRow(row)
            self.photos.blockSignals(False)

    def set_scope(self, scope: str) -> None:
        self.scope = scope if scope in {"treated", "all"} else "treated"
        self._fill_photos()
        shown = self.shown_photos()
        if shown and self.current not in shown:
            self.show_photo(shown[0])
        else:
            self._sync_row()

    def _fill_photos(self) -> None:
        treated = self.treated_photos()
        total = len(self.photo_names)
        if not treated:
            self.scope = "all"
        self.scope_row.setVisible(bool(treated))
        self.scope_buttons["treated"].setText(f"Treated · {len(treated)}")
        self.scope_buttons["all"].setText(f"All · {total}")
        for key, button in self.scope_buttons.items():
            button.setChecked(key == self.scope)

        shown = self.shown_photos()
        self.photos.blockSignals(True)
        self.photos.clear()
        for name in shown:
            # A photograph is recognised by its picture; the filename is
            # what you read once you have found it.
            item = QListWidgetItem(f"  {name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setSizeHint(QSize(0, PHOTO_ROW))
            pixmap = self.loader.request(name, "thumb")
            if pixmap is not None:
                item.setIcon(plain_icon(pixmap))
            self.photos.addItem(item)
        self.photos.blockSignals(False)
        self.counter.setText(
            f"{len(shown)} photograph{'' if len(shown) == 1 else 's'}"
            + (f" of {total}" if len(shown) != total else ""))

    def _chose_row(self, row: int) -> None:
        shown = self.shown_photos()
        if 0 <= row < len(shown):
            self.show_photo(shown[row])
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

        self._sync_row()
        self._fill_treatments()
        self.thumbs.prefer(name)
        self._show_treated()

    def _original_ready(self, name: str, size: str, pixmap) -> None:
        if name == self.current and size == "detail":
            self.stage.set_as_shot(pixmap)
        if size != "thumb":
            return
        for row in range(self.photos.count()):
            item = self.photos.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                item.setIcon(plain_icon(pixmap))
                break

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
            entry.setToolTip(
                f"{item['name']}\n{item.get('intent', '')}".strip())
            entry.setSizeHint(QSize(0, TREATMENT_TILE))
            preview = self.previews.get((self.current, str(item["id"])))
            if preview is not None:
                entry.setIcon(plain_icon(preview))
            self.treatments.addItem(entry)
        self.treatments.blockSignals(False)
        self._request_previews()
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
        self.treatments.setFixedHeight(rows * TREATMENT_TILE + 10)

    def _chose_treatment(self, row: int) -> None:
        if not (0 <= row < len(self.available)):
            return
        chosen = self.available[row]
        self.treatment = str(chosen["id"])
        self.intent.setText(str(chosen.get("intent") or ""))
        self.develop_button.setEnabled(True)
        self.engine_note.setText(self._engine_note())
        self._show_treated()
        self._show_verdict()

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

    def _show_treated(self, arriving: bool = False) -> None:
        if not self.current or not self.treatment:
            self.stage.set_treated(None)
            return
        cached = self.rendered.get((self.current, self.treatment))
        self.stage.set_treated(
            cached, self._treatment_name(), arriving=arriving)
        if cached is not None:
            self._report("")

    def _treatment_name(self) -> str:
        return next(
            (str(item["name"]) for item in self.available
             if item["id"] == self.treatment), "Developed")

    def _demosaic(self) -> str:
        return str((self.workspace.payload().get("rendering") or {}).get(
            "demosaic") or "markesteijn-3-pass")

    def develop_current(self, arriving: bool = False) -> None:
        if not self.current or not self.treatment:
            return
        self._arriving = bool(arriving)
        if (self.current, self.treatment) in self.rendered:
            self._show_treated(arriving=arriving)
            return
        self.develop_button.setEnabled(False)
        self._report(f"Developing {self.current}. This takes a moment.")
        self.renderer.render(
            self.current, self.treatment, self.engine_for(self.current),
            self._demosaic())

    def _request_previews(self) -> None:
        """Queue what each treatment does to every frame on the list.

        The proof stays something the photographer asks for; these are the
        pictures the asking is a choice between, so the whole selection is
        worked through rather than only the frame in front of you. The
        frame in front of you goes first. Anything already rendered at
        this size is read from the workspace's cache rather than decoded
        again.
        """
        demosaic = self._demosaic()
        pairs: list[tuple[str, str]] = []
        settings: dict[str, tuple[str, str]] = {}
        for photo in self.shown_photos():
            try:
                treatments = self.workspace.treatments(photo)
            except Exception:                        # noqa: BLE001 - skipped
                continue
            if len(treatments) < 2:
                # Only the baseline: nothing to choose between, so nothing
                # to render ahead of being asked.
                continue
            settings[photo] = (self.engine_for(photo), demosaic)
            pairs.extend(
                (photo, str(item["id"])) for item in treatments
                if (photo, str(item["id"])) not in self.previews)
        self.thumbs.want(pairs, settings)
        if self.current:
            self.thumbs.prefer(self.current)

    def _thumb_ready(self, photo: str, treatment: str, pixmap) -> None:
        self.previews[(photo, treatment)] = pixmap
        if photo != self.current:
            return
        for row in range(self.treatments.count()):
            entry = self.treatments.item(row)
            if entry.data(Qt.ItemDataRole.UserRole) == treatment:
                entry.setIcon(plain_icon(pixmap))
                break

    def _thumb_progress(self, done: int, total: int) -> None:
        outstanding = self.thumbs.outstanding()
        self.sweep_note.setVisible(bool(outstanding))
        self.sweep_note.setText(
            f"Rendering previews · {done} of {total}. They are kept, so "
            "this happens once." if outstanding else "")

    def _rendered(self, photo: str, treatment: str, pixmap) -> None:
        self.rendered[(photo, treatment)] = pixmap
        self.develop_button.setEnabled(True)
        if photo == self.current and treatment == self.treatment:
            self.stage.set_treated(
                pixmap, self._treatment_name(),
                arriving=getattr(self, "_arriving", False))
            self._show_verdict()
            self._report(
                "Developed. Hold space to see it as shot; the photograph "
                "itself is unchanged.", "ok")

    def _render_failed(self, photo: str, reason: str) -> None:
        self.develop_button.setEnabled(True)
        if photo == self.current:
            self._report(reason, "alarm")

    # --- verification ---------------------------------------------------

    def suggestion(self) -> str:
        if not self.current or not self.treatment:
            return ""
        return self.workspace.suggestion_for(self.current, self.treatment)

    def _show_verdict(self) -> None:
        """Say whether this rendering has been checked, and what was found."""
        suggestion = self.suggestion()
        self.verify_button.setEnabled(bool(suggestion))
        self.verify_button.setToolTip(
            "Ask a model whether this rendering did what the treatment said "
            "it would, without losing the subject."
            if suggestion else
            "The baseline is asked to do nothing, so there is no claim to "
            "check.")
        certificate = None
        if suggestion:
            record = self.workspace.render_record(self.current, self._variant())
            if record is not None:
                certificate = self.workspace.verification_for(record["path"])
        if certificate is None:
            self.verdict.setVisible(False)
            return
        judgment = certificate.get("judgment", {}) or {}
        satisfactory = bool(judgment.get("satisfactory"))
        concerns = [str(item) for item in judgment.get("concerns", []) or []]
        votes = ""
        if judgment.get("votes_total"):
            votes = (f"  ({judgment['votes_satisfactory']} of "
                     f"{judgment['votes_total']} models)")
        text = (
            f"{'Verified' if satisfactory else 'Not satisfied'}{votes}. "
            f"{str(judgment.get('reasoning') or '').strip()}")
        if concerns:
            text += "\n\nConcerns: " + "; ".join(concerns)
        self.verdict.setText(text)
        self.verdict.setProperty("tone", "ok" if satisfactory else "alarm")
        self.verdict.style().unpolish(self.verdict)
        self.verdict.style().polish(self.verdict)
        self.verdict.setVisible(True)

    def verify_current(self) -> None:
        """Have this rendering judged against what the treatment promised."""
        suggestion = self.suggestion()
        if not suggestion:
            return
        self.verify_button.setEnabled(False)
        self._report(
            f"Rendering {self.current} at full size to verify it. The "
            "certificate covers the file that would be delivered, not the "
            "proof on screen.")
        self.verifier.prepare(
            self.current, self.treatment, self.engine_for(self.current),
            self._demosaic())

    def _verification_ready(self, photo: str, variant: str, path: str) -> None:
        self.verify_button.setEnabled(True)
        self.verification_wanted.emit({
            "photo": photo,
            "variant": variant,
            "developed": path,
            "original": str(self.workspace.source_for(photo)),
            "suggestion": self.workspace.suggestion_for(photo, self.treatment),
        })

    def _verify_failed(self, photo: str, reason: str) -> None:
        self.verify_button.setEnabled(True)
        self._report(f"{photo} could not be verified: {reason}", "alarm")

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
        self.thumbs.stop()
        self.renderer.shutdown()
        self.exporter.shutdown()
        self.verifier.shutdown()




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
