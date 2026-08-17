"""Disagreeing with a treatment, in the treatment's own terms.

A model wrote prose, the compiler turned it into bounded operations, and
those operations are the whole of what the renderer executes. So this page
does not offer a second set of sliders beside them: it shows the operations
themselves, each beside the sentence that produced it, and lets the
photographer move a value inside the range the compiler would have accepted
or switch the operation off.

That keeps two things true at once. The rendering always corresponds to
something readable -- "model asked +0.45 EV, you set +0.30 EV" -- and an
adjusted recipe stays exactly as executable as the one it came from,
because the bounds are the compiler's own.

Guardrails appear here but cannot be moved. They are what the treatment
promised not to do and what the verification pass checks; a photographer
who quietly switched one off would be holding a certificate that judged the
rendering against a claim it no longer makes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from opencull_gui import adjustments, presets, zones
from opencull_gui.development import DevelopmentWorkspace

from . import theme
from .colour import load_for_screen
from .develop import PROOF_EDGE, THUMB_EDGE, PhotoLabel, PreviewQueue, Renderer
from .previews import PreviewLoader, plain_icon
from .widgets import tooltip
from .zoneslider import ZoneSlider

PHOTO_ROW = 30
TREATMENT_ROW = 34
# The filmstrip under the picture: icon, a line of name, breathing room.
STRIP_HEIGHT = 128
STRIP_TILE = 150
# Treatments and presets share this list, so it can be thirteen rows long.
# Past six it scrolls rather than growing, because the controls below it
# are what the page is for.
TREATMENT_ROWS_SHOWN = 6

# Sliders are integers. Every control is carried at this resolution and
# divided back down, which is finer than any of the units are read at.
TICKS = 1000

# The controls with a natural colour axis wear it as a thin ramp under
# the groove: which way does what, in the colour itself. The directions
# match the engine's math -- positive temperature warms (red up, blue
# down), positive tint pulls magenta (green divided down), saturation
# grows from grey. Indicative, not a preview.
COLOUR_RAMPS = {
    "color.temperature": [
        (0.0, (64, 120, 210)), (0.5, (150, 150, 150)),
        (1.0, (235, 160, 60))],
    "color.tint": [
        (0.0, (90, 180, 90)), (0.5, (150, 150, 150)),
        (1.0, (210, 90, 190))],
    "color.saturation": [
        (0.0, (128, 128, 128)), (0.35, (152, 140, 134)),
        (1.0, (225, 90, 70))],
}


class Control(QWidget):
    """One compiled operation, and the sentence it came from.

    An absent control -- an operation the recipe never used -- sits at
    neutral with its checkbox off; ticking it is the ask that inserts
    the operation. The slider's groove is painted with this frame's
    advice bands: teal safe, amber artistic, red damage.
    """

    changed = Signal(str, dict)
    wanted = Signal(str, float)   # an absent op, asked into existence

    def __init__(self, control: dict[str, Any],
                 bands: dict[str, Any] | None = None):
        super().__init__()
        self.control = control
        self.bands = bands or {}
        self.absent = bool(control.get("absent"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.enabled = QCheckBox(control["label"])
        self.enabled.setChecked(control["enabled"] and not self.absent)
        self.enabled.setFont(theme.body(10))
        self.enabled.setToolTip(tooltip(
            "Not part of this treatment. Tick it, or move the slider, "
            "and it becomes a real operation in the recipe."
            if self.absent else
            "Switch this operation off entirely. What it was asked to do "
            "stays readable."))
        self.enabled.toggled.connect(self._switched)
        head.addWidget(self.enabled)
        head.addStretch(1)

        self.reading = QLabel(
            adjustments.written(control["value"], control["unit"]))
        self.reading.setObjectName("reading")
        self.reading.setFont(theme.mono(9))
        head.addWidget(self.reading)
        self.about = QPushButton("!")
        self.about.setObjectName("aboutDot")
        self.about.setCheckable(True)
        self.about.setFixedSize(16, 16)
        self.about.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about.setToolTip(tooltip(
            "The story of this control: the sentence that produced it, "
            "what the model asked for, and what you set."))
        self.about.toggled.connect(self._tell)
        head.addWidget(self.about)
        layout.addLayout(head)

        self.slider = ZoneSlider()
        self.slider.setRange(0, TICKS)
        self.slider.setValue(self._tick(control["value"]))
        self.slider.setEnabled(control["enabled"] and not self.absent)
        # An arrow key moves one of the unit's own steps -- +1%, +0.01 EV,
        # +10 K -- not one thousandth of the range; page keys take ten.
        span = control["high"] - control["low"]
        if span > 0:
            per_step = max(
                1, round(adjustments.step_for(control["unit"])
                         / span * TICKS))
            self.slider.setSingleStep(per_step)
            self.slider.setPageStep(per_step * 10)
        safe = self.bands.get("safe")
        artistic = self.bands.get("artistic")
        if safe and artistic:
            self.slider.set_zones(control["low"], control["high"],
                                  tuple(safe), tuple(artistic))
        self.slider.set_marks(
            control["low"], control["high"],
            None if self.absent else float(control["asked"]),
            float(control.get("neutral", 0.0)))
        ramp = COLOUR_RAMPS.get(str(control.get("op") or ""))
        if ramp:
            self.slider.set_ramp(ramp)
        self.slider.valueChanged.connect(self._moved)
        layout.addWidget(self.slider)

        # The story stays one click away rather than always on screen:
        # seventeen sliders each trailing two lines of prose made the
        # column mostly prose. The (!) beside the reading unfolds it.
        self.source = QLabel(
            f"from: “{control['source']}”" if control["source"] else "")
        self.source.setObjectName("rowPath")
        self.source.setWordWrap(True)
        self.source.setFont(theme.body(8))
        self.source.hide()
        layout.addWidget(self.source)

        self.provenance = QLabel(adjustments.describe(control))
        self.provenance.setObjectName("provenance")
        self.provenance.setWordWrap(True)
        self.provenance.setFont(theme.body(8))
        self.provenance.hide()
        layout.addWidget(self.provenance)

    def _tell(self, on: bool) -> None:
        self.provenance.setVisible(on)
        self.source.setVisible(on and bool(self.control["source"]))

    # --- the slider is integers, the control is not ----------------------

    def _tick(self, value: float) -> int:
        low, high = self.control["low"], self.control["high"]
        if high <= low:
            return 0
        return int(round((value - low) / (high - low) * TICKS))

    def _value(self, tick: int) -> float:
        low, high = self.control["low"], self.control["high"]
        return low + (high - low) * tick / TICKS

    def value(self) -> float:
        return self._value(self.slider.value())

    def _moved(self, tick: int) -> None:
        value = self._value(tick)
        self.reading.setText(adjustments.written(value, self.control["unit"]))
        self._retell(value)
        if self.absent:
            # Moving the slider is as clear an ask as ticking the box.
            self.absent = False
            self.enabled.blockSignals(True)
            self.enabled.setChecked(True)
            self.enabled.blockSignals(False)
            self.slider.setEnabled(True)
            self.wanted.emit(self.control["op"], value)
            return
        self.changed.emit(self.control["id"], {"value": value})

    def _switched(self, on: bool) -> None:
        self.slider.setEnabled(on)
        self._retell(self.value())
        if self.absent:
            if on:
                # Asked into existence at its current position.
                self.absent = False
                self.wanted.emit(self.control["op"], self.value())
            return
        self.changed.emit(self.control["id"], {"enabled": on})

    def _retell(self, value: float) -> None:
        shown = dict(self.control)
        shown["value"] = value
        shown["enabled"] = self.enabled.isChecked()
        self.provenance.setText(adjustments.describe(shown))
        moved = (not shown["enabled"]
                 or abs(value - self.control["asked"]) > 1e-9)
        self.provenance.setProperty("moved", moved)
        self.provenance.style().unpolish(self.provenance)
        self.provenance.style().polish(self.provenance)


class GeometrySlider(QWidget):
    """One geometric fact about a mask: a name, a ZoneSlider, a reading."""

    changed = Signal(str, float)

    def __init__(self, key: str, label: str, low: float, high: float,
                 value: float, unit: str = "%", ramp: list | None = None):
        super().__init__()
        self.key = key
        self.low, self.high, self.unit = low, high, unit
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        name = QLabel(label)
        name.setFont(theme.body(9))
        name.setFixedWidth(64)
        row.addWidget(name)
        self.slider = ZoneSlider()
        self.slider.setRange(0, TICKS)
        self.slider.setValue(
            int((value - low) / (high - low) * TICKS) if high > low else 0)
        if high > low:
            # Geometry reads in whole percents; one key, one percent.
            per_step = max(1, round(TICKS / (high - low)))
            self.slider.setSingleStep(per_step)
            self.slider.setPageStep(per_step * 10)
        if ramp:
            self.slider.set_ramp(ramp)
        self.slider.valueChanged.connect(self._moved)
        row.addWidget(self.slider, 1)
        self.reading = QLabel(f"{value:.0f}{unit}")
        self.reading.setObjectName("reading")
        self.reading.setFont(theme.mono(9))
        self.reading.setFixedWidth(48)
        row.addWidget(self.reading)

    def _moved(self, tick: int) -> None:
        value = self.low + (self.high - self.low) * tick / TICKS
        self.reading.setText(f"{value:.0f}{self.unit}")
        self.changed.emit(self.key, value)



class Histogram(QWidget):
    """What the pixels actually did, beside the sliders that did it.

    Three channel curves over 256 bins, computed from the same proof the
    pane is showing -- the adjusted rendering, or the frame as shot while
    the comparison key is held -- never from a mask overlay's tint. The
    zone bands say how far is wise; this says what happened.
    """

    HEIGHT = 88

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self._bins: list | None = None

    def show_pixmap(self, pixmap) -> None:
        if pixmap is None or pixmap.isNull():
            self._bins = None
            self.update()
            return
        import numpy as np
        from PySide6.QtGui import QImage

        # A histogram of a downscaled copy is indistinguishable from the
        # full frame's and costs nothing on a slider drag.
        image = pixmap.toImage().scaled(
            320, 200, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        ).convertToFormat(QImage.Format.Format_RGB888)
        width, height = image.width(), image.height()
        stride = image.bytesPerLine()
        flat = np.frombuffer(image.constBits(), np.uint8, height * stride)
        pixels = flat.reshape(height, stride)[:, :width * 3]
        pixels = pixels.reshape(height, width, 3)
        self._bins = [
            np.bincount(pixels[..., channel].ravel(),
                        minlength=256).astype(np.float32)
            for channel in range(3)
        ]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.EDGE_SOFT), 1))
        painter.setBrush(QColor(theme.INK))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
        if not self._bins:
            painter.end()
            return
        peak = max(float(bins.max()) for bins in self._bins) or 1.0
        top, bottom = 6.0, self.height() - 6.0
        left, span = 6.0, self.width() - 12.0
        for bins, colour in zip(self._bins, (
                QColor(220, 90, 80), QColor(120, 200, 130),
                QColor(110, 140, 230))):
            path = QPainterPath()
            path.moveTo(left, bottom)
            for bin_index in range(256):
                x = left + span * bin_index / 255.0
                y = bottom - (bottom - top) * min(
                    float(bins[bin_index]) / peak, 1.0)
                path.lineTo(x, y)
            path.lineTo(left + span, bottom)
            path.closeSubpath()
            fill = QColor(colour)
            fill.setAlpha(70)
            painter.setPen(QPen(colour, 1))
            painter.setBrush(fill)
            painter.drawPath(path)
        painter.end()


def _band_rgb(band: str, *, hue_shift: float = 0.0, sat: float = 0.75,
              val: float = 0.8) -> tuple[int, int, int]:
    import colorsys

    centres = {"red": 0, "orange": 30, "yellow": 60, "green": 120,
               "teal": 180, "blue": 230, "purple": 280, "magenta": 315}
    hue = ((centres.get(band, 0) + hue_shift) % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return (int(r * 255), int(g * 255), int(b * 255))


class CropOverlay(QWidget):
    """The photographer's frame, drawn over the picture and draggable.

    A rectangle in fractions of the shown photograph: corners and edges
    resize it, the inside moves it, the outside world is dimmed and a
    thirds grid sits where composition is judged. The overlay only
    gathers the ask; the crop itself is an operation like any other.
    """

    GRAB = 14.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rect_f = [0.0, 0.0, 1.0, 1.0]   # left, top, width, height
        self.aspect: float | None = None      # None = free
        self._mode = ""
        self._from = None
        self._begin = None
        self.setMouseTracking(False)

    # --- geometry between widget and photograph ---------------------------

    def _shown(self):
        from PySide6.QtCore import QRectF

        label = self.parent()
        pixmap = label.pixmap() if label is not None else None
        if pixmap is None or pixmap.isNull():
            return QRectF(0, 0, max(self.width(), 1), max(self.height(), 1))
        ratio = float(pixmap.devicePixelRatio() or 1.0)
        width, height = pixmap.width() / ratio, pixmap.height() / ratio
        return QRectF((self.width() - width) / 2,
                      (self.height() - height) / 2, width, height)

    def _frame_rect(self):
        from PySide6.QtCore import QRectF

        shown = self._shown()
        left, top, wide, tall = self.rect_f
        return QRectF(shown.left() + left * shown.width(),
                      shown.top() + top * shown.height(),
                      wide * shown.width(), tall * shown.height())

    # --- the hand ----------------------------------------------------------

    def _hit(self, pos) -> str:
        frame = self._frame_rect()
        near = self.GRAB
        left = abs(pos.x() - frame.left()) <= near
        right = abs(pos.x() - frame.right()) <= near
        top = abs(pos.y() - frame.top()) <= near
        bottom = abs(pos.y() - frame.bottom()) <= near
        inside_x = frame.left() - near <= pos.x() <= frame.right() + near
        inside_y = frame.top() - near <= pos.y() <= frame.bottom() + near
        if top and left:
            return "tl"
        if top and right:
            return "tr"
        if bottom and left:
            return "bl"
        if bottom and right:
            return "br"
        if top and inside_x:
            return "t"
        if bottom and inside_x:
            return "b"
        if left and inside_y:
            return "l"
        if right and inside_y:
            return "r"
        if frame.contains(pos):
            return "move"
        return ""

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._mode = self._hit(event.position())
        self._from = event.position()
        self._begin = list(self.rect_f)

    def mouseMoveEvent(self, event) -> None:   # noqa: N802 - Qt naming
        if not self._mode or self._from is None:
            return
        shown = self._shown()
        dx = (event.position().x() - self._from.x()) / max(shown.width(), 1)
        dy = (event.position().y() - self._from.y()) / max(shown.height(), 1)
        left, top, wide, tall = self._begin
        least = 0.05
        mode = self._mode
        if mode == "move":
            left = min(max(left + dx, 0.0), 1.0 - wide)
            top = min(max(top + dy, 0.0), 1.0 - tall)
        else:
            right, bottom = left + wide, top + tall
            if "l" in mode:
                left = min(max(left + dx, 0.0), right - least)
            if "r" in mode:
                right = min(max(right + dx, left + least), 1.0)
            if "t" in mode:
                top = min(max(top + dy, 0.0), bottom - least)
            if "b" in mode:
                bottom = min(max(bottom + dy, top + least), 1.0)
            wide, tall = right - left, bottom - top
            if self.aspect:
                # The photograph's own pixels are the aspect's units.
                ratio = shown.width() / max(shown.height(), 1)
                want_tall = wide * ratio / self.aspect
                if mode in ("l", "r"):
                    middle = top + tall / 2
                    tall = min(want_tall, 1.0)
                    top = min(max(middle - tall / 2, 0.0), 1.0 - tall)
                else:
                    tall = min(want_tall, 1.0 - top)
                    wide = tall * self.aspect / ratio
                    if "l" in mode:
                        left = right - wide
        self.rect_f = [left, top, wide, tall]
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._mode = ""
        self._from = None

    # --- the paint ---------------------------------------------------------

    def paintEvent(self, event) -> None:       # noqa: N802 - Qt naming
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter, QPen

        painter = QPainter(self)
        shown = self._shown()
        frame = self._frame_rect()
        scrim = QColor(10, 9, 9, 150)
        painter.fillRect(QRectF(shown.left(), shown.top(),
                                shown.width(), frame.top() - shown.top()),
                         scrim)
        painter.fillRect(QRectF(shown.left(), frame.bottom(), shown.width(),
                                shown.bottom() - frame.bottom()), scrim)
        painter.fillRect(QRectF(shown.left(), frame.top(),
                                frame.left() - shown.left(),
                                frame.height()), scrim)
        painter.fillRect(QRectF(frame.right(), frame.top(),
                                shown.right() - frame.right(),
                                frame.height()), scrim)
        painter.setPen(QPen(QColor(theme.PAPER), 1.4))
        painter.drawRect(frame)
        painter.setPen(QPen(QColor(237, 231, 222, 90), 1))
        for third in (1 / 3, 2 / 3):
            x = frame.left() + frame.width() * third
            y = frame.top() + frame.height() * third
            painter.drawLine(int(x), int(frame.top()),
                             int(x), int(frame.bottom()))
            painter.drawLine(int(frame.left()), int(y),
                             int(frame.right()), int(y))
        painter.setPen(QPen(QColor(theme.SAFELIGHT), 2))
        for cx in (frame.left(), frame.right()):
            for cy in (frame.top(), frame.bottom()):
                painter.drawLine(int(cx - 8), int(cy), int(cx + 8), int(cy))
                painter.drawLine(int(cx), int(cy - 8), int(cx), int(cy + 8))
        painter.end()


class HslPanel(QWidget):
    """Eight colour bands, each with a hue, saturation and lightness move.

    The engine has spoken this vocabulary from the start -- treatments
    written by models use it -- but the page never offered it to the
    hand. One component shown at a time, the way a person works: turn
    the blues, then decide about their saturation.
    """

    changed = Signal(str, str, float)   # band, component, value

    def __init__(self, state: dict, parent=None):
        super().__init__(parent)
        self._state = dict(state)
        self._component = "saturation"
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        switch = QHBoxLayout()
        switch.setSpacing(6)
        self._tabs = {}
        for name, label in (("hue", "Hue"), ("saturation", "Saturation"),
                            ("lightness", "Lightness")):
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setProperty("slim", "true")
            button.setCheckable(True)
            button.setFont(theme.body(9))
            button.setChecked(name == self._component)
            button.clicked.connect(
                lambda _on=False, n=name: self._switch(n))
            self._tabs[name] = button
            switch.addWidget(button)
        switch.addStretch(1)
        column.addLayout(switch)
        self._rows = {}
        for band in adjustments.HSL_BANDS:
            row = GeometrySlider(band, band.title(), -100.0, 100.0, 0.0)
            row.changed.connect(self._moved)
            self._rows[band] = row
            column.addWidget(row)
        self._restyle()

    def _bound(self) -> float:
        return 45.0 if self._component == "hue" else 100.0

    def _switch(self, component: str) -> None:
        self._component = component
        for name, button in self._tabs.items():
            button.setChecked(name == component)
        self._restyle()

    def _restyle(self) -> None:
        bound = self._bound()
        for band, row in self._rows.items():
            row.low, row.high = -bound, bound
            held = self._state.get((band, self._component), {})
            value = float(held.get("value", 0.0))
            row.slider.blockSignals(True)
            row.slider.setValue(int(
                (value + bound) / (2 * bound) * TICKS))
            row.slider.blockSignals(False)
            row.reading.setText(f"{value:+.0f}")
            if self._component == "hue":
                ramp = [(0.0, _band_rgb(band, hue_shift=-45)),
                        (0.5, _band_rgb(band)),
                        (1.0, _band_rgb(band, hue_shift=45))]
            elif self._component == "saturation":
                ramp = [(0.0, (128, 128, 128)),
                        (1.0, _band_rgb(band, sat=0.95))]
            else:
                ramp = [(0.0, _band_rgb(band, val=0.25)),
                        (1.0, _band_rgb(band, val=0.95))]
            row.slider.set_ramp(ramp)
            row.slider.update()

    def _moved(self, band: str, value: float) -> None:
        self._state[(band, self._component)] = {"value": float(value)}
        self.changed.emit(band, self._component, float(value))


class CurvePanel(QWidget):
    """The classic RGB tone curve, drawn with the renderer's own math.

    The line on the panel is the engine's LUT for these points -- the
    same monotone interpolation the render runs -- so what is drawn is
    what develops. Drag a point; click the line to add one; right-click
    a middle point to remove it. The endpoints move only up and down.
    """

    changed = Signal(list)   # the control points, 0..255

    HEIGHT = 170
    GRAB = 12.0

    def __init__(self, points: list | None = None, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.points: list[list[float]] = [
            [float(x), float(y)] for x, y in (points or [[0, 0], [255, 255]])]
        self._dragging: int | None = None

    def is_identity(self) -> bool:
        return self.points == [[0.0, 0.0], [255.0, 255.0]]

    # --- panel space <-> curve space --------------------------------------

    def _rect(self):
        from PySide6.QtCore import QRectF

        return QRectF(6, 6, self.width() - 12, self.height() - 12)

    def _to_panel(self, x: float, y: float):
        from PySide6.QtCore import QPointF

        area = self._rect()
        return QPointF(area.left() + area.width() * x / 255.0,
                       area.bottom() - area.height() * y / 255.0)

    def _to_curve(self, pos) -> tuple[float, float]:
        area = self._rect()
        x = (pos.x() - area.left()) / max(area.width(), 1) * 255.0
        y = (area.bottom() - pos.y()) / max(area.height(), 1) * 255.0
        return max(0.0, min(255.0, x)), max(0.0, min(255.0, y))

    # --- the hand ----------------------------------------------------------

    def _near(self, pos) -> int | None:
        for index, (x, y) in enumerate(self.points):
            spot = self._to_panel(x, y)
            if (abs(spot.x() - pos.x()) <= self.GRAB
                    and abs(spot.y() - pos.y()) <= self.GRAB):
                return index
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        index = self._near(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            if index is not None and 0 < index < len(self.points) - 1:
                del self.points[index]
                self.update()
                self.changed.emit([list(p) for p in self.points])
            return
        if index is None:
            x, y = self._to_curve(event.position())
            self.points.append([x, y])
            self.points.sort(key=lambda p: p[0])
            index = next(i for i, p in enumerate(self.points)
                         if p[0] == x and p[1] == y)
        self._dragging = index
        self.update()

    def mouseMoveEvent(self, event) -> None:   # noqa: N802 - Qt naming
        if self._dragging is None:
            return
        x, y = self._to_curve(event.position())
        index = self._dragging
        if index == 0:
            x = 0.0
        elif index == len(self.points) - 1:
            x = 255.0
        else:
            # A point stays between its neighbours: the curve is a
            # function, and the engine's interpolation demands it.
            x = max(self.points[index - 1][0] + 1.0,
                    min(self.points[index + 1][0] - 1.0, x))
        self.points[index] = [x, y]
        self.update()
        self.changed.emit([list(p) for p in self.points])

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._dragging = None

    # --- the paint ---------------------------------------------------------

    def paintEvent(self, event) -> None:       # noqa: N802 - Qt naming
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QPainter, QPen

        from development_engine import _curve_lut

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.EDGE_SOFT), 1))
        painter.setBrush(QColor(theme.INK))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
        area = self._rect()
        painter.setPen(QPen(QColor(theme.EDGE_SOFT), 1))
        for quarter in (0.25, 0.5, 0.75):
            x = area.left() + area.width() * quarter
            y = area.top() + area.height() * quarter
            painter.drawLine(int(x), int(area.top()), int(x),
                             int(area.bottom()))
            painter.drawLine(int(area.left()), int(y),
                             int(area.right()), int(y))
        # The identity diagonal, faint: what "no curve" would do.
        painter.setPen(QPen(QColor(theme.FAINT), 1, Qt.PenStyle.DotLine))
        painter.drawLine(self._to_panel(0, 0).toPoint(),
                         self._to_panel(255, 255).toPoint())
        # The curve itself, from the engine's own LUT.
        lut = _curve_lut(self.points)
        painter.setPen(QPen(QColor(theme.SAFELIGHT), 2))
        line = [self._to_panel(i, float(lut[i]) * 255.0)
                for i in range(256)]
        painter.drawPolyline([QPointF(p) for p in line])
        # The hands.
        for x, y in self.points:
            spot = self._to_panel(x, y)
            painter.setPen(QPen(QColor(theme.INK), 1))
            painter.setBrush(QColor(theme.PAPER))
            painter.drawEllipse(spot, 4.5, 4.5)
        painter.end()


class FineTunePage(QWidget):
    """The operations behind one treatment, and the proof of moving them."""

    closed = Signal()
    zones_wanted = Signal(str)   # the frame whose slider advice is asked for

    def __init__(self, report, workspace: DevelopmentWorkspace,
                 loader: PreviewLoader, pool: QThreadPool | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.report = report
        self.workspace = workspace
        self.loader = loader
        self.photos: list[str] = []
        self.treatments: list[dict] = []
        self.current = ""
        self.treatment = ""
        self.recipe: dict[str, Any] = {}
        self.changes: dict[str, dict] = {}
        self.controls: list[Control] = []
        self._pristine: dict[str, Any] = {}
        # 0 is the base layer; N is the Nth mask. Selecting a layer
        # points the whole control surface at it, Capture One fashion.
        self.layer = 0
        # Which mask is being shown as a tint, if any, and the plain
        # pixmap underneath it.
        self._overlay_for = ""
        # Which colour layer is waiting for a click on the picture.
        self._picking_for = ""
        # The page's memory: every gesture pushes the state it is about
        # to change, so undo walks back through real states rather than
        # inverting operations. A slider drag is one gesture, not forty:
        # pushes with the same label inside a beat are coalesced.
        self._undo: list = []
        self._redo: list = []
        self._remember_key = ""
        self._remember_at = 0.0
        self._plain_pixmap = None
        # The frame as shot, for the press-and-hold comparison, and
        # whether the hold is down right now.
        self._as_shot_pixmap = None
        self._holding = False

        self.renderer = Renderer(workspace, PROOF_EDGE, pool, self)
        self.renderer.done.connect(self._rendered)
        self.renderer.failed.connect(self._render_failed)
        # Thumbnails for the filmstrip, at the develop page's own edge so
        # the two pages share one cache and the icons are usually free.
        self.thumbs = PreviewQueue(
            workspace, THUMB_EDGE, self.renderer._pool, parent=self)
        self.thumbs.ready.connect(self._thumb_ready)
        self.previews: dict[tuple[str, str], object] = {}

        self._build()
        self.refresh()

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

        self.list = QListWidget()
        self.list.setObjectName("clusterList")
        self.list.setFixedWidth(200)
        self.list.currentRowChanged.connect(self._chose_photo)
        split.addWidget(self.list)

        stage = QFrame()
        stage.setObjectName("pane")
        column = QVBoxLayout(stage)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.caption = QLabel("AS ADJUSTED")
        self.caption.setObjectName("paneCaption")
        self.caption.setFont(theme.display(8))
        head.addWidget(self.caption)
        head.addStretch(1)
        self.crop_button = QPushButton("Crop ⛶")
        self.crop_button.setObjectName("ghost")
        self.crop_button.setProperty("slim", "true")
        self.crop_button.setFont(theme.body(9))
        self.crop_button.setCheckable(True)
        self.crop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.crop_button.setToolTip(tooltip(
            "Frame the photograph: drag the rectangle's corners and "
            "edges, move it from inside, straighten below it. The crop "
            "is an operation like any other -- undoable, exported with "
            "the recipe, applied at full size."))
        self.crop_button.toggled.connect(self._crop_mode_toggled)
        head.addWidget(self.crop_button)
        wb = QPushButton("WB ⌖")
        wb.setObjectName("ghost")
        wb.setProperty("slim", "true")
        wb.setFont(theme.body(9))
        wb.setCursor(Qt.CursorShape.PointingHandCursor)
        wb.setToolTip(tooltip(
            "White balance by eye-dropper: click, then click something "
            "that should be neutral grey -- a card, concrete, a white "
            "shirt in shade. Temperature and tint move to make it so."))
        wb.clicked.connect(self._start_wb_pick)
        head.addWidget(wb)
        hint = QLabel("hold B: as shot")
        hint.setObjectName("paneHint")
        hint.setFont(theme.body(9))
        head.addWidget(hint)
        column.addLayout(head)
        self.frame = PhotoLabel()
        self.frame.setObjectName("paneImage")
        self.frame.installEventFilter(self)
        # Says a render is on its way -- but only once it has taken long
        # enough to wonder. A fast render never shows it at all.
        from PySide6.QtCore import QTimer

        self._crop_overlay = CropOverlay(self.frame)
        self._crop_overlay.hide()
        self._crop_base = None
        crop_bar = QHBoxLayout()
        crop_bar.setSpacing(8)
        self._crop_aspect = QComboBox()
        self._crop_aspect.setFont(theme.body(9))
        for label, value in (("Free", 0.0), ("Original", -1.0),
                             ("1:1", 1.0), ("3:2", 1.5), ("2:3", 2 / 3),
                             ("4:3", 4 / 3), ("16:9", 16 / 9)):
            self._crop_aspect.addItem(label, value)
        self._crop_aspect.currentIndexChanged.connect(self._crop_aspected)
        crop_bar.addWidget(self._crop_aspect)
        self._crop_angle = GeometrySlider(
            "angle", "Straighten", -15.0, 15.0, 0.0, unit="°")
        self._crop_angle.changed.connect(self._crop_angled)
        crop_bar.addWidget(self._crop_angle, 1)
        crop_apply = QPushButton("Apply")
        crop_apply.setObjectName("primary")
        crop_apply.setFont(theme.body(9))
        crop_apply.clicked.connect(self._crop_apply)
        crop_bar.addWidget(crop_apply)
        crop_cancel = QPushButton("Cancel")
        crop_cancel.setFont(theme.body(9))
        crop_cancel.clicked.connect(
            lambda: self.crop_button.setChecked(False))
        crop_bar.addWidget(crop_cancel)
        self._crop_bar = QWidget()
        self._crop_bar.setLayout(crop_bar)
        self._crop_bar.layout().setContentsMargins(0, 0, 0, 0)
        self._crop_bar.hide()
        column.addWidget(self._crop_bar)

        self._preparing = QLabel("developing…", self.frame)
        self._preparing.setObjectName("hint")
        self._preparing.setFont(theme.body(9))
        self._preparing.setStyleSheet(
            "background: rgba(19, 18, 17, 0.8); border-radius: 8px; "
            "padding: 4px 10px;")
        self._preparing.hide()
        self._render_pending = False
        self._prepare_timer = QTimer(self)
        self._prepare_timer.setSingleShot(True)
        self._prepare_timer.setInterval(350)
        self._prepare_timer.timeout.connect(self._still_preparing)
        column.addWidget(self.frame, 1)

        # The treatments, horizontal under the picture -- a filmstrip of
        # what this frame could be, each with its own rendering. Moving
        # them out of the side panel is what gives the layers and the
        # controls the column they needed.
        self.treatment_list = QListWidget()
        self.treatment_list.setObjectName("treatmentList")
        self.treatment_list.setFlow(QListWidget.Flow.LeftToRight)
        self.treatment_list.setWrapping(False)
        self.treatment_list.setIconSize(QSize(132, 88))
        self.treatment_list.setFixedHeight(STRIP_HEIGHT)
        self.treatment_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.treatment_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.treatment_list.currentRowChanged.connect(self._chose_treatment)
        column.addWidget(self.treatment_list)
        split.addWidget(stage, 1)

        split.addWidget(self._panel())
        outer.addLayout(split, 1)

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

        self.title = QLabel("FINE TUNING")
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

    def _panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setFixedWidth(360)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        self.histogram = Histogram()
        layout.addWidget(self.histogram)

        self.prompt = QLineEdit()
        self.prompt.setFont(theme.body(10))
        self.prompt.setPlaceholderText(
            "Say it: shadows +12, vignette -8, temperature 5400 kelvin")
        self.prompt.setToolTip(tooltip(
            "Typed words compile on this machine, through the same grammar "
            "the suggestions use, and move the controls below. No model is "
            "asked and nothing is spent."))
        self.prompt.returnPressed.connect(self.speak)
        layout.addWidget(self.prompt)

        layers_head = QHBoxLayout()
        layers_head.setSpacing(8)
        layers_title = QLabel("LAYERS")
        layers_title.setObjectName("eyebrow")
        layers_title.setFont(theme.display(8))
        layers_head.addWidget(layers_title)
        layers_head.addStretch(1)
        self.add_layer_button = QPushButton("+ mask")
        self.add_layer_button.setObjectName("ghost")
        self.add_layer_button.setFont(theme.body(8))
        self.add_layer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_layer_button.setToolTip(tooltip(
            "A mask is the answer to two parts of the frame needing "
            "opposite things: radial around a point, linear from an "
            "edge, luma across a band of brightness. It becomes a "
            "layer here, with the whole control surface of its own."))
        self.add_layer_button.clicked.connect(self._add_mask)
        layers_head.addWidget(self.add_layer_button)
        layout.addLayout(layers_head)

        self.layers = QListWidget()
        self.layers.setObjectName("treatmentList")
        self.layers.setToolTip(tooltip(
            "The base layer is the whole frame and is always there. "
            "Each mask is a layer above it: select one and every "
            "control below edits that mask's inside. The tick is the "
            "layer's visibility."))
        self.layers.currentRowChanged.connect(self._chose_layer)
        self.layers.itemChanged.connect(self._layer_visibility)
        self.layers.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.layers.customContextMenuRequested.connect(self._layer_menu)
        layout.addWidget(self.layers)

        scroll = QScrollArea()
        self._scroll = scroll
        # Where the column should sit after a rebuild. The area grows its
        # range asynchronously as the new widgets land, so the restore
        # rides rangeChanged for the burst instead of firing once early.
        self._hold_scroll = 0
        scroll.verticalScrollBar().rangeChanged.connect(self._scroll_grew)
        scroll.setObjectName("controlScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("controls")
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setSpacing(6)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        # Two rows on a real seam -- restore, then persist-and-ask. Five
        # buttons in one row of this panel clipped every label to a
        # syllable ("s sho", "pe fi"); a label nobody can read is not a
        # button, and these five actions are too abstract for icons to
        # say alone.
        from PySide6.QtGui import QKeySequence, QShortcut

        QShortcut(QKeySequence.StandardKey.Undo, self, self.undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, self.redo)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.undo_button = QPushButton("↶")
        self.undo_button.setObjectName("ghost")
        self.undo_button.setProperty("slim", "true")
        self.undo_button.setFixedWidth(34)
        self.undo_button.setToolTip(tooltip("Undo (Ctrl+Z)"))
        self.undo_button.clicked.connect(self.undo)
        actions.addWidget(self.undo_button)
        self.redo_button = QPushButton("↷")
        self.redo_button.setObjectName("ghost")
        self.redo_button.setProperty("slim", "true")
        self.redo_button.setFixedWidth(34)
        self.redo_button.setToolTip(tooltip("Redo (Ctrl+Shift+Z)"))
        self.redo_button.clicked.connect(self.redo)
        actions.addWidget(self.redo_button)
        self.shot_button = QPushButton("As shot")
        self.shot_button.setObjectName("ghost")
        self.shot_button.setFont(theme.body(10))
        self.shot_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.shot_button.setToolTip(tooltip(
            "Switch every operation off -- the whole treatment, masks "
            "included -- so the frame stands as the camera developed it. "
            "A working state, not a peek: build your own recipe up from "
            "here, control by control, and keep or export the result. "
            "Nothing is deleted; every switch can be turned back on, "
            "and As suggested restores the treatment whole."))
        self.shot_button.clicked.connect(self.reset_to_shot)
        actions.addWidget(self.shot_button)
        self.reset_button = QPushButton("As suggested")
        self.reset_button.setObjectName("ghost")
        self.reset_button.setFont(theme.body(10))
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.setToolTip(tooltip(
            "Put every control back to what the model asked for -- the "
            "selected treatment's own recipe, whole."))
        self.reset_button.clicked.connect(self.reset)
        actions.addWidget(self.reset_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        keeps = QHBoxLayout()
        keeps.setSpacing(8)
        self.preset_button = QPushButton("Preset…")
        self.preset_button.setObjectName("ghost")
        self.preset_button.setFont(theme.body(10))
        self.preset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preset_button.setToolTip(tooltip(
            "Keep these adjustments as a look you can apply to any "
            "photograph. The numbers travel; the crop and the "
            "straightening stay with this frame, because they are about "
            "where its subject is."))
        self.preset_button.clicked.connect(self.save_preset)
        keeps.addWidget(self.preset_button)
        self.recipe_button = QPushButton("Recipe…")
        self.recipe_button.setObjectName("ghost")
        self.recipe_button.setFont(theme.body(10))
        self.recipe_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recipe_button.setToolTip(tooltip(
            "Write this version as a portable recipe file -- the same "
            "format the develop page imports -- masks and all, so it "
            "renders identically anywhere."))
        self.recipe_button.clicked.connect(self.save_recipe_file)
        keeps.addWidget(self.recipe_button)
        self.advise_button = QPushButton("Advise…")
        self.advise_button.setObjectName("ghost")
        self.advise_button.setFont(theme.body(10))
        self.advise_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advise_button.setToolTip(tooltip(
            "Ask a model to place each slider's safe and artistic bands "
            "for THIS frame -- one paid call. Until then the bands are "
            "professional defaults: right on average, wrong in "
            "particular. The advice paints the next time this frame's "
            "controls are opened."))
        self.advise_button.clicked.connect(self._ask_zones)
        keeps.addWidget(self.advise_button)
        keeps.addStretch(1)
        layout.addLayout(keeps)

        self.keep_button = QPushButton("Develop this version")
        self.keep_button.setObjectName("primary")
        self.keep_button.setFont(theme.body(10))
        self.keep_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.keep_button.setToolTip(tooltip(
            "Render at full size and record it as its own version, beside "
            "the treatment it came from."))
        self.keep_button.clicked.connect(self.keep)
        layout.addWidget(self.keep_button)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        layout.addWidget(self.status)
        return panel

    # --- what there is to tune -------------------------------------------

    def refresh(self) -> None:
        payload = self.workspace.payload()
        self.photos = sorted(
            str(item.get("photo")) for item in payload.get("candidates", [])
            if isinstance(item, dict) and item.get("photo"))
        self.list.blockSignals(True)
        self.list.clear()
        for photo in self.photos:
            item = QListWidgetItem(photo)
            item.setSizeHint(QSize(0, PHOTO_ROW))
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.photos:
            self.list.setCurrentRow(0)
            self.show_photo(self.photos[0])
        else:
            self._nothing()

    def _nothing(self) -> None:
        self.progress.setText("")
        self.keep_button.setEnabled(False)
        self.reset_button.setEnabled(False)
        self.frame.set_message(
            "Fine tuning moves the numbers a treatment compiled into, so "
            "there has to be a treatment first. Ask for editing suggestions, "
            "and the frames you asked about appear here.")

    def _chose_photo(self, row: int) -> None:
        if 0 <= row < len(self.photos):
            self.show_photo(self.photos[row])

    def _forget_frame(self) -> None:
        self._as_shot_pixmap = None
        self._plain_pixmap = None

    def show_photo(self, photo: str) -> None:
        self.current = photo
        self.changes = {}
        self._forget_frame()
        self.progress.setText(photo)
        # The baseline compiles to no operations at all, so there is nothing
        # in it to move: it is left out rather than offered and found empty.
        self.treatments = [
            item for item in self.workspace.treatments(photo)
            if item.get("id") != "calibrated"]
        self.treatment_list.blockSignals(True)
        self.treatment_list.clear()
        wanted: list[tuple[str, str]] = []
        for item in self.treatments:
            name = str(item.get("name") or item.get("id"))
            entry = QListWidgetItem(name)
            entry.setFont(theme.body(8))
            entry.setToolTip(tooltip(name))
            entry.setSizeHint(QSize(STRIP_TILE, STRIP_HEIGHT - 14))
            held = self.previews.get((photo, str(item.get("id"))))
            if held is not None:
                entry.setIcon(plain_icon(held))
            else:
                wanted.append((photo, str(item.get("id"))))
            self.treatment_list.addItem(entry)
        self.treatment_list.blockSignals(False)
        if wanted:
            self.thumbs.want(
                wanted, {photo: (self.engine(), self._demosaic())})
        # The frame is never empty while the first render cooks: the
        # photograph as shot is on screen the moment it is chosen.
        try:
            self._as_shot_pixmap = load_for_screen(
                self.workspace._as_shot_preview(photo, self.proof_edge()))
            if self._as_shot_pixmap is not None:
                self.caption.setText("AS SHOT · rendering the treatment…")
                self.frame.set_source(self._as_shot_pixmap)
        except Exception:                            # noqa: BLE001 - blank
            self._as_shot_pixmap = None
        if self.treatments:
            self.treatment_list.setCurrentRow(0)
            self.show_treatment(str(self.treatments[0].get("id")))
        else:
            self._clear()
            self.keep_button.setEnabled(False)
            self.reset_button.setEnabled(False)
            self.frame.set_message(
                f"{photo} has no treatment with executable operations, so "
                "there is nothing here to move.")

    def _chose_treatment(self, row: int) -> None:
        if 0 <= row < len(self.treatments):
            self.show_treatment(str(self.treatments[row].get("id")))

    def engine(self) -> str:
        return str(self.workspace.decoder_for(self.current).get("engine")
                   or "default")

    def show_treatment(self, treatment: str) -> None:
        self.treatment = treatment
        self.changes = {}
        self.layer = 0
        self._undo.clear()
        self._redo.clear()
        self._remember_key = ""
        try:
            self.recipe = self.workspace.compiled_recipe(
                self.current, treatment, self.engine())
        except Exception as exc:                     # noqa: BLE001 - reported
            self.recipe = {}
            self._pristine = {}
            self._clear()
            self._report(f"That treatment could not be read: {exc}", "alarm")
            return
        # The compile as it arrived, before any hand touched it: what
        # every reset rebuilds from. The mirror above accumulates the
        # photographer's structure -- inserts, masks -- and without a
        # pristine copy, "back to asked" could only be approximated by
        # remembering what to subtract.
        self._pristine = json.loads(json.dumps(self.recipe))
        self._show_controls()
        self.render()

    def _remember(self, label: str) -> None:
        import json as json_module
        import time as time_module

        now = time_module.monotonic()
        if label == self._remember_key and now - self._remember_at < 1.2:
            self._remember_at = now
            return
        self._undo.append((json_module.dumps(self.changes), self.layer))
        del self._undo[:-50]
        self._redo.clear()
        self._remember_key = label
        self._remember_at = now

    def undo(self) -> None:
        import json as json_module

        if not self._undo:
            self._report("Nothing to undo.")
            return
        self._redo.append((json_module.dumps(self.changes), self.layer))
        held, layer = self._undo.pop()
        self.changes = json_module.loads(held)
        self.layer = layer
        self._remember_key = ""
        self._rebuild_mirror()

    def redo(self) -> None:
        import json as json_module

        if not self._redo:
            self._report("Nothing to redo.")
            return
        self._undo.append((json_module.dumps(self.changes), self.layer))
        held, layer = self._redo.pop()
        self.changes = json_module.loads(held)
        self.layer = layer
        self._remember_key = ""
        self._rebuild_mirror()

    def _rebuild_mirror(self) -> None:
        """The page's recipe, recomputed as pristine plus what remains.

        Every reset filters self.changes and calls this: the mirror can
        never drift from the changes dict, because it is never edited --
        only derived.
        """
        self.recipe = (adjustments.apply(self._pristine, self.changes)
                       if self.changes
                       else json.loads(json.dumps(self._pristine)))
        self._show_controls()
        self.render()

    def _crop_mode_toggled(self, on: bool) -> None:
        self._crop_bar.setVisible(on)
        if on:
            held = self.changes.get("crop") or {}
            self._crop_overlay.rect_f = [
                float(held.get("left", 0.0)), float(held.get("top", 0.0)),
                float(held.get("width", 1.0)),
                float(held.get("height", 1.0))]
            self._crop_angle.slider.blockSignals(True)
            angle = float(held.get("angle", 0.0))
            self._crop_angle.slider.setValue(
                int((angle + 15) / 30 * TICKS))
            self._crop_angle.slider.blockSignals(False)
            self._crop_angle.reading.setText(f"{angle:.0f}°")
            self._crop_overlay.setGeometry(self.frame.rect())
            self._crop_overlay.show()
            self._crop_overlay.raise_()
            self.render()          # the un-cropped proof, to frame within
        else:
            self._crop_overlay.hide()
            self._crop_base = None
            self.render()

    def _crop_angled(self, _key: str, angle: float) -> None:
        # Straightening is judged live: the shown proof turns under the
        # fixed rectangle, cheap at proof size.
        if self._crop_base is not None:
            from PySide6.QtGui import QTransform

            turned = self._crop_base.transformed(
                QTransform().rotate(-angle),
                Qt.TransformationMode.SmoothTransformation)
            self.frame.set_source(turned)
            self._crop_overlay.update()

    def _crop_aspected(self) -> None:
        value = float(self._crop_aspect.currentData() or 0.0)
        if value == 0.0:
            self._crop_overlay.aspect = None
        elif value < 0.0:
            source = self.frame._source
            self._crop_overlay.aspect = (
                source.width() / max(source.height(), 1)
                if source is not None else None)
        else:
            self._crop_overlay.aspect = value
        self._crop_overlay.update()

    def _crop_angle_value(self) -> float:
        tick = self._crop_angle.slider.value()
        return -15.0 + 30.0 * tick / TICKS

    def _crop_apply(self) -> None:
        left, top, wide, tall = self._crop_overlay.rect_f
        self._remember("crop")
        self.changes["crop"] = {
            "left": round(left, 4), "top": round(top, 4),
            "width": round(wide, 4), "height": round(tall, 4),
            "angle": round(self._crop_angle_value(), 2)}
        self.crop_button.setChecked(False)
        self._rebuild_mirror()

    def _start_wb_pick(self) -> None:
        if not self.current or not self.treatment:
            return
        self._picking_for = "@wb"
        self.frame.setCursor(Qt.CursorShape.CrossCursor)
        self._report(
            "Click something that should be neutral grey.")

    def _nudge_base_value(self, op: str, delta: float) -> None:
        """Move one whole-frame value BY an amount, in the op's own terms.

        A treatment may already hold the operation -- possibly in
        absolute mode, a 5400 K white balance -- and the dropper's answer
        is a correction on top of what is already there, not a
        replacement of it. An absent operation is inserted at the delta.
        """
        held = next(
            (item for item in self._pristine.get("operations", []) or []
             if isinstance(item, dict) and item.get("op") == op
             and isinstance(item.get("value"), (int, float))),
            None)
        if held is not None:
            key = str(held.get("id") or op)
            already = self.changes.get(key, {}).get("value")
            base = float(already if isinstance(already, (int, float))
                         else held["value"])
            self.changes.setdefault(key, {})["value"] = base + float(delta)
        else:
            inserts = self.changes.setdefault("+insert", [])
            entry = next((item for item in inserts
                          if item.get("op") == op), None)
            if entry is None:
                inserts.append({"op": op, "value": float(delta)})
            else:
                entry["value"] = float(entry.get("value", 0.0)) + float(delta)

    def _wb_from(self, red: float, green: float, blue: float) -> None:
        import math

        floor = 1e-4
        red, green, blue = (max(red, floor), max(green, floor),
                            max(blue, floor))
        # The engine warms by multiplying red and dividing blue by one
        # factor; a neutral click means solving that factor so the two
        # meet, then tint so green agrees.
        factor = math.sqrt(blue / red)
        temperature = max(-2000.0, min(2000.0, (factor - 1.0) * 5000.0))
        tint = max(-100.0, min(
            100.0, 200.0 * (1.0 - red * factor / green)))
        self._remember("white balance")
        self._nudge_base_value("color.temperature", round(temperature))
        self._nudge_base_value("color.tint", round(tint))
        self._rebuild_mirror()
        self._report(
            f"Neutralised: temperature {temperature:+.0f} K, "
            f"tint {tint:+.0f}.", "ok")

    def _start_pick(self, mask_id: str) -> None:
        self._picking_for = mask_id
        self.frame.setCursor(Qt.CursorShape.CrossCursor)
        self._report(
            "Click the picture on the colour this layer should select.")

    def _end_pick(self) -> None:
        self._picking_for = ""
        self.frame.unsetCursor()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        from PySide6.QtCore import QEvent

        if watched is self.frame and event.type() == QEvent.Type.Resize:
            if self._crop_overlay.isVisible():
                self._crop_overlay.setGeometry(self.frame.rect())
        if (watched is self.frame and self._picking_for
                and event.type() == QEvent.Type.MouseButtonPress):
            self._pick_at(event.position())
            return True
        return super().eventFilter(watched, event)

    def _pick_at(self, pos) -> None:
        """The hue under the click, in the picture's own pixels.

        The label centres an aspect-fit copy, so the click walks back
        through that placement; a click on the matting around the
        picture is no pick at all and the crosshair stays.
        """
        shown = self.frame.pixmap()
        if shown is None or shown.isNull():
            self._end_pick()
            return
        ratio = float(shown.devicePixelRatio() or 1.0)
        width, height = shown.width() / ratio, shown.height() / ratio
        left = (self.frame.width() - width) / 2.0
        top = (self.frame.height() - height) / 2.0
        x, y = pos.x() - left, pos.y() - top
        if not (0 <= x < width and 0 <= y < height):
            return
        image = shown.toImage()
        colour = image.pixelColor(int(x * ratio), int(y * ratio))
        red, green, blue = colour.redF(), colour.greenF(), colour.blueF()
        if self._picking_for == "@wb":
            self._end_pick()
            if max(red, green, blue) < 0.04 or min(red, green, blue) > 0.97:
                self._report(
                    "That spot is clipped -- black or blown -- and says "
                    "nothing about the light. Click a mid grey.", "alarm")
                return
            self._wb_from(red, green, blue)
            return
        brightest = max(red, green, blue)
        delta = brightest - min(red, green, blue)
        saturation = delta / brightest if brightest > 1e-6 else 0.0
        if saturation < 0.06:
            self._report(
                "That spot is nearly grey -- it has no honest hue to "
                "select by. Click something with colour in it.", "alarm")
            return
        if brightest == red:
            hue = ((green - blue) / delta) % 6
        elif brightest == green:
            hue = (blue - red) / delta + 2
        else:
            hue = (red - green) / delta + 4
        hue = round(hue * 60) % 360
        asked = self._picking_for
        self._end_pick()
        self._mask_changed(asked, {"geometry": {"hue": float(hue)}})
        self._show_controls()
        self._report(
            f"Picked hue {hue}° -- the layer now selects that colour.",
            "ok")

    def _mask_changed(self, key: str, change: dict) -> None:
        self._remember(f"{key} shape")
        held = self.changes.setdefault(key, {})
        if "geometry" in change:
            held.setdefault("geometry", {}).update(change["geometry"])
        if "enabled" in change:
            held["enabled"] = change["enabled"]
        # The recipe the overlay reads must be the recipe being rendered.
        self.recipe = adjustments.apply(self.recipe, {key: change})
        self.changes[key] = held
        if self._overlay_for == key:
            self._paint_overlay()
        self.render()

    def _layer_menu(self, where) -> None:
        from PySide6.QtWidgets import QMenu

        item = self.layers.itemAt(where)
        ordinal = self._layer_of_row(self.layers.row(item)) if item else 0
        if ordinal < 1:
            return
        menu = QMenu(self)
        menu.setFont(theme.body(10))
        rename = menu.addAction("Rename…")
        erase = menu.addAction("Erase this mask")
        chosen = menu.exec(self.layers.mapToGlobal(where))
        if chosen is rename:
            placed = {m["ordinal"]: m for m in adjustments.masks(self.recipe)}
            current = str(placed.get(ordinal, {}).get("label") or "")
            name, agreed = QInputDialog.getText(
                self, "Rename this mask", "Call it:", text=current)
            if agreed and name.strip():
                self._remember("rename mask")
                self._mask_structure(ordinal, {"label": name.strip()})
        elif chosen is erase:
            self._remember("erase mask")
            self._mask_structure(ordinal, {"deleted": True})
            if self.layer == ordinal:
                self.layer = 0
            self._rebuild_mirror()

    def _mask_structure(self, ordinal: int, change: dict) -> None:
        """A rename or an erasure, routed to where the mask lives.

        A mask the model wrote is addressed by its ordinal; a mask the
        photographer added still lives in the +mask list, and the change
        must land there or the next re-fold would resurrect it.
        """
        pristine_masks = sum(
            1 for item in self._pristine.get("operations", []) or []
            if isinstance(item, dict)
            and str(item.get("op", "")).startswith("mask.")
            and str(item.get("op", "")) != "mask.vignette"
            and isinstance(item.get("value"), dict))
        if ordinal <= pristine_masks:
            held = self.changes.setdefault(f"mask:{ordinal}", {})
            held.update(change)
        else:
            added = self.changes.get("+mask", [])
            index = ordinal - pristine_masks - 1
            if 0 <= index < len(added):
                if change.get("deleted"):
                    added[index]["erased"] = True
                if "label" in change:
                    added[index]["label"] = change["label"]
        self._rebuild_mirror()

    def _add_mask(self) -> None:
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.setFont(theme.body(10))
        for label in ("Radial — around a point",
                      "Linear — from an edge",
                      "Luma — a band of brightness",
                      "Color — a range of colour"):
            action = menu.addAction(label)
            action.setData(label.split(" ", 1)[0].lower())
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        self._create_mask(str(chosen.data()))

    def _create_mask(self, shape: str) -> None:
        self._remember("add mask")
        geometry = {"centre_x": 50.0, "centre_y": 50.0,
                    "radius": 30.0, "feather": 100, "reach": 40.0}
        if shape == "color":
            geometry = {"hue": 25.0, "range": 30.0, "softness": 20.0,
                        "sat_floor": 10.0, "feather": 100, "opacity": 100}
        made = {"shape": shape, "geometry": geometry,
                "effects": [{"op": "tone.exposure", "value": 0.0}]}
        self.changes.setdefault("+mask", []).append(made)
        self.recipe = adjustments.apply(self.recipe, {"+mask": [made]})
        # The new mask is the layer being worked on; select it.
        self.layer = len(adjustments.masks(self.recipe))
        self._show_controls()
        self.render()

    def _show_mask(self, key: str, on: bool) -> None:
        """Tint the proof where one mask lands, from the engine's weights."""
        self._overlay_for = key if on else ""
        if on:
            self._paint_overlay()
        elif self._plain_pixmap is not None:
            self.frame.set_source(self._plain_pixmap)

    def _paint_overlay(self) -> None:
        from PySide6.QtGui import QPainter, QPixmap

        from opencull_gui import maskpaint

        if self._plain_pixmap is None or not self._overlay_for:
            return
        placed = {item["id"]: item for item in adjustments.masks(self.recipe)}
        mask = placed.get(self._overlay_for)
        if mask is None:
            return
        # The value dict, straight off the recipe by ordinal.
        ordinal = int(self._overlay_for.split(":", 1)[1])
        values = [item["value"] for item in
                  self.recipe.get("operations", [])
                  if isinstance(item, dict)
                  and str(item.get("op", "")).startswith("mask.")
                  and str(item.get("op", "")) != "mask.vignette"
                  and isinstance(item.get("value"), dict)]
        if not 1 <= ordinal <= len(values):
            return
        try:
            reference = self.workspace._as_shot_preview(
                self.current, PROOF_EDGE)
            png = maskpaint.overlay_png(
                reference, mask["shape"], values[ordinal - 1])
        except Exception as exc:                     # noqa: BLE001 - shown
            self._report(f"The mask could not be shown: {exc}", "alarm")
            return
        wash = QPixmap()
        wash.loadFromData(png)
        composed = QPixmap(self._plain_pixmap)
        painter = QPainter(composed)
        painter.drawPixmap(composed.rect(), wash, wash.rect())
        painter.end()
        self.frame.set_source(composed)

    def _clear(self) -> None:
        self.controls = []
        self._overlay_for = ""
        self._holding = False
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _fill_layers(self) -> None:
        """The base layer and one row per mask, visibility on the row."""
        placed = adjustments.masks(self.recipe)
        if self.layer > len(placed):
            self.layer = 0
        self.layers.blockSignals(True)
        self.layers.clear()
        base = QListWidgetItem("  Base — the whole frame")
        base.setFont(theme.body(9))
        base.setFlags(Qt.ItemFlag.ItemIsEnabled
                      | Qt.ItemFlag.ItemIsSelectable)
        self.layers.addItem(base)
        for mask in placed:
            row = QListWidgetItem(
                f"  Mask · {mask['shape']} — {mask['label'][:44]}")
            row.setFont(theme.body(9))
            # The row remembers its mask's true ordinal: an erased layer
            # keeps its number forever, so rows and ordinals part ways.
            row.setData(Qt.ItemDataRole.UserRole, mask["ordinal"])
            row.setFlags(Qt.ItemFlag.ItemIsEnabled
                         | Qt.ItemFlag.ItemIsSelectable
                         | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked if mask["enabled"]
                              else Qt.CheckState.Unchecked)
            row.setToolTip(tooltip(
                f"{mask['label']}\nUntick to switch this mask off "
                "entirely; everything inside it stops. Right-click to "
                "rename or erase it."))
            self.layers.addItem(row)
        self.layers.setCurrentRow(self._row_of_layer(self.layer))
        self.layers.blockSignals(False)
        rows = self.layers.count()
        self.layers.setFixedHeight(rows * 26 + 10)

    def _row_of_layer(self, ordinal: int) -> int:
        for row in range(1, self.layers.count()):
            if self.layers.item(row).data(
                    Qt.ItemDataRole.UserRole) == ordinal:
                return row
        return 0

    def _layer_of_row(self, row: int) -> int:
        if row <= 0:
            return 0
        item = self.layers.item(row)
        return int(item.data(Qt.ItemDataRole.UserRole) or 0) if item else 0

    def _chose_layer(self, row: int) -> None:
        ordinal = self._layer_of_row(row)
        if row < 0 or ordinal == self.layer:
            return
        self.layer = ordinal
        self._show_controls()

    def _layer_visibility(self, item: QListWidgetItem) -> None:
        ordinal = self._layer_of_row(self.layers.row(item))
        if ordinal < 1:
            return
        on = item.checkState() == Qt.CheckState.Checked
        self._mask_changed(f"mask:{ordinal}", {"enabled": on})

    def _show_controls(self) -> None:
        # Rebuilding the column must not move it: ticking one control
        # into the recipe rebuilt everything and threw the scroll back
        # to the top, right out from under the hand that ticked it.
        held = (self._scroll.verticalScrollBar().value()
                if getattr(self, "_scroll", None) is not None else 0)
        self._clear()
        self._fill_layers()
        on_mask = self.layer > 0
        surface = (adjustments.mask_surface(self.recipe, self.layer)
                   if on_mask else adjustments.full_surface(self.recipe))
        compiled = [item for item in adjustments.full_surface(self.recipe)
                    if not item.get("absent")]
        self.keep_button.setEnabled(bool(compiled))
        self.reset_button.setEnabled(bool(compiled))
        advice = zones.load(
            Path(str(self.workspace.project.get("source_folder") or ".")),
            self.current) if self.current else {}
        if on_mask:
            self._show_geometry(self.layer)
        else:
            # The tone curve belongs to the whole frame, so it lives on
            # the base layer, above the sections. Seeded from the curve
            # already drawn -- pending change first, then the recipe's own
            # operation -- so reopening the frame reopens the same curve.
            pending = self.changes.get("curve")
            if isinstance(pending, dict) and pending.get("points"):
                points = pending["points"]
            else:
                drawn = next(
                    (item for item in self.recipe.get("operations", []) or []
                     if isinstance(item, dict)
                     and item.get("op") == "tone.curve"
                     and isinstance(item.get("value"), dict)),
                    None)
                points = (drawn["value"].get("points")
                          if drawn is not None else None)
            self.body.addWidget(self._heading("Curve", ""))
            curve = CurvePanel(points)
            curve.setToolTip(tooltip(
                "The classic RGB tone curve, on top of everything else "
                "the treatment does. Drag a point; click the line to add "
                "one; right-click a middle point to remove it. The line "
                "is the renderer's own interpolation -- what is drawn is "
                "what develops."))
            curve.changed.connect(self._curve_changed)
            self.body.addWidget(curve)
        # Every control of every section, always. The first shape folded
        # the unused ones behind a per-section count, and the fold read
        # as absence: the photographer this page is for looked at it and
        # asked where the rest of the controls were. A control the
        # treatment used comes first and carries its asked mark; the
        # rest sit quiet -- unchecked, slider still -- until touched,
        # which is what asks them into the recipe. The panel scrolls;
        # an instrument does not hide its keys to look shorter.
        by_section: dict[str, list[dict]] = {}
        for control in surface:
            by_section.setdefault(control["section"], []).append(control)
        touched = self._touched_sections()
        for section, members in by_section.items():
            present = [item for item in members if not item.get("absent")]
            self.body.addWidget(self._heading(
                section.upper() + (f"  ·  {len(present)}" if present else ""),
                section if section in touched else ""))
            # Canonical order, whether a control is in the recipe or not:
            # ticking one in used to promote it to the top of its section,
            # which moved it out from under the hand that ticked it.
            for control in members:
                widget = Control(control, advice.get(control["op"]))
                widget.changed.connect(self._control_changed)
                widget.wanted.connect(self._control_wanted)
                self.controls.append(widget)
                self.body.addWidget(widget)
        if not on_mask:
            self.body.addWidget(self._heading("Colour bands", ""))
            bands = HslPanel(adjustments.hsl_state(self.recipe))
            bands.setToolTip(tooltip(
                "Each band of colour, turned, saturated or lightened on "
                "its own. The same vocabulary the treatments write; here "
                "it answers to the hand."))
            bands.changed.connect(self._hsl_changed)
            self.body.addWidget(bands)
        for text in adjustments.guardrails(self.recipe):
            rail = QLabel(f"◆ {text}")
            rail.setObjectName("guardrail")
            rail.setWordWrap(True)
            rail.setFont(theme.body(8))
            rail.setToolTip(tooltip(
                "A promise this treatment made, which verification checks. "
                "It is shown rather than offered: switching it off would "
                "leave a certificate judging a claim the rendering no longer "
                "makes."))
            self.body.addWidget(rail)
        self.body.addStretch(1)
        if getattr(self, "_scroll", None) is not None and held:
            from PySide6.QtCore import QTimer

            self._hold_scroll = held
            self._scroll.verticalScrollBar().setValue(held)
            # The burst of layout growth is over well within this; after
            # it, the hand owns the scrollbar again.
            QTimer.singleShot(150, lambda: setattr(
                self, "_hold_scroll", 0))

    def _scroll_grew(self, _minimum: int, maximum: int) -> None:
        if self._hold_scroll:
            self._scroll.verticalScrollBar().setValue(
                min(self._hold_scroll, maximum))

    # --- moving them ------------------------------------------------------

    def _control_changed(self, key: str, change: dict) -> None:
        self._remember(str(key))
        if str(key).startswith("mask:") and "/" in str(key):
            head, _, op = str(key).partition("/")
            added = next(
                (item for item in (self.changes.get(head) or {}).get(
                    "add_effects", []) if item.get("op") == op), None)
            if added is not None and "value" in change:
                added["value"] = change["value"]
                if change.get("enabled") is False:
                    self.changes[head]["add_effects"].remove(added)
                self.render()
                return
            self.changes.setdefault(key, {}).update(change)
            self.render()
            return
        inserted = next(
            (item for item in self.changes.get("+insert", [])
             if item.get("op") == key), None)
        if inserted is not None and "value" in change:
            # The control was asked in by hand; its value lives on the
            # insert entry, not in a second change that apply() would
            # race it with.
            inserted["value"] = change["value"]
            if change.get("enabled") is False:
                self.changes["+insert"].remove(inserted)
            self.render()
            return
        self.changes.setdefault(key, {}).update(change)
        self.render()

    def _hsl_changed(self, band: str, component: str, value: float) -> None:
        self._remember(f"hsl {band} {component}")
        held = self.changes.setdefault("+hsl", [])
        entry = next((item for item in held
                      if item.get("channel") == band
                      and item.get("component") == component), None)
        if entry is None:
            held.append({"channel": band, "component": component,
                         "value": float(value)})
        else:
            entry["value"] = float(value)
        self.recipe = adjustments.apply(self.recipe, {"+hsl": [
            {"channel": band, "component": component,
             "value": float(value)}]})
        self.render()

    def _curve_changed(self, points: list) -> None:
        self._remember("curve")
        identity = points == [[0.0, 0.0], [255.0, 255.0]]
        if identity:
            self.changes.pop("curve", None)
        else:
            self.changes["curve"] = {"points": points}
        self.render()

    def _ask_zones(self) -> None:
        if not self.current:
            return
        self.zones_wanted.emit(self.current)
        self._report(
            f"Asked. The bands for {self.current} land beside the recipes "
            "and paint the next time this frame's controls are opened.")

    def _show_geometry(self, ordinal: int) -> None:
        """Where this layer's mask lands: its shape, movable, and shown."""
        placed = adjustments.masks(self.recipe)
        mask = next((item for item in placed
                     if item["ordinal"] == int(ordinal)), None)
        if mask is None:
            return
        geometry = dict(mask["geometry"])
        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel(f"THIS MASK  ·  {mask['shape']}")
        title.setObjectName("axisName")
        title.setFont(theme.display(7))
        row.addWidget(title)
        row.addStretch(1)
        show = QPushButton("Show")
        show.setObjectName("ghost")
        show.setFont(theme.body(8))
        show.setCheckable(True)
        show.setChecked(self._overlay_for == mask["id"])
        show.setCursor(Qt.CursorShape.PointingHandCursor)
        show.setToolTip(tooltip(
            "Tint the picture where this mask lands, from the "
            "renderer's own weights. What you see is what is blended."))
        show.toggled.connect(
            lambda on, key=mask["id"]: self._show_mask(key, on))
        row.addWidget(show)
        holder = QWidget()
        holder.setLayout(row)
        holder.layout().setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(holder)
        where = QLabel(mask["label"])
        where.setObjectName("rowPath")
        where.setWordWrap(True)
        where.setFont(theme.body(8))
        self.body.addWidget(where)
        key_prefix = mask["id"]
        if mask["shape"] == "radial":
            for key, label, low, high in (
                    ("centre_x", "Centre X", 0.0, 100.0),
                    ("centre_y", "Centre Y", 0.0, 100.0),
                    ("radius", "Radius", 1.0, 100.0),
                    ("feather", "Feather", 5.0, 100.0)):
                slider = GeometrySlider(
                    key, label, low, high, float(geometry.get(key, 50)))
                slider.changed.connect(
                    lambda k, v, m=key_prefix: self._mask_changed(
                        m, {"geometry": {k: v}}))
                self.body.addWidget(slider)
            inverted = QCheckBox("Inverted — everything except it")
            inverted.setChecked(bool(geometry.get("inverted")))
            inverted.setFont(theme.body(9))
            inverted.toggled.connect(
                lambda on, m=key_prefix: self._mask_changed(
                    m, {"geometry": {"inverted": bool(on)}}))
            self.body.addWidget(inverted)
        elif mask["shape"] == "linear":
            edge = QComboBox()
            edge.addItems(list(adjustments.EDGES))
            edge.setCurrentText(str(geometry.get("edge", "bottom")))
            edge.setFont(theme.body(9))
            edge.currentTextChanged.connect(
                lambda text, m=key_prefix: self._mask_changed(
                    m, {"geometry": {"edge": text}}))
            self.body.addWidget(edge)
            reach = GeometrySlider(
                "reach", "Reach", 5.0, 100.0,
                float(geometry.get("reach", 100)))
            reach.changed.connect(
                lambda k, v, m=key_prefix: self._mask_changed(
                    m, {"geometry": {k: v}}))
            self.body.addWidget(reach)
        elif mask["shape"] == "luma":
            band = QComboBox()
            band.addItems(list(adjustments.BANDS))
            band.setCurrentText(str(geometry.get("band", "shadows")))
            band.setFont(theme.body(9))
            band.currentTextChanged.connect(
                lambda text, m=key_prefix: self._mask_changed(
                    m, {"geometry": {"band": text}}))
            self.body.addWidget(band)
        elif mask["shape"] == "color":
            # Selected by what the pixels are, not where they sit: a hue
            # around a centre, softly, gated so near-grey stays out.
            pick = QPushButton("Pick the colour from the picture…")
            pick.setObjectName("ghost")
            pick.setFont(theme.body(9))
            pick.setCursor(Qt.CursorShape.PointingHandCursor)
            pick.setToolTip(tooltip(
                "Click, then click the picture: the hue under the click "
                "becomes this layer's colour. Sampled from the adjusted "
                "proof -- the picture as it looks right now."))
            pick.clicked.connect(
                lambda _checked=False, m=key_prefix: self._start_pick(m))
            self.body.addWidget(pick)
            wheel = [
                (0.0, (210, 60, 60)), (1 / 6, (210, 200, 60)),
                (2 / 6, (70, 190, 80)), (3 / 6, (60, 190, 200)),
                (4 / 6, (70, 90, 210)), (5 / 6, (200, 70, 200)),
                (1.0, (210, 60, 60))]
            for key, label, low, high in (
                    ("hue", "Hue", 0.0, 360.0),
                    ("range", "Range", 5.0, 120.0),
                    ("softness", "Softness", 0.0, 90.0),
                    ("sat_floor", "Sat floor", 0.0, 80.0)):
                slider = GeometrySlider(
                    key, label, low, high, float(geometry.get(key, 20)),
                    ramp=wheel if key == "hue" else None)
                slider.changed.connect(
                    lambda k, v, m=key_prefix: self._mask_changed(
                        m, {"geometry": {k: v}}))
                self.body.addWidget(slider)
            inverted = QCheckBox("Inverted — everything except it")
            inverted.setChecked(bool(geometry.get("inverted")))
            inverted.setFont(theme.body(9))
            inverted.toggled.connect(
                lambda on, m=key_prefix: self._mask_changed(
                    m, {"geometry": {"inverted": bool(on)}}))
            self.body.addWidget(inverted)

    def _heading(self, title: str, resettable: str) -> QWidget:
        """A section's name -- and, where the section has been moved,
        the small word that puts it back.

        A widget, not a bare layout: _clear() walks the body deleting
        widgets, and a layout's children are invisible to it -- the
        first build of this as a QHBoxLayout left every heading's
        corpse painted over the next fill.
        """
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("axisName")
        heading.setFont(theme.display(7))
        row.addWidget(heading)
        row.addStretch(1)
        if resettable:
            undo = QPushButton("reset")
            undo.setObjectName("ghost")
            undo.setFont(theme.body(8))
            undo.setCursor(Qt.CursorShape.PointingHandCursor)
            undo.setToolTip(tooltip(
                f"Put every {resettable.lower()} move back to what the "
                "model asked for, leaving the other sections alone."))
            undo.clicked.connect(
                lambda _=False, s=resettable: self.reset_section(s))
            row.addWidget(undo)
        return holder

    def _touched_sections(self) -> set[str]:
        """Which of the ACTIVE layer's sections have been moved."""
        touched: set[str] = set()
        if self.layer > 0:
            head = f"mask:{self.layer}"
            for key, change in self.changes.items():
                if key == head:
                    for item in change.get("add_effects", []) or []:
                        touched.add(adjustments._section_of(
                            str(item.get("op", ""))))
                elif str(key).startswith(head + "/"):
                    touched.add(adjustments._section_of(
                        str(key).partition("/")[2]))
            touched.discard("Other")
            return touched
        surface = {item["id"]: item["section"]
                   for item in adjustments.full_surface(self.recipe)}
        for key, change in self.changes.items():
            if str(key).startswith("mask:") or key == "+mask":
                continue
            if key == "+insert":
                for item in change:
                    touched.add(
                        adjustments._section_of(str(item.get("op", ""))))
            else:
                touched.add(surface.get(
                    key, adjustments._section_of(str(key))))
        touched.discard("Other")
        return touched

    def _control_wanted(self, op: str, value: float) -> None:
        """An absent operation, asked into the active layer by hand.

        The ask rides the changes dict -- the only payload the render
        path carries -- so the renderer, the cache identity and the
        full-size keep all see the same insertion. On the base layer it
        is a whole-frame insert; on a mask layer it becomes one of that
        mask's effects.
        """
        self._remember(f"want {op}")
        if self.layer > 0:
            key = f"mask:{self.layer}"
            added = self.changes.setdefault(key, {}).setdefault(
                "add_effects", [])
            if not any(item.get("op") == op for item in added):
                added.append({"op": op, "value": value})
            else:
                for item in added:
                    if item.get("op") == op:
                        item["value"] = value
            self.recipe = adjustments.apply(
                self.recipe, {key: {"add_effects": [
                    {"op": op, "value": value}]}})
            self._show_controls()
            self.render()
            return
        inserts = self.changes.setdefault("+insert", [])
        if not any(item.get("op") == op for item in inserts):
            inserts.append({"op": op, "value": value})
        else:
            for item in inserts:
                if item.get("op") == op:
                    item["value"] = value
        self.recipe = adjustments.apply(
            self.recipe, {"+insert": [{"op": op, "value": value}]})
        self._show_controls()
        self.render()

    def speak(self) -> None:
        """Move the controls by saying so.

        The words compile locally through the recipe grammar; what was
        heard moves, what was not is said back. Free words a model would
        have to interpret are named as exactly that, not swallowed.
        """
        text = self.prompt.text().strip()
        if not text:
            return
        heard, unheard = adjustments.compile_words(text)
        by_op = {widget.control["op"]: widget for widget in self.controls}
        moved: list[str] = []
        absent: list[str] = []
        for operation in heard:
            widget = by_op.get(str(operation["op"]))
            label = adjustments.LABELS.get(
                str(operation["op"]), str(operation["op"]))
            if widget is None:
                absent.append(label)
                continue
            if str(operation.get("mode")) == "absolute":
                value = float(operation["value"])
            else:
                value = widget.value() + float(operation["value"])
            widget.slider.setValue(
                widget._tick(adjustments.clamp(widget.control, value)))
            moved.append(
                f"{label} {adjustments.written(widget.value(), widget.control['unit'])}")
        parts = []
        if moved:
            parts.append("Moved " + " · ".join(moved) + ".")
        if absent:
            parts.append(
                "This treatment has no "
                + ", ".join(dict.fromkeys(absent))
                + " to move.")
        if unheard:
            parts.append(
                "Not understood: "
                + "; ".join(f"“{phrase}”" for phrase in unheard)
                + " — free words need a model to read them, and that is "
                "not built yet.")
        self._report(" ".join(parts) or "Nothing to do.",
                     "alarm" if (unheard or absent) and not moved else "ok")
        if moved and not unheard:
            self.prompt.clear()

    def reset(self) -> None:
        """Put every control back to what the model asked for.

        Structure too: an inserted operation or an added mask is a
        change like any other, and "as suggested" that kept them would
        be a third state with nobody's name on it.
        """
        self._remember("as suggested")
        self.changes = {}
        self._rebuild_mirror()
        self._report("Back to the treatment as it was suggested.")

    def reset_to_shot(self) -> None:
        """Every operation off: the frame as the camera developed it.

        Not an erasure -- a changes dict that disables the pristine
        recipe's every operation and mask, so the same payload the
        renderer always gets carries this state too, the switches stay
        on the page to be turned back on one by one, and As suggested
        is still one click away.
        """
        self._remember("as shot")
        turned_off: dict[str, Any] = {
            item["id"]: {"enabled": False}
            for item in adjustments.controls(self._pristine)}
        for index in range(len(adjustments.masks(self._pristine))):
            turned_off[f"mask:{index + 1}"] = {"enabled": False}
        self.changes = turned_off
        self._rebuild_mirror()
        self._report(
            "Everything is switched off; the frame stands as shot. "
            "Build up from here, or take As suggested to restore the "
            "treatment.")

    def reset_section(self, section: str) -> None:
        """Put one section of the ACTIVE layer back, the rest standing."""
        self._remember("reset section")
        kept: dict[str, Any] = {}
        if self.layer > 0:
            head = f"mask:{self.layer}"
            for key, change in self.changes.items():
                if key == head and isinstance(change, dict):
                    remaining = dict(change)
                    remaining["add_effects"] = [
                        item for item in change.get("add_effects", []) or []
                        if adjustments._section_of(
                            str(item.get("op", ""))) != section]
                    if not remaining["add_effects"]:
                        remaining.pop("add_effects")
                    if remaining:
                        kept[key] = remaining
                    continue
                if (str(key).startswith(head + "/")
                        and adjustments._section_of(
                            str(key).partition("/")[2]) == section):
                    continue
                kept[key] = change
        else:
            surface = {item["id"]: item["section"]
                       for item in adjustments.full_surface(self.recipe)}
            for key, change in self.changes.items():
                if key == "+insert":
                    remaining = [
                        item for item in change
                        if adjustments._section_of(str(item.get("op", "")))
                        != section]
                    if remaining:
                        kept[key] = remaining
                    continue
                if str(key).startswith("mask:") or key == "+mask":
                    kept[key] = change
                    continue
                if surface.get(
                        key, adjustments._section_of(str(key))) == section:
                    continue
                kept[key] = change
        self.changes = kept
        self._rebuild_mirror()
        self._report(f"{section} is back to what was asked.")

    def moved(self) -> int:
        return len(adjustments.moved(
            adjustments.apply(self.recipe, self.changes)))

    def proof_edge(self) -> int:
        """The pixels the preview can actually show, and no more.

        Fine tuning re-renders on every slider move, and it was paying
        for a 1600px proof to fill a pane that is usually far smaller.
        The edge is the frame's own longest side in physical pixels --
        device ratio included -- snapped UP to the next 256 so a window
        nudged a few pixels does not orphan the render cache, floored
        where a collapsed layout would otherwise ask for a thumbnail,
        and capped at the full proof: bigger than the screen buys
        nothing the screen can show.
        """
        shown = max(self.frame.width(), self.frame.height())
        if shown < 64:
            return PROOF_EDGE
        physical = shown * float(self.frame.devicePixelRatioF())
        snapped = int(-(-physical // 256) * 256)
        return max(512, min(snapped, PROOF_EDGE))

    def render(self) -> None:
        if not self.current or not self.treatment:
            return
        # The caption follows the pixels, not the request: it is set by
        # _rendered when the picture actually changes. Until then the
        # pane honestly shows the frame as shot, and says so.
        asked = self.changes
        if self.crop_button.isChecked():
            # Framing happens on the whole photograph: the proof under
            # the rectangle is rendered without the crop being framed.
            asked = {key: value for key, value in self.changes.items()
                     if key != "crop"}
        self.renderer.render(
            self.current, self.treatment, self.engine(), self._demosaic(),
            adjustments=asked or None, maximum=self.proof_edge())
        self._render_pending = True
        self._prepare_timer.start()

    def _demosaic(self) -> str:
        return str(self.workspace.payload().get("rendering", {}).get(
            "demosaic") or "markesteijn-3-pass")

    def _thumb_ready(self, photo: str, treatment: str, pixmap) -> None:
        self.previews[(photo, treatment)] = pixmap
        if photo != self.current:
            return
        for row, item in enumerate(self.treatments):
            if str(item.get("id")) == treatment:
                entry = self.treatment_list.item(row)
                if entry is not None:
                    entry.setIcon(plain_icon(pixmap))
                break

    def _still_preparing(self) -> None:
        if self._render_pending:
            self._preparing.adjustSize()
            self._preparing.move(
                self.frame.width() - self._preparing.width() - 12,
                self.frame.height() - self._preparing.height() - 12)
            self._preparing.raise_()
            self._preparing.show()

    def _rendered(self, photo: str, treatment: str, pixmap) -> None:
        if photo != self.current or treatment != self.treatment:
            return
        self._render_pending = False
        self._preparing.hide()
        if self.crop_button.isChecked():
            self._crop_base = pixmap
            self._crop_overlay.setGeometry(self.frame.rect())
            self._crop_overlay.raise_()
            self._crop_overlay.update()
        self._plain_pixmap = pixmap
        if self._as_shot_pixmap is None:
            try:
                self._as_shot_pixmap = load_for_screen(
                    self.workspace._as_shot_preview(
                        photo, self.proof_edge()))
            except Exception:                        # noqa: BLE001 - no hold
                self._as_shot_pixmap = None
        if not self._holding:
            self.caption.setText(
                "AS ADJUSTED" if self.changes else "AS SUGGESTED")
            self.frame.set_source(pixmap)
            # Always the plain rendering: a mask overlay's tint would
            # pollute the channels with the tint's own colour.
            self.histogram.show_pixmap(pixmap)
            if self._overlay_for:
                self._paint_overlay()

    def hold(self, holding: bool) -> None:
        """The frame as shot, for as long as the key is down.

        The judgement a slider asks for is "better than what the camera
        gave me?", and that question is answered in one place at one
        size -- not by memory of another page.
        """
        if holding == self._holding:
            return
        self._holding = holding
        if holding and self._as_shot_pixmap is not None:
            self.caption.setText("AS SHOT")
            self.frame.set_source(self._as_shot_pixmap)
            # The comparison is honest end to end: the curves flip to the
            # camera's rendering with the picture, and back.
            self.histogram.show_pixmap(self._as_shot_pixmap)
        elif self._plain_pixmap is not None:
            self.caption.setText(
                "AS ADJUSTED" if self.changes else "AS SUGGESTED")
            self.frame.set_source(self._plain_pixmap)
            self.histogram.show_pixmap(self._plain_pixmap)
            if self._overlay_for:
                self._paint_overlay()

    def _render_failed(self, photo: str, reason: str) -> None:
        self._render_pending = False
        self._preparing.hide()
        self._report(f"{photo} could not be rendered: {reason}", "alarm")

    def keep(self) -> None:
        """Record this version at full size, beside the one it came from."""
        if not self.current or not self.treatment:
            return
        if not self.changes:
            self._report(
                "Nothing has been moved, so this is the treatment as "
                "suggested. Develop it from the development page.", "alarm")
            return
        self.keep_button.setEnabled(False)
        self._report(
            f"Rendering {self.current} at full size with your adjustments. "
            "It is recorded as its own version, not as the treatment.")
        try:
            record = self.workspace.render_full(
                self.current, self.treatment, self.engine(),
                self._demosaic(), adjustments=self.changes)
        except Exception as exc:                     # noqa: BLE001 - reported
            self.keep_button.setEnabled(True)
            self._report(f"It could not be rendered: {exc}", "alarm")
            return
        self.keep_button.setEnabled(True)
        variant = str((record.get("render") or {}).get("variant") or "")
        self._report(
            f"Kept as {variant}. It is on the export page beside the "
            "treatment it came from.", "ok")

    # --- keeping a look ---------------------------------------------------

    def preset_operations(self) -> list[dict[str, Any]]:
        """This version's adjustments, as a look rather than as a render.

        The compiled recipe with the photographer's moves folded in is
        exactly what the renderer would execute, which is what makes it
        worth keeping: what gets saved is what they were looking at, not
        the prose that started it.
        """
        if not self.recipe:
            return []
        applied = adjustments.apply(self.recipe, self.changes)
        return presets.portable_operations(applied.get("operations", []))

    def save_preset(self) -> None:
        name, said = self.ask_preset_name()
        if not said:
            return
        try:
            kept = presets.save(
                name, self.preset_operations(),
                intent=f"Kept from the {self._treatment_name()} treatment "
                       f"of {self.current}.",
                origin_note={"photo": self.current,
                             "treatment": self.treatment},
                root=self.workspace.presets_root)
        except presets.PresetError as exc:
            self._report(str(exc), "alarm")
            return
        self._report(
            f"Kept as the preset “{kept['name']}”. It is on every "
            "photograph's treatment list, under Presets.", "ok")

    def save_recipe_file(self) -> None:
        """Write this version as a portable recipe file.

        The same format the import button reads, so what leaves this
        machine can arrive on another one -- or come back to this one --
        and render identically. The whole recipe travels, masks and
        all; a preset deliberately carries only the portable moves.
        """
        from PySide6.QtWidgets import QFileDialog

        if not self.recipe:
            return
        applied = adjustments.apply(self.recipe, self.changes)
        suggested = str(
            Path.home() / f"{Path(self.current).stem}-"
            f"{self._treatment_name().lower().replace(' ', '-')}.recipe.json")
        chosen, _filter = QFileDialog.getSaveFileName(
            self, "Save as recipe file", suggested,
            "Darkimiya recipe (*.json)")
        if not chosen:
            return
        payload = {
            "format": "darkimiya-portable-recipe-v1",
            "name": f"{self._treatment_name()} — {Path(self.current).stem}",
            "photo": "",
            "recipe": applied,
        }
        try:
            Path(chosen).write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8")
        except OSError as exc:
            self._report(f"That could not be written: {exc}", "alarm")
            return
        self._report(
            f"Written to {Path(chosen).name}. Any Darkimiya can import it "
            "from the develop page, and it renders exactly this.", "ok")

    def ask_preset_name(self) -> tuple[str, bool]:
        suggested = f"{self._treatment_name()} — {Path(self.current).stem}"
        return QInputDialog.getText(
            self, "Save as preset", "Call this look:", text=suggested)

    def _treatment_name(self) -> str:
        return next(
            (str(item["name"]) for item in self.treatments
             if item["id"] == self.treatment), self.treatment)

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def step(self, delta: int) -> None:
        if not self.photos:
            return
        row = (self.list.currentRow() + delta) % len(self.photos)
        self.list.setCurrentRow(row)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.closed.emit()
        elif key == Qt.Key.Key_B and not event.isAutoRepeat():
            self.hold(True)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_J):
            self.step(1)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_K):
            self.step(-1)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_B and not event.isAutoRepeat():
            self.hold(False)
        else:
            super().keyReleaseEvent(event)

    def shutdown(self) -> None:
        self.renderer.shutdown()
