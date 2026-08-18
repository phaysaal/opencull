"""A slider whose groove says how far is wise.

The groove is painted in three bands: teal where a professional would
move without comment, amber where the move is an artistic statement,
and red where the photograph is being damaged for nothing. The bands
are advice at the exact place the decision is made; the handle still
travels the compiler's whole range, because the photographer outranks
the advice.

Two marks ride above the groove: a hollow notch where the model asked
for the value, and a hairline at neutral. A standard QSlider cannot
paint any of this -- a stylesheet grooves in one brush -- so this is
a QSlider subclass that draws its own groove and handle and keeps the
stock mouse behaviour.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSlider

# The bands, on both themes. Muted on purpose: the groove is advice,
# not a warning lamp, and seventeen glowing sliders are a fairground.
_RED = QColor(157, 66, 58)
_AMBER = QColor(172, 122, 51)
_TEAL = QColor(58, 130, 118)
_GROOVE_OFF = QColor(70, 70, 74)
_HANDLE = QColor(232, 232, 235)
_HANDLE_EDGE = QColor(20, 20, 22)
_ASKED = QColor(240, 240, 244)
_NEUTRAL_TICK = QColor(140, 140, 146)


class ZoneSlider(QSlider):
    """A horizontal slider with safe / artistic / damage bands."""

    GROOVE = 6
    HANDLE = 14

    RAMP = 3

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        # Fractions of the slider's range, settled by set_zones.
        self._bands: list[tuple[float, float, QColor]] = []
        self._asked_fraction: float | None = None
        self._neutral_fraction: float | None = None
        # The colour ramp under the groove, for controls with a natural
        # axis: temperature blue to amber, tint green to magenta,
        # saturation grey to vivid. It says which way does what, where
        # the bands above it say how far is wise -- two answers, two
        # strips, neither painted over the other. Indicative, not a
        # preview: it teaches the direction, it does not promise what
        # this frame's own pixels will do.
        self._ramp: list[tuple[float, QColor]] = []
        self.setMinimumHeight(26)
        # The wheel belongs to the panel. A column of thirty sliders is
        # scrolled with the wheel, and any slider under the passing
        # pointer that grabs it changes a value nobody meant to change.
        # Adjusting is the drag and the keyboard; scrolling is the wheel.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Held Ctrl or Shift says "I mean this control": the wheel then
        # adjusts. Bare, the wheel stays the panel's and scrolls.
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.ShiftModifier):
            super().wheelEvent(event)
            event.accept()
            return
        event.ignore()

    def set_ramp(self, stops: list[tuple[float, tuple[int, int, int]]]) -> None:
        """A left-to-right colour ramp; empty paints nothing."""
        self._ramp = [(float(at), QColor(*rgb)) for at, rgb in stops]
        self.setMinimumHeight(30 if self._ramp else 26)
        self.update()

    # --- what the paint needs, in range fractions -------------------------

    def set_zones(self, low: float, high: float,
                  safe: tuple[float, float],
                  artistic: tuple[float, float]) -> None:
        """Paint bands for a control spanning [low, high]."""
        span = high - low
        if span <= 0:
            self._bands = []
            self.update()
            return

        def fraction(value: float) -> float:
            return max(0.0, min(1.0, (value - low) / span))

        s0, s1 = fraction(safe[0]), fraction(safe[1])
        a0, a1 = fraction(artistic[0]), fraction(artistic[1])
        self._bands = [
            (0.0, a0, _RED),
            (a0, s0, _AMBER),
            (s0, s1, _TEAL),
            (s1, a1, _AMBER),
            (a1, 1.0, _RED),
        ]
        self.update()

    def set_marks(self, low: float, high: float,
                  asked: float | None, neutral: float | None) -> None:
        span = high - low
        if span <= 0:
            self._asked_fraction = self._neutral_fraction = None
        else:
            self._asked_fraction = (
                max(0.0, min(1.0, (asked - low) / span))
                if asked is not None else None)
            self._neutral_fraction = (
                max(0.0, min(1.0, (neutral - low) / span))
                if neutral is not None else None)
        self.update()

    # --- painting ---------------------------------------------------------

    def _groove_rect(self) -> QRectF:
        margin = self.HANDLE / 2 + 1
        return QRectF(margin, (self.height() - self.GROOVE) / 2,
                      self.width() - 2 * margin, self.GROOVE)

    def _x_of(self, fraction: float, groove: QRectF) -> float:
        return groove.left() + fraction * groove.width()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        groove = self._groove_rect()
        radius = self.GROOVE / 2

        if self.isEnabled() and self._bands:
            for start, stop, colour in self._bands:
                if stop - start <= 0:
                    continue
                band = QRectF(self._x_of(start, groove), groove.top(),
                              (stop - start) * groove.width(), groove.height())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(colour)
                painter.drawRoundedRect(band, radius, radius)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_GROOVE_OFF)
            painter.drawRoundedRect(groove, radius, radius)

        if self._ramp and self.isEnabled():
            strip = QRectF(groove.left(), groove.bottom() + 3,
                           groove.width(), self.RAMP)
            gradient = QLinearGradient(strip.left(), 0.0, strip.right(), 0.0)
            for at, colour in self._ramp:
                gradient.setColorAt(at, colour)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRoundedRect(strip, self.RAMP / 2, self.RAMP / 2)

        # Neutral hairline, under the asked notch.
        if self._neutral_fraction is not None and self.isEnabled():
            x = self._x_of(self._neutral_fraction, groove)
            painter.setPen(QPen(_NEUTRAL_TICK, 1))
            painter.drawLine(int(x), int(groove.top() - 4),
                             int(x), int(groove.bottom() + 4))

        # Where the model asked for the value: a hollow notch that stays
        # put while the handle moves, so the disagreement is visible.
        if self._asked_fraction is not None and self.isEnabled():
            x = self._x_of(self._asked_fraction, groove)
            painter.setPen(QPen(_ASKED, 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(x - 2.4, groove.top() - 3.6,
                                    4.8, groove.height() + 7.2))

        # The handle.
        span = self.maximum() - self.minimum()
        fraction = ((self.value() - self.minimum()) / span) if span else 0.0
        x = self._x_of(fraction, groove)
        centre_y = groove.center().y()
        painter.setPen(QPen(_HANDLE_EDGE, 1))
        painter.setBrush(_HANDLE if self.isEnabled() else _GROOVE_OFF)
        painter.drawEllipse(QRectF(x - self.HANDLE / 2,
                                   centre_y - self.HANDLE / 2,
                                   self.HANDLE, self.HANDLE))
        painter.end()
