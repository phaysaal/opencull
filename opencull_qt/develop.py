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

import json
from pathlib import Path

from PySide6.QtCore import (
    QRectF,
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
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
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
from opencull_gui.directions import verdict_of

from . import theme
from .colour import load_for_screen
from .previews import PreviewLoader, plain_icon, scaled
from .widgets import Stamp, short_path, tooltip, workspace_title

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
# The presets heading is a row of text, not a row with a picture in it.
PRESETS_HEADING = 30
# The fewest tiles the list is allowed to shrink to, whatever the window
# is doing. Past whatever the panel can spare it scrolls, because the
# button that develops the chosen treatment lives below it and must not
# be pushed off the bottom.
TILES_SHOWN_LEAST = 4
# What the heading row carries where a treatment carries its id. No
# treatment can be called this, so the two can never be confused.
PRESETS_ROW = "\u00b7presets\u00b7"
ROUNDS_ROW = "\u00b7rounds\u00b7"

# The filters a photographer actually puts on the front of a lens. Naming
# them beats a free number: these are the three that exist as products,
# and each one means a different amount of colour survives.
CUTOFFS = (
    ("filter not stated", 0.0),
    ("720nm", 720.0),
    ("760nm", 760.0),
    ("850nm", 850.0),
)

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
                 maximum: int = 0, adjustments: dict | None = None,
                 window: dict | None = None):
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
        self.window = window
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            path = self.workspace.recipe_preview(
                self.photo, self.treatment, self.engine, self.demosaic,
                self.maximum, self.adjustments, window=self.window)
        except Exception as exc:
            self.signals.failed.emit(self.generation, self.photo, str(exc))
            return
        self.signals.done.emit(
            self.generation, self.photo, self.treatment, str(path))


# What each adjustment is called where a photographer can read it. The
# operation's own name is the renderer's vocabulary, not theirs.
ADJUSTMENT_NAMES = {
    "tone.exposure": "exposure",
    "tone.contrast": "contrast",
    "tone.brightness": "brightness",
    "tone.highlight": "highlight recovery",
    "tone.shadow": "shadows",
    "tone.white": "the white point",
    "tone.black": "the black point",
    "color.saturation": "saturation",
    "color.temperature": "white balance",
    "color.tint": "tint",
    "detail.clarity": "clarity",
    "detail.structure": "structure",
    "detail.dehaze": "dehaze",
    "detail.sharpen_amount": "sharpening",
    "detail.denoise_luminance": "luminance noise",
    "detail.denoise_color": "colour noise",
    "levels.white_input": "levels, white",
    "levels.black_input": "levels, black",
    "levels.midpoint": "levels, midpoint",
    "finish.vignette": "vignette",
    "lens.profile": "the lens profile",
    "lens.chromatic_aberration": "chromatic aberration",
    "geometry.crop_aspect": "the crop",
    "geometry.rotation": "straightening",
}

STAGE_NAMES = {
    "developing the raw": "developing the raw",
    "reading the frame": "reading the frame",
    "matching the camera": "matching it to the camera",
    "writing the photograph": "writing the photograph",
}

MASK_NAMES = {
    "luma": "a luminance mask", "color": "a colour mask",
    "linear": "a gradient", "radial": "a radial mask",
    "vignette": "a vignette",
}


def _adjustment_name(what: str) -> str:
    """One adjustment, said the way a photographer would say it."""
    if what in STAGE_NAMES:
        return STAGE_NAMES[what]
    if what.startswith("mask:"):
        return MASK_NAMES.get(what[5:], "a masked adjustment")
    if what.startswith("color.hsl_range:"):
        _op, channel, component = (what.split(":") + ["", ""])[:3]
        family = channel.replace("/", " and ")
        return f"{family} {component}".strip()
    return ADJUSTMENT_NAMES.get(what, what.split(".")[-1].replace("_", " "))


