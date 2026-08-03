"""Reusable pieces of the Darkimiya interface."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme


class ElidedLabel(QLabel):
    """A label that shortens its text rather than widening its row.

    QLabel has no eliding of its own, so a long path pushed the controls
    beside it out of the window.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full = text
        super().setText(text)
        self.setToolTip(text)

    def full_text(self) -> str:
        return self._full

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        elided = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, self.width())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect(), int(self.alignment()), elided)
        painter.end()


class SprocketEdge(QWidget):
    """Film perforations down the leading edge of a row.

    Painted rather than styled so that the same widget can advance them while
    work is in flight: film moving through the machine.
    """

    PITCH = 16
    HOLE = 5
    WIDTH = 3

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedWidth(13)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._offset = 0.0
        self._running = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def is_running(self) -> bool:
        return self._running

    def set_running(self, running: bool) -> None:
        if running == self._running:
            return
        self._running = running
        if running:
            self._timer.start(60)
        else:
            self._timer.stop()
            self._offset = 0.0
        self.update()

    def _advance(self) -> None:
        self._offset = (self._offset + 1.0) % self.PITCH
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(theme.SAFELIGHT if self._running else theme.PAPER)
        colour.setAlphaF(0.85 if self._running else 0.16)
        painter.setPen(Qt.PenStyle.NoPen)
        x = (self.width() - self.WIDTH) / 2
        y = -self.PITCH + self._offset
        while y < self.height() + self.PITCH:
            path = QPainterPath()
            path.addRoundedRect(x, y, self.WIDTH, self.HOLE, 1.2, 1.2)
            painter.fillPath(path, colour)
            y += self.PITCH
        painter.end()


class Row(QFrame):
    """One folder or one job."""

    def __init__(
        self,
        name: str,
        path: str,
        state_label: str,
        tone: str = "",
        actions: list[tuple[str, Callable[[], None]]] | None = None,
        progress: int | None = None,
        last: bool = False,
    ):
        super().__init__()
        self.setObjectName("rowLast" if last else "row")
        self.name = name
        self.tone = tone

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 14, 0)
        layout.setSpacing(14)

        self.sprocket = SprocketEdge()
        self.sprocket.set_running(tone == "running")
        layout.addWidget(self.sprocket)

        column = QVBoxLayout()
        column.setContentsMargins(4, 11, 0, 11)
        column.setSpacing(3)

        title = QLabel(name)
        title.setObjectName("rowName")
        title.setFont(theme.body(10, weight=title.font().Weight.DemiBold))
        column.addWidget(title)

        self.path_label = ElidedLabel(path)
        self.path_label.setObjectName("rowPath")
        self.path_label.setFont(theme.mono(8))
        column.addWidget(self.path_label)

        if progress is not None:
            self.meter = QProgressBar()
            self.meter.setObjectName("meter")
            self.meter.setRange(0, 100)
            self.meter.setValue(max(0, min(100, progress)))
            self.meter.setTextVisible(False)
            self.meter.setFixedHeight(3)
            column.addWidget(self.meter)
        layout.addLayout(column, 1)

        self.badge = QLabel(state_label.upper())
        self.badge.setObjectName("rowState")
        self.badge.setProperty("tone", tone)
        self.badge.setFont(theme.display(8))
        layout.addWidget(self.badge)

        for label, handler in actions or []:
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFont(theme.body(9))
            button.clicked.connect(lambda _=False, run=handler: run())
            layout.addWidget(button)


def band(title: str) -> tuple[QWidget, QVBoxLayout]:
    """A titled section containing a rounded list of rows."""
    section = QWidget()
    layout = QVBoxLayout(section)
    layout.setContentsMargins(0, 22, 0, 0)
    layout.setSpacing(10)

    heading = QLabel(title.upper())
    heading.setObjectName("bandTitle")
    heading.setFont(theme.display(8))
    layout.addWidget(heading)

    rows = QFrame()
    rows.setObjectName("rows")
    inner = QVBoxLayout(rows)
    inner.setContentsMargins(0, 0, 0, 0)
    inner.setSpacing(0)
    layout.addWidget(rows)
    return section, inner


def replace_rows(layout: QVBoxLayout, widgets: list[QWidget]) -> None:
    """Swap a list's contents, disposing of the widgets it held."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
    for widget in widgets:
        layout.addWidget(widget)
