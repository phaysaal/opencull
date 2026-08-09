"""Reusable pieces of the Darkimiya interface."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
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


def workspace_title(report, photos_root=None) -> str:
    """Name a workspace for the shoot, not for the file it was written to.

    A report is named after the folder plus how it was made, so the raw stem
    puts machinery in the window's title: "A Journey To Matsushima-manual-
    selection". The folder is what the photographer calls the work.
    """
    if photos_root is not None:
        name = Path(str(photos_root)).name
        if name:
            return name
    stem = Path(str(getattr(report, "path", report))).stem
    for suffix in ("-results", "-manual-selection"):
        stem = stem.removesuffix(suffix)
    return stem


class Paragraph(QLabel):
    """A wrapped paragraph that is as tall as its own text.

    A QLabel with wordWrap knows its height only once its width is settled,
    and the layout holding it asks for a height before that -- so the last
    line of a two-line paragraph is cut off. Asking Qt for the height at the
    width the label actually got, each time it gets one, is the only answer
    that survives the window being resized.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        self._fit()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        width = self.width()
        if width > 0:
            self.setMinimumHeight(self.heightForWidth(width))


def short_path(value: str) -> str:
    """A path with the home directory written the way people write it."""
    home = str(Path.home())
    return f"~{value[len(home):]}" if value.startswith(home) else value


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