class _ExportSignals(QObject):
    done = Signal(str, str, str)          # photo, requested, written
    failed = Signal(str, str)             # photo, reason
    stepped = Signal(str, int, int, str)  # photo, done, total, what


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
                self.photo, self.treatment, self.engine, self.demosaic,
                progress=lambda done, total, what: self.signals.stepped.emit(
                    self.photo, done, total, what))
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
    progressed = Signal(int, int)         # delivered, asked for
    stepped = Signal(str, int, int, str)  # photo, done, total, what

    def __init__(self, workspace: DevelopmentWorkspace,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.workspace = workspace
        self.pending = 0
        self.asked = 0
        self._signals = _ExportSignals()
        self._signals.done.connect(self._finished)
        self._signals.failed.connect(self._failed)
        self._signals.stepped.connect(
            lambda photo, done, total, what: self.stepped.emit(
                photo, done, total, what))
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def export(self, photo: str, treatment: str, engine: str, demosaic: str,
               destination: str) -> None:
        self.pending += 1
        self.asked += 1
        self.progressed.emit(self.asked - self.pending, self.asked)
        self._pool.start(_ExportJob(
            self.workspace, photo, treatment, engine, demosaic, destination,
            self._signals))

    def _finished(self, photo: str, requested: str, written: str) -> None:
        self._settle()
        self.done.emit(photo, requested, written)

    def _failed(self, photo: str, reason: str) -> None:
        self._settle()
        self.failed.emit(photo, reason)

    def _settle(self) -> None:
        self.pending = max(0, self.pending - 1)
        self.progressed.emit(self.asked - self.pending, self.asked)
        if self.pending == 0:
            self.asked = 0

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
        # The latest-wins slot. Fine tuning asks for a render on every
        # slider tick; queueing each one had a drag pile up dozens of
        # full renders that all ran serially and were all discarded as
        # stale on arrival -- the preview sat frozen while the machine
        # ground through them. Instead, while one render is in flight the
        # newest request waits here, each new one replacing the last, and
        # it is submitted the moment the running render settles. At most
        # one render runs and one waits; a drag costs two renders, not
        # forty.
        self._inflight = 0
        self._waiting: tuple | None = None

    def render(self, photo: str, treatment: str, engine: str,
               demosaic: str, adjustments: dict | None = None,
               maximum: int = 0, window: dict | None = None) -> None:
        """``maximum`` overrides the proof edge for this one render.

        A caller that knows how many pixels will actually be shown --
        the fine-tune page, re-rendering on every slider move -- passes
        the screen's own size and pays for nothing it cannot display.
        ``window`` renders only that part of the frame, fast, with every
        frame-coordinate operation behaving as if the whole were here.
        """
        request = (photo, treatment, engine, demosaic, adjustments,
                   maximum or self.maximum, window)
        if self._inflight > 0:
            # The running render is left to land -- its picture is still
            # fresher than what is on screen, so the drag reads as
            # movement -- and this newest request takes the waiting slot.
            self._waiting = request
            return
        self._start(request)

    def _start(self, request: tuple) -> None:
        (photo, treatment, engine, demosaic, adjustments, maximum,
         window) = request
        self._generation += 1
        self._inflight += 1
        self._pool.start(_RenderJob(
            self.workspace, photo, treatment, engine, demosaic,
            self._signals, self._generation, maximum, adjustments,
            window))

    def _settle(self) -> None:
        self._inflight = max(0, self._inflight - 1)
        if self._waiting is not None and self._inflight == 0:
            request, self._waiting = self._waiting, None
            self._start(request)

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
            self._inflight += 1
            self._pool.start(_RenderJob(
                self.workspace, photo, treatment, engine, demosaic,
                self._signals, self._generation, self.maximum))

    def abandon(self) -> None:
        """Stop caring about a render whose frame is no longer on screen."""
        self._generation += 1
        # The waiting request belongs to the abandoned frame too.
        self._waiting = None

    def drop_queued(self) -> None:
        """Give up the work not yet started, and the right to its results.

        A render already running is left alone -- killing a decoder halfway
        wastes what it has done and leaves its working files behind.
        """
        self._generation += 1
        self._waiting = None
        self._pool.clear()
        # Cleared jobs never signal; only the (at most one) running job
        # still will. Counting the cleared ones as settled keeps the
        # latest-wins slot from waiting forever on jobs that no longer
        # exist.
        self._inflight = min(self._inflight, 1)

    def _finished(self, generation: int, photo: str, treatment: str,
                  path: str) -> None:
        self._settle()
        if generation != self._generation:
            return
        pixmap = load_for_screen(path)
        if pixmap.isNull():
            self.failed.emit(photo, "the render could not be read back")
            return
        self.done.emit(photo, treatment, pixmap)

    def _failed(self, generation: int, photo: str, reason: str) -> None:
        self._settle()
        if generation == self._generation:
            self.failed.emit(photo, reason)

    def shutdown(self) -> None:
        self._generation += 1
        self._waiting = None
        self._pool.clear()
        self._pool.waitForDone(5000)
        self._inflight = 0


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

    def forget(self) -> None:
        """Drop the queue and the memory of what has been asked for.

        Not the same as stopping. Stopping is for shutdown and cannot be
        undone; this is for when the frames themselves have changed --
        the album marked infrared, say -- and every previous answer is an
        answer about a different photograph.
        """
        self._pending.clear()
        self._asked.clear()
        self._done = 0

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
        pixmap = load_for_screen(path)
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

    # The magnification changed by a hand -- wheel or double click --
    # so whoever renders the proof can render the part being looked at
    # at the resolution it is being looked at.
    magnified = Signal()

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
        # The magnifier. Zero means fit-to-pane, the lifetime default;
        # the wheel zooms around the cursor, a drag pans, a double click
        # comes home. The zoom survives a new proof arriving, so a
        # slider move can be judged at the crop being inspected.
        self._zoom = 0.0
        self._centre = [0.5, 0.5]
        self._pan_from = None
        # The fast pass: a freshly rendered piece of the frame, drawn
        # over the stale proof at its own place until the full render
        # lands and replaces both.
        self._patch: QPixmap | None = None
        self._patch_window: dict | None = None

    def set_source(self, pixmap: QPixmap | None) -> None:
        # The same photograph at a different resolution must show the
        # same view: the zoom is display-per-source-pixel, so when the
        # source grows the zoom shrinks in step and the crop on screen
        # does not jump -- it sharpens.
        if (pixmap is not None and self._source is not None
                and self._zoom > 0 and self._source.width() > 0
                and pixmap.width() > 0
                and pixmap.width() != self._source.width()):
            self._zoom *= self._source.width() / pixmap.width()
        self._patch = None
        self._patch_window = None
        self._source = pixmap
        if pixmap is None:
            self.setPixmap(QPixmap())
            return
        self.setText("")
        self._redraw()

    def set_patch(self, pixmap: QPixmap | None,
                  window: dict | None) -> None:
        """A fast render of one part of the frame, worn over the proof."""
        self._patch = pixmap
        self._patch_window = dict(window) if window else None
        self._redraw()

    def visible_window(self, margin: float = 0.25) -> dict | None:
        """What part of the source the eye is on, with shoulders.

        Fractions of the source frame, widened by ``margin`` of the view
        on every side so a small pan does not walk off the fast pass.
        None at fit -- the whole frame is the window.
        """
        if self._source is None or self._zoom <= 0:
            return None
        source_w = self._source.width()
        source_h = self._source.height()
        if not source_w or not source_h:
            return None
        view_w = min(source_w, max(self.width(), 1) / self._zoom)
        view_h = min(source_h, max(self.height(), 1) / self._zoom)
        cx = min(max(self._centre[0], view_w / 2 / source_w),
                 1 - view_w / 2 / source_w)
        cy = min(max(self._centre[1], view_h / 2 / source_h),
                 1 - view_h / 2 / source_h)
        wide = view_w * (1 + 2 * margin) / source_w
        tall = view_h * (1 + 2 * margin) / source_h
        x0 = min(max(cx - wide / 2, 0.0), max(1.0 - wide, 0.0))
        y0 = min(max(cy - tall / 2, 0.0), max(1.0 - tall, 0.0))
        return {"x": round(x0, 4), "y": round(y0, 4),
                "w": round(min(wide, 1.0), 4),
                "h": round(min(tall, 1.0), 4)}

    def magnification(self) -> float:
        """How far past fit the eye is: 1 at fit, 8 at the deep end."""
        if self._zoom <= 0 or self._source is None:
            return 1.0
        return max(1.0, self._zoom / max(self._fit_scale(), 1e-6))

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

    def _fit_scale(self) -> float:
        if self._source is None or self._source.width() == 0:
            return 1.0
        return min(max(self.width(), 1) / self._source.width(),
                   max(self.height(), 1) / self._source.height())

    def _redraw(self) -> None:
        if self._source is None:
            return
        if self._zoom <= 0.0:
            self.setPixmap(scaled(
                self._source, max(self.width(), 1), max(self.height(), 1)))
            return
        source_w = self._source.width()
        source_h = self._source.height()
        view_w = min(source_w, max(self.width(), 1) / self._zoom)
        view_h = min(source_h, max(self.height(), 1) / self._zoom)
        self._centre[0] = min(max(self._centre[0],
                                  view_w / 2 / source_w),
                              1 - view_w / 2 / source_w)
        self._centre[1] = min(max(self._centre[1],
                                  view_h / 2 / source_h),
                              1 - view_h / 2 / source_h)
        from PySide6.QtCore import QRect

        left = int(self._centre[0] * source_w - view_w / 2)
        top = int(self._centre[1] * source_h - view_h / 2)
        piece = self._source.copy(QRect(left, top,
                                        int(view_w), int(view_h)))
        shown = scaled(piece, max(self.width(), 1), max(self.height(), 1))
        if self._patch is not None and self._patch_window is not None:
            # The fast pass, drawn where it belongs over the stale
            # proof: the sharp window rides the coarse frame until the
            # full render lands and set_source clears both.
            ratio = float(shown.devicePixelRatio() or 1.0)
            scale_x = (shown.width() / ratio) / max(view_w, 1e-6)
            scale_y = (shown.height() / ratio) / max(view_h, 1e-6)
            held = self._patch_window
            target = QRectF(
                (held["x"] * source_w - left) * scale_x,
                (held["y"] * source_h - top) * scale_y,
                held["w"] * source_w * scale_x,
                held["h"] * source_h * scale_y)
            painter = QPainter(shown)
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(target, self._patch,
                               QRectF(self._patch.rect()))
            painter.end()
        self.setPixmap(shown)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._source is None:
            return
        fit = self._fit_scale()
        current = self._zoom if self._zoom > 0 else fit
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        asked = current * factor
        if asked <= fit * 1.02:
            self._zoom = 0.0
            self._centre = [0.5, 0.5]
        else:
            self._zoom = min(asked, fit * 8)
            # Keep the pixel under the cursor under the cursor.
            pos = event.position()
            shown = self.pixmap()
            if shown is not None and not shown.isNull():
                ratio = float(shown.devicePixelRatio() or 1.0)
                width = shown.width() / ratio
                height = shown.height() / ratio
                dx = (pos.x() - (self.width() - width) / 2) / max(width, 1)
                dy = (pos.y() - (self.height() - height) / 2) / max(
                    height, 1)
                if 0 <= dx <= 1 and 0 <= dy <= 1:
                    lean = 1 - current / self._zoom
                    self._centre[0] += (dx - 0.5) * lean
                    self._centre[1] += (dy - 0.5) * lean
        self._redraw()
        self.magnified.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._zoom = 0.0
        self._centre = [0.5, 0.5]
        self._redraw()
        self.magnified.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._zoom > 0:
            self._pan_from = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._pan_from is not None and self._zoom > 0 \
                and self._source is not None:
            moved = event.position() - self._pan_from
            self._pan_from = event.position()
            self._centre[0] -= moved.x() / self._zoom / self._source.width()
            self._centre[1] -= moved.y() / self._zoom / self._source.height()
            self._redraw()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._pan_from = None
        super().mouseReleaseEvent(event)

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
    treatment_wanted = Signal(str, int)   # photo, rounds of budget
    finetune_wanted = Signal(str, str)    # photo, treatment to open on
    program_wanted = Signal(str, dict)    # program name, parameters

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
        # Which selection the pre-render sweep last worked through. The
        # sweep only warms thumbnails ahead of being asked for; it need not
        # rerun on a mere row change, only when the shown frames change.
        self._swept: tuple[str, ...] | None = None
        # Presets start folded away. Somebody who mostly develops what
        # was written for the frame should not scroll past nine looks to
        # reach the camera's own rendering.
        self.presets_open = False
        # Rounds start folded for the same reason presets do -- and a
        # run the panel refused still deserves its heading, because the
        # photographer paid for those pictures.
        self.rounds_open = False
        # The frames whose treatment runs were active at the last poll,
        # so a completion is an event rather than a state nobody reads.
        self._treating_last: set[str] = set()
        # How each frame's last treatment run ended, so the page that
        # asked is the page that answers. Cleared by asking again.
        self._treatment_outcome: dict[str, str] = {}
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
        self.exporter.progressed.connect(self._delivery_progress)
        self.exporter.stepped.connect(self._delivery_step)
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
            button.setToolTip(tooltip(tip))
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
        self.panel = panel
        layout = QVBoxLayout(panel)
        self.panel_layout = layout
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
        why.setToolTip(tooltip("The recorded story of this frame."))
        why.clicked.connect(lambda: self.why_wanted.emit(self.current))
        head.addWidget(why)
        layout.addLayout(head)

        # Whether a panel accepted these treatments belongs beside them,
        # not in a report somewhere: it is the difference between an edit
        # somebody vouched for and one nobody did.
        self.stamp = Stamp()
        layout.addWidget(self.stamp)

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
        self.treatments.itemClicked.connect(self._clicked_treatment)
        layout.addWidget(self.treatments)

        # Directly under the list it feeds: this is where a missing answer
        # is noticed, and the entry the run produces appears just above.
        self.treat_button = QPushButton("Kimiya Treatment…")
        self.treat_button.setObjectName("ghost")
        self.treat_button.setFont(theme.body(10))
        self.treat_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.treat_button.setToolTip(tooltip(
            "Develop this frame in rounds: a model plans, renders, "
            "measures its own result and revises, within a budget you "
            "set. Every round is kept beside the photographs, and the "
            "finished treatment appears in the list above once a panel "
            "has vouched for it. Each round costs several model calls."))
        self.treat_button.clicked.connect(self.treat_current)
        layout.addWidget(self.treat_button)

        self.timelapse_button = QPushButton("Timelapse…")
        self.timelapse_button.setObjectName("ghost")
        self.timelapse_button.setFont(theme.body(10))
        self.timelapse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timelapse_button.setToolTip(tooltip(
            "Pin a moving subject across the whole folder and write the "
            "aligned, numbered frames a video is made from. The obvious "
            "parameters are filled by the process; you say what the "
            "subject is and what the frames should wear."))
        self.timelapse_button.clicked.connect(self.timelapse_current)
        layout.addWidget(self.timelapse_button)

        # What a treatment run for this frame is doing right now: a busy
        # bar because the work is real, and words because a bar that
        # cannot say "round 2 of 3" is just a light left on.
        self.treating_bar = QProgressBar()
        self.treating_bar.setRange(0, 0)
        self.treating_bar.setTextVisible(False)
        self.treating_bar.setFixedHeight(6)
        self.treating_bar.setVisible(False)
        layout.addWidget(self.treating_bar)
        self.treating = QLabel("")
        self.treating.setObjectName("hint")
        self.treating.setWordWrap(True)
        self.treating.setFont(theme.body(9))
        self.treating.setVisible(False)
        layout.addWidget(self.treating)

        # The treatment's story, folded. A Kimiya intent runs to a
        # paragraph, and a paragraph always on screen reads as scaffolding
        # left up. One elided line says what it is; the (!) unfolds the
        # whole of it for whoever asks, the same dot the controls wear.
        told = QHBoxLayout()
        told.setContentsMargins(0, 0, 0, 0)
        told.setSpacing(6)
        self.intent_line = QLabel("")
        self.intent_line.setObjectName("hint")
        self.intent_line.setFont(theme.body(9))
        told.addWidget(self.intent_line, 1)
        self.intent_dot = QPushButton("!")
        self.intent_dot.setObjectName("aboutDot")
        self.intent_dot.setCheckable(True)
        self.intent_dot.setFixedSize(16, 16)
        self.intent_dot.setCursor(Qt.CursorShape.PointingHandCursor)
        self.intent_dot.setToolTip(tooltip(
            "The whole of what this treatment says it is doing."))
        self.intent_dot.toggled.connect(self._unfold_intent)
        told.addWidget(self.intent_dot)
        layout.addLayout(told)
        self.intent = QLabel("")
        self.intent.setObjectName("hint")
        self.intent.setWordWrap(True)
        self.intent.setFont(theme.body(9))
        self.intent.setVisible(False)
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

        # A filter on the front of the lens was there for the whole
        # album, so this is one decision about the folder rather than a
        # question asked again on every frame.
        self.infrared = QCheckBox("Infrared album")
        self.infrared.setFont(theme.body(9))
        self.infrared.setCursor(Qt.CursorShape.PointingHandCursor)
        self.infrared.setToolTip(tooltip(
            "Develop these frames without matching them to the camera's "
            "own rendering. Past an infrared filter the camera does not "
            "know what it is looking at, and matching to its guess puts "
            "that guess into every treatment."))
        self.infrared.setChecked(self.workspace.infrared())
        self.infrared.toggled.connect(self._chose_spectrum)

        spectrum_row = QHBoxLayout()
        spectrum_row.setSpacing(8)
        spectrum_row.addWidget(self.infrared)
        # Which filter, in nanometres. It is the difference between a
        # shoot with colour left in it and one without, so it is worth
        # asking rather than guessing -- and it is what the assessing
        # model is told.
        self.cutoff = QComboBox()
        self.cutoff.setFont(theme.body(9))
        self.cutoff.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cutoff.setToolTip(tooltip(
            "The filter's cut-off. At 720nm there is still colour to work "
            "with; by 850nm the three channels record the same light and "
            "the photograph is monochrome whatever anyone does to it."))
        for label, value in CUTOFFS:
            self.cutoff.addItem(label, value)
        self.cutoff.setCurrentIndex(
            max(0, [value for _label, value in CUTOFFS].index(
                self.workspace.cutoff_nm())
                if self.workspace.cutoff_nm() in
                [value for _label, value in CUTOFFS] else 0))
        self.cutoff.setVisible(self.workspace.infrared())
        self.cutoff.currentIndexChanged.connect(
            lambda _index: self._chose_cutoff())
        spectrum_row.addWidget(self.cutoff)
        spectrum_row.addStretch(1)
        layout.addLayout(spectrum_row)

        self.sweep_note = QLabel("")
        self.sweep_note.setObjectName("hint")
        self.sweep_note.setWordWrap(True)
        self.sweep_note.setFont(theme.body(9))
        self.sweep_note.setVisible(False)
        layout.addWidget(self.sweep_note)
        layout.addStretch(1)

        # Clicking a treatment already develops it into the comparison,
        # so a primary button that does the same again was a light that
        # said "important" and a switch that did nothing new. The primary
        # action from here is to go deeper into the chosen treatment.
        self.finetune_button = QPushButton("Advanced fine-tune…")
        self.finetune_button.setObjectName("primary")
        self.finetune_button.setFont(theme.body(10))
        self.finetune_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.finetune_button.setToolTip(tooltip(
            "Open this treatment's recipe as controls: move what it "
            "asked for, switch operations off, and keep the result as "
            "your own preset."))
        self.finetune_button.clicked.connect(self.finetune_current)
        layout.addWidget(self.finetune_button)

        self.export_button = QPushButton("Export…")
        self.export_button.setObjectName("ghost")
        self.export_button.setFont(theme.body(10))
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.setToolTip(tooltip(
            "Render this treatment at the photograph's own size and write a "
            "copy where you choose."))
        self.export_button.clicked.connect(self.export_current)
        layout.addWidget(self.export_button)

        self.verify_button = QPushButton("Verify…")
        self.verify_button.setObjectName("ghost")
        self.verify_button.setFont(theme.body(10))
        self.verify_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.verify_button.setToolTip(tooltip(
            "Ask a model whether this rendering did what the treatment said "
            "it would, without losing the subject."))
        self.verify_button.clicked.connect(self.verify_current)
        layout.addWidget(self.verify_button)

        # A full-size render takes a minute and a half of somebody's
        # evening. A disabled button says only that it is unavailable.
        self.delivery_meter = QProgressBar()
        self.delivery_meter.setRange(0, 100)
        self.delivery_meter.setTextVisible(False)
        self.delivery_meter.setFixedHeight(4)
        self.delivery_meter.hide()
        layout.addWidget(self.delivery_meter)
        self.delivery_note = QLabel("")
        self.delivery_note.setObjectName("hint")
        self.delivery_note.setWordWrap(True)
        self.delivery_note.setFont(theme.body(9))
        self.delivery_note.hide()
        layout.addWidget(self.delivery_note)
        return panel

    def _delivery_step(self, photo: str, done: int, total: int,
                       what: str) -> None:
        """Where inside one photograph the render has got to.

        A treatment is twenty-odd adjustments and a demosaic. Counting
        them turns a bar that could only say "started" into one that says
        how far, which is the difference between waiting and wondering.
        """
        if self.exporter.pending <= 0:
            return
        self.delivery_meter.setRange(0, 100)
        self.delivery_meter.setValue(
            round(100 * done / total) if total else 0)
        self.delivery_meter.show()
        said = _adjustment_name(what)
        batch = ""
        if self.exporter.asked > 1:
            made = self.exporter.asked - self.exporter.pending + 1
            batch = f"{photo}, {made} of {self.exporter.asked} — "
        self.delivery_note.setText(f"{batch}{said}.")
        self.delivery_note.show()

    def _delivery_progress(self, done: int, total: int) -> None:
        """How far through the batch, while it is still being made."""
        running = self.exporter.pending > 0
        self.delivery_meter.setVisible(running)
        self.delivery_note.setVisible(running)
        if not running:
            return
        # The one being rendered now is the one after those finished.
        self.delivery_meter.setRange(0, 0 if total <= 1 else 100)
        if total > 1:
            self.delivery_meter.setValue(round(100 * done / total))
        self.delivery_note.setText(
            f"Rendering {done + 1} of {total} at full size, then writing "
            "it out. About a minute and a half each; you can keep looking "
            "at other frames." if total > 1 else
            "Rendering at full size, then writing it out. About a minute "
            "and a half; you can keep looking at other frames.")

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
        # The shown set is about to be rebuilt, so the sweep must run again.
        self._swept = None
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

    def _directions_entry(self, photo: str) -> dict:
        try:
            payload = self.workspace.directions()
        except Exception:                            # noqa: BLE001 - absent
            return {}
        entries = (payload.get("directions") or {}).get("entries", [])
        return next(
            (item for item in entries
             if isinstance(item, dict) and item.get("photo") == photo), {})

    def _fill_treatments(self) -> None:
        self.stamp.set_verdict(verdict_of(self._directions_entry(self.current)))
        try:
            available = self.workspace.treatments(self.current)
        except Exception as exc:
            available = []
            self._report(str(exc), "alarm")
        self.available = available
        previous = self.treatment
        written = [item for item in available
                   if item.get("kind") not in {"preset", "round"}]
        stated = [item for item in available if item.get("kind") == "preset"]
        rounds = [item for item in available if item.get("kind") == "round"]
        # A preset the photographer is already on keeps the section open,
        # so refilling the list on the way to the next frame does not shut
        # the thing they are working in.
        if any(item["id"] == previous for item in stated):
            self.presets_open = True
        if any(item["id"] == previous for item in rounds):
            self.rounds_open = True
        self.treatments.blockSignals(True)
        self.treatments.clear()
        for item in written:
            self.treatments.addItem(self._treatment_row(item))
        if rounds:
            self.treatments.addItem(self._rounds_row(rounds))
            if self.rounds_open:
                for item in rounds:
                    self.treatments.addItem(self._treatment_row(item))
        if stated:
            self.treatments.addItem(self._presets_row(len(stated)))
            if self.presets_open:
                for item in stated:
                    self.treatments.addItem(self._treatment_row(item))
        self.treatments.blockSignals(False)
        self._request_previews()
        self._size_treatments()
        row = self._row_of(previous)
        if available:
            self.treatments.setCurrentRow(max(row, 0))
            self._chose_treatment(max(row, 0))
        else:
            self.treatment = ""
            self._say_intent("This photograph has no treatment available.")
            self.finetune_button.setEnabled(False)

    def _treatment_row(self, item: dict) -> QListWidgetItem:
        if (item.get("kind") == "round"
                and (self.current, str(item["id"])) not in self.previews):
            proof = Path(str(item.get("proof") or ""))
            if proof.is_file():
                # The round was already rendered once, by the run that
                # made it; its proof is its own thumbnail for nothing.
                picture = load_for_screen(str(proof))
                if picture is not None and not picture.isNull():
                    self.previews[(self.current, str(item["id"]))] = scaled(
                        picture, THUMB_EDGE, THUMB_EDGE)
        entry = QListWidgetItem(f"  {item['name']}")
        entry.setData(Qt.ItemDataRole.UserRole, item["id"])
        entry.setToolTip(tooltip(
            f"{item['name']}\n{item.get('story') or item.get('intent', '')}".strip()))
        entry.setSizeHint(QSize(0, TREATMENT_TILE))
        preview = self.previews.get((self.current, str(item["id"])))
        if preview is not None:
            entry.setIcon(plain_icon(preview))
        return entry

    def _rounds_row(self, rounds: list[dict]) -> QListWidgetItem:
        """The heading over a treatment run's rounds, carrying its verdict.

        A run the panel declined is not hidden behind silence: the
        heading says so, and every round under it stays choosable --
        the photographs are real and the photographer paid for them.
        """
        blessed = bool(rounds and rounds[0].get("warranted"))
        entry = QListWidgetItem(
            f"  {'▾' if self.rounds_open else '▸'}  TREATMENT ROUNDS · "
            f"{len(rounds)}" + ("" if blessed else "  — not warranted"))
        entry.setData(Qt.ItemDataRole.UserRole, ROUNDS_ROW)
        entry.setToolTip(tooltip(
            "Every round the newest Kimiya Treatment run rendered for "
            "this frame, each with its measurements and its own "
            f"critique. {rounds[0].get('run_status', '')} Choosing one "
            "renders exactly what that round rendered, at full quality."))
        entry.setFont(theme.display(8))
        entry.setSizeHint(QSize(0, PRESETS_HEADING))
        entry.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return entry

    def _presets_row(self, count: int) -> QListWidgetItem:
        """The heading that opens and closes the presets, and is one itself.

        Nine presets at tile height are eight hundred pixels of list in a
        panel that has room for four treatments, and most of the time the
        photographer wants the treatment written for this photograph. So
        they fold, and folding is one click on the row that says so.
        """
        entry = QListWidgetItem(
            f"  {'▾' if self.presets_open else '▸'}  PRESETS · {count}")
        entry.setData(Qt.ItemDataRole.UserRole, PRESETS_ROW)
        entry.setToolTip(tooltip(
            "Looks you can apply to any photograph. A preset says nothing "
            "about this frame in particular, so nothing verifies it."))
        entry.setFont(theme.display(8))
        entry.setSizeHint(QSize(0, PRESETS_HEADING))
        # Clickable, so it can be opened; unselectable, so it can never
        # become the treatment that gets rendered.
        entry.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return entry

    def _row_of(self, treatment: str) -> int:
        for row in range(self.treatments.count()):
            if self.treatments.item(row).data(
                    Qt.ItemDataRole.UserRole) == treatment:
                return row
        return -1

    def _at_row(self, row: int) -> dict | None:
        if not (0 <= row < self.treatments.count()):
            return None
        chosen = self.treatments.item(row).data(Qt.ItemDataRole.UserRole)
        return next(
            (item for item in self.available if item["id"] == chosen), None)

    def treatment_jobs(self, jobs: list[dict]) -> None:
        """What the queue says about treatments of this folder's frames.

        Pushed by the launcher on its poll tick. Two duties: say that a
        frame is being treated while it is, and notice the moment a run
        finishes -- the moment the finished treatment should appear in
        the list above without anyone reopening the page.
        """
        active = {}
        for job in jobs:
            if job.get("status") in {"queued", "running", "stopping",
                                     "detached"}:
                active[str(job.get("photo") or "")] = job
        ended = {str(job.get("photo") or ""): job for job in jobs
                 if job.get("status") in {"completed", "failed"}}
        arrived = set(ended) & self._treating_last
        for photo in arrived:
            job = ended[photo]
            self._treatment_outcome[photo] = (
                "The treatment is warranted -- it has joined the list above."
                if job.get("status") == "completed"
                else str(job.get("message") or "The treatment run ended."))
        self._treating_last = set(active)
        busy = self.current in active
        self.treat_button.setEnabled(not busy)
        if busy:
            job = active[self.current]
            if str(job.get("status")) == "running":
                progress = job.get("progress") or {}
                rounds = int(job.get("rounds") or 0)
                done = int(progress.get("completed_items") or 0)
                stage = str(progress.get("stage") or "").strip()
                said = f"Treating this frame now -- {stage}" if stage else                     "Treating this frame now"
                if rounds:
                    said += f" ({done} of {rounds} rounds rendered)"
                self.treating.setText(said + ".")
            else:
                self.treating.setText(
                    "A treatment of this frame is waiting in the queue.")
        elif self.current in self._treatment_outcome:
            # The run this page asked for has ended; say how, here,
            # where the photographer is looking -- not only on the
            # queue page they are not.
            self.treating.setText(self._treatment_outcome[self.current])
        self.treating.setVisible(
            busy or self.current in self._treatment_outcome)
        self.treating_bar.setVisible(
            busy and str(active[self.current].get("status")) == "running")
        if arrived:
            # A run just ended. If it committed, its report is registered
            # and the list rebuild will offer it; if it abstained, the
            # rebuild changes nothing and the rounds are still on disk.
            self._fill_treatments()

    def _timelapse_selection(self) -> list[str]:
        """The frames the photographer means by "selected".

        The marks made in assessment come first: "worth developing" is
        the photographer's own say about which frames matter, and it is
        the only selection a folder opened without culling has -- the
        everything-included report counts every frame and confines
        nothing. Only where nobody has marked anything does the cull's
        kept set speak instead.
        """
        try:
            from opencull_gui.shortlist_reviews import (
                default_shortlist_review_path,
            )

            shortlist = self.report.path.with_name(
                f"{self.report.path.stem}.professional-shortlist.json")
            review = default_shortlist_review_path(shortlist)
            told = json.loads(review.read_text(encoding="utf-8"))
            marked = sorted(
                photo for photo, entry in (told.get("entries") or {}).items()
                if isinstance(entry, dict)
                and entry.get("interesting") is True)
            if marked:
                return marked
        except Exception:                            # noqa: BLE001 - fall back
            pass
        try:
            from shortlist_kernel import chosen_photographs

            from opencull_gui.reviews import default_review_path

            review = default_review_path(self.report.path)
            value = (json.loads(review.read_text(encoding="utf-8"))
                     if review.is_file() else None)
            return chosen_photographs(self.report.data, value)
        except Exception:                            # noqa: BLE001 - whole folder
            return []

    def timelapse_current(self) -> None:
        """The whole-folder stabilizer, obvious parameters pre-filled."""
        from .timelapse import TimelapseDialog

        photos = str(self.workspace.project.get("source_folder") or "")
        if not photos:
            return
        # The look already chosen on this page, carried into the dialog so
        # the timelapse wears the same treatment being looked at.
        look = (self.treatment[len("preset-"):]
                if self.treatment.startswith("preset-") else "")
        selection = self._timelapse_selection()
        # No Qt parent on purpose: a modal dialog with a transient parent is
        # glued to it by some desktops (GNOME attaches modal dialogs) and
        # then cannot be dragged. exec() keeps it application-modal without a
        # parent; it is centred on this window by hand instead.
        dialog = TimelapseDialog(photos, look=look, selection=selection)
        dialog.adjustSize()
        host = self.window().frameGeometry()
        dialog.move(host.center() - dialog.rect().center())
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        request = dialog.run_request()
        if request is None:
            return
        self.program_wanted.emit(request["program"], request["parameters"])
        self._report(
            "Queued. The frames land in the timelapse folder's frames/; "
            "the report beside them ends with the ffmpeg line that makes "
            "the video.")

    def treat_current(self) -> None:
        """Ask for a Kimiya Treatment of this frame, budget stated first."""
        if not self.current:
            return
        rounds, agreed = QInputDialog.getInt(
            self, "Kimiya Treatment",
            f"Treat {self.current} in how many rounds?\n\n"
            "Each round is a plan, a question per mask, a render and a "
            "critique -- several model calls. Three is usually enough; "
            "the treatment stops early when it is satisfied or when a "
            "round changes nothing.",
            3, 1, 6)
        if not agreed:
            return
        self._treatment_outcome.pop(self.current, None)
        self.treatment_wanted.emit(self.current, int(rounds))
        self._report(
            f"Queued: {self.current}, {rounds} round"
            f"{'' if rounds == 1 else 's'}. The rounds land beside the "
            "photographs under .darkimiya/Treatments; the finished "
            "treatment joins this list when the panel vouches for it.")

    def toggle_presets(self) -> None:
        self.presets_open = not self.presets_open
        self._fill_treatments()

    def _size_treatments(self) -> None:
        """Let the list be as tall as its contents and no taller.

        Two treatments in a panel-high box reads as a list that failed to
        load the rest.
        """
        rows = [self.treatments.item(row).sizeHint().height()
                for row in range(self.treatments.count())]
        # The list's own 4px padding top and bottom, and its 1px border.
        wanted = (sum(rows) or TREATMENT_TILE) + 10
        self.treatments.setFixedHeight(min(wanted, self._treatment_ceiling()))

    def _treatment_ceiling(self) -> int:
        """How tall the list may grow: whatever the panel can spare.

        A fixed ceiling was the wrong shape. Five tiles is most of a
        laptop panel and a third of a tall one, so the same list either
        crowded the frame or scrolled with half the panel empty beneath
        it. What it may have is what is left after everything else the
        panel must show -- the intent, the notes, and the buttons, which
        are not negotiable.
        """
        panel = getattr(self, "panel", None)
        layout = getattr(self, "panel_layout", None)
        floor = TILES_SHOWN_LEAST * TREATMENT_TILE + PRESETS_HEADING + 10
        if panel is None or layout is None or panel.height() <= 0:
            return floor
        # The layout's own idea of what it needs, less what this list is
        # currently taking, is what everything else needs.
        others = layout.sizeHint().height() - self.treatments.height()
        return max(floor, panel.height() - others)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """A taller window is more room for treatments, not more empty panel."""
        super().resizeEvent(event)
        if self.treatments.count():
            self._size_treatments()

    def _say_intent(self, text: str) -> None:
        """One elided line of the story; the whole stays behind the dot."""
        settled = " ".join(str(text).split())
        self.intent.setText(str(text))
        from PySide6.QtCore import Qt as QtFlags
        from PySide6.QtGui import QFontMetrics

        metrics = QFontMetrics(self.intent_line.font())
        room = max(self.intent_line.width(), 120)
        self.intent_line.setText(metrics.elidedText(
            settled, QtFlags.TextElideMode.ElideRight, room))
        self.intent_line.setToolTip(tooltip(str(text)) if settled else "")
        self.intent_line.setVisible(bool(settled))
        self.intent_dot.setVisible(
            bool(settled) and metrics.horizontalAdvance(settled) > room)
        # A new treatment folds the story again.
        if self.intent_dot.isChecked():
            self.intent_dot.setChecked(False)
        else:
            self.intent.setVisible(False)

    def _unfold_intent(self, on: bool) -> None:
        self.intent.setVisible(on and bool(self.intent.text()))
        self._size_treatments()

    def _clicked_treatment(self, item: QListWidgetItem) -> None:
        """A click on a treatment asks to see it; a click on the heading folds."""
        if item.data(Qt.ItemDataRole.UserRole) == PRESETS_ROW:
            self.toggle_presets()
            return
        if item.data(Qt.ItemDataRole.UserRole) == ROUNDS_ROW:
            self.rounds_open = not self.rounds_open
            self._fill_treatments()
            return
        self.develop_current(arriving=True)

    def _chose_treatment(self, row: int) -> None:
        chosen = self._at_row(row)
        if chosen is None:
            return
        self.treatment = str(chosen["id"])
        self._say_intent(str(chosen.get("intent") or ""))
        # The intent's height just changed; the list's ceiling is what
        # the panel can spare after it, so it must be asked again.
        self._size_treatments()
        self.finetune_button.setEnabled(True)
        self.engine_note.setText(self._engine_note())
        if chosen.get("kind") == "preset":
            # Its picture was not swept for, so it is asked for now --
            # once, for this frame, because this is the frame the
            # photographer is looking at it on.
            self._want_preview(self.current, str(chosen["id"]))
        self._show_treated()
        self._show_verdict()

    def _want_preview(self, photo: str, treatment: str) -> None:
        if not photo or (photo, treatment) in self.previews:
            return
        self.thumbs.want(
            [(photo, treatment)],
            {photo: (self.engine_for(photo), self._demosaic())})
        self.thumbs.prefer(photo)

    def _chose_cutoff(self) -> None:
        """Record which filter, without redeveloping anything.

        The cut-off changes what the assessing model is told and which
        preset suits the shoot. It does not change the decode, so the
        proofs already made are still proofs of this photograph.
        """
        if not self.workspace.infrared():
            return
        self.workspace.set_spectrum("infrared", self.cutoff.currentData())

    def _chose_spectrum(self, infrared: bool) -> None:
        """Redevelop the album from the other base.

        Every proof on screen was made from the base that is being left
        behind, so they are dropped rather than shown beside the new
        ones. They stay on disk under their own identity; going back
        finds them again without rendering anything.
        """
        self.cutoff.setVisible(infrared)
        try:
            self.workspace.set_spectrum(
                "infrared" if infrared else "visible",
                self.cutoff.currentData() if infrared else 0)
        except Exception as exc:                     # noqa: BLE001 - reported
            self._report(str(exc), "alarm")
            return
        self.previews.clear()
        self.rendered.clear()
        self.thumbs.forget()
        self._fill_treatments()
        self._show_treated()
        self._report(
            "Developing from the raw itself, with no camera match."
            if infrared else
            "Back to matching the camera's own rendering.", "ok")

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

    def finetune_current(self) -> None:
        """Go deeper into the chosen treatment: its recipe as controls."""
        if not self.current or not self.treatment:
            return
        self.finetune_wanted.emit(self.current, self.treatment)

    def develop_current(self, arriving: bool = False) -> None:
        if not self.current or not self.treatment:
            return
        self._arriving = bool(arriving)
        if (self.current, self.treatment) in self.rendered:
            self._show_treated(arriving=arriving)
            return
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
        shown = tuple(self.shown_photos())
        if self._swept == shown:
            # The set has not changed since the sweep last decided it, so a
            # row change need not re-decide it for every frame -- that is
            # what made navigating the develop list slow. Just move the
            # frame in front to the front of the render queue.
            if self.current:
                self.thumbs.prefer(self.current)
            return
        demosaic = self._demosaic()
        pairs: list[tuple[str, str]] = []
        settings: dict[str, tuple[str, str]] = {}
        # The workspace read is the same for every frame in the sweep, and
        # it reads every recipe the shortlist has. Read it once here rather
        # than once per frame -- doing it per frame is what made opening the
        # develop page on a large folder take tens of seconds.
        try:
            shared = self.workspace.payload()
        except Exception:                            # noqa: BLE001 - skipped
            shared = None
        for photo in self.shown_photos():
            try:
                treatments = self.workspace.treatments(
                    photo, payload=shared, include_presets=False)
            except Exception:                        # noqa: BLE001 - skipped
                continue
            if len(treatments) < 2:
                # Only the baseline: nothing to choose between, so nothing
                # to render ahead of being asked.
                continue
            settings[photo] = (self.engine_for(photo), demosaic)
            pairs.extend(
                (photo, str(item["id"])) for item in treatments
                # A preset is a look the photographer reaches for, not one
                # of the answers written about this frame, so it is not
                # rendered ahead of being asked for. Nine of them across a
                # whole selection would be the sweep several times over,
                # and on RAW frames that is minutes of demosaicing for
                # pictures nobody has looked at.
                if item.get("kind") != "preset"
                and (photo, str(item["id"])) not in self.previews)
        self.thumbs.want(pairs, settings)
        self._swept = shown
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
        self.finetune_button.setEnabled(True)
        if photo == self.current and treatment == self.treatment:
            self.stage.set_treated(
                pixmap, self._treatment_name(),
                arriving=getattr(self, "_arriving", False))
            self._show_verdict()
            self._report(
                "Developed. Hold space to see it as shot; the photograph "
                "itself is unchanged.", "ok")

    def _render_failed(self, photo: str, reason: str) -> None:
        self.finetune_button.setEnabled(True)
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
        self.verify_button.setToolTip(tooltip(
            "Ask a model whether this rendering did what the treatment said "
            "it would, without losing the subject."
            if suggestion else
            "The baseline is asked to do nothing, so there is no claim to "
            "check."))
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