class IconButton(QPushButton):
    """A small painted glyph, for an action that needs no word beside it."""

    def __init__(self, glyph: str = "remove", tip: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.glyph = glyph
        self.setObjectName("icon")
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        if tip:
            self.setToolTip(tip)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(theme.ALARM if self.underMouse() else theme.FAINT)
        pen = QPen(colour)
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        # A cross, not a bin: this removes the folder from the library, it
        # does not destroy anything inside it.
        box = self.rect().adjusted(9, 9, -9, -9)
        painter.drawLine(box.topLeft(), box.bottomRight())
        painter.drawLine(box.topRight(), box.bottomLeft())
        painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().leaveEvent(event)
        self.update()


class Filmstrip(QWidget):
    """Frames from a folder, shown as a short strip of film.

    A photographer recognises a shoot by its pictures, not by its folder
    name, so the library shows the pictures. The perforations are the same
    signature the rows carried, turned on its side.
    """

    PERF = 5
    PITCH = 14

    clicked = Signal()

    def __init__(self, count: int = 3, parent: QWidget | None = None):
        super().__init__(parent)
        self.count = count
        self._pixmaps: list = [None] * count
        self._opens = False
        self.setMinimumHeight(120)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_frame(self, index: int, pixmap) -> None:
        if 0 <= index < self.count:
            self._pixmaps[index] = pixmap
            self.update()

    def clear(self) -> None:
        self._pixmaps = [None] * self.count
        self.update()

    def set_opens(self, opens: bool, tip: str = "") -> None:
        """Whether the pictures are a way in, and where to.

        A strip that cannot be opened must not look as though it can, so the
        cursor and the hover cue both follow this rather than being set once.
        """
        self._opens = opens
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if opens
            else Qt.CursorShape.ArrowCursor)
        self.setToolTip(tip if opens else "")
        self.setMouseTracking(opens)
        self.update()

    def opens(self) -> bool:
        return self._opens

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._opens and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(theme.INK))

        margin = 9
        window = QRect(
            0, margin, self.width(), max(0, self.height() - margin * 2))
        cell = self.width() / self.count if self.count else self.width()
        for index in range(self.count):
            box = QRect(
                int(index * cell), window.top(),
                int(cell) - 1, window.height())
            pixmap = self._pixmaps[index]
            if pixmap is None or pixmap.isNull():
                painter.fillRect(box, QColor(theme.SURFACE))
                continue
            # Fill the cell and crop, so a strip reads as one continuous film
            # rather than as letterboxed thumbnails.
            scaled_pixmap = pixmap.scaled(
                box.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            offset_x = (scaled_pixmap.width() - box.width()) // 2
            offset_y = (scaled_pixmap.height() - box.height()) // 2
            painter.drawPixmap(
                box, scaled_pixmap,
                QRect(offset_x, offset_y, box.width(), box.height()))

        # Perforations along both edges of the strip.
        colour = QColor(theme.PAPER)
        colour.setAlphaF(0.20)
        painter.setPen(Qt.PenStyle.NoPen)
        x = 4.0
        while x < self.width():
            for top in (2.0, self.height() - margin + 2.0):
                path = QPainterPath()
                path.addRoundedRect(x, top, self.PERF, 3.0, 1.0, 1.0)
                painter.fillPath(path, colour)
            x += self.PITCH

        if self._opens and self.underMouse():
            # A light on the frames, not the safelight: amber here would say
            # work is in flight, which is what it means everywhere else.
            wash = QColor(theme.PAPER)
            wash.setAlphaF(0.07)
            painter.fillRect(window, wash)
        painter.end()


class ProjectCard(QFrame):
    """One folder in the library, shown by its frames."""

    def __init__(
        self,
        name: str,
        path: str,
        subtitle: str,
        state_label: str,
        tone: str = "",
        actions: list[tuple[str, Callable[[], None]]] | None = None,
        progress: int | None = None,
        frames: int = 3,
        on_remove: Callable[[], None] | None = None,
        on_open: Callable[[], None] | None = None,
        open_hint: str = "",
    ):
        super().__init__()
        self.setObjectName("card")
        self.name = name
        self.tone = tone
        # Uniform, so a grid of folders reads as a grid rather than as a
        # ragged column: a card carries a meter or buttons, rarely both.
        self.setFixedSize(316, 268)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.strip = Filmstrip(frames)
        self.strip.setFixedHeight(132)
        # The pictures are the obvious way into a folder, so they are one.
        # They lead where the folder's own button leads and nowhere else: a
        # card whose button is absent has nothing for the strip to open.
        if on_open is not None:
            self.strip.set_opens(True, open_hint)
            self.strip.clicked.connect(on_open)
        layout.addWidget(self.strip)

        body = QVBoxLayout()
        body.setContentsMargins(14, 12, 14, 13)
        body.setSpacing(3)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel(name)
        title.setObjectName("cardName")
        title.setFont(theme.body(11, weight=title.font().Weight.DemiBold))
        header.addWidget(title, 1)

        self.badge = QLabel(state_label.upper())
        self.badge.setObjectName("rowState")
        self.badge.setProperty("tone", tone)
        self.badge.setFont(theme.display(8))
        header.addWidget(self.badge)
        body.addLayout(header)

        self.path_label = ElidedLabel(path)
        self.path_label.setObjectName("rowPath")
        self.path_label.setFont(theme.mono(8))
        body.addWidget(self.path_label)

        if subtitle:
            count = QLabel(subtitle)
            count.setObjectName("cardCount")
            count.setFont(theme.body(9))
            body.addWidget(count)

        if progress is not None:
            self.meter = QProgressBar()
            self.meter.setRange(0, 100)
            self.meter.setValue(max(0, min(100, progress)))
            self.meter.setTextVisible(False)
            self.meter.setFixedHeight(3)
            body.addSpacing(4)
            body.addWidget(self.meter)

        if actions:
            body.addSpacing(9)
            buttons = QHBoxLayout()
            buttons.setSpacing(7)
            for label, handler in actions:
                button = QPushButton(label)
                button.setObjectName("ghost")
                # Four actions share a fixed-width card; at the ghost
                # button's full padding they overflow it and Qt clips the
                # labels to gibberish. Slim padding keeps every word whole.
                button.setProperty("slim", True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setFont(theme.body(9))
                button.clicked.connect(lambda _=False, run=handler: run())
                buttons.addWidget(button)
            buttons.addStretch(1)
            if on_remove is not None:
                self.remove_button = IconButton(
                    tip="Remove from the library. The photographs stay.")
                self.remove_button.clicked.connect(
                    lambda _=False: on_remove())
                buttons.addWidget(self.remove_button)
            body.addLayout(buttons)

        body.addStretch(1)
        layout.addLayout(body)
