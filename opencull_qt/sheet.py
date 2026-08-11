"""The frames a run is about to read, before it reads them.

A phase that has not happened yet used to be a paragraph and a button. That
is enough to know what the run does and not enough to decide whether to pay
for it: the thing a photographer wants to see before spending a model call
on twenty-three photographs is the twenty-three photographs.

So the invitation shows the selection as a contact sheet. Which frames these
are is not decided here -- it is the same list the run itself will read,
computed by the kernel that will read it, so the sheet cannot promise one
set and the run take another.

Thumbnails load off the interface thread and arrive as they are decoded, so
a folder of two thousand does not stall the window before it draws.

A selectable sheet is the prefilter: click a frame to leave it out of the
run, and the button that pays re-counts. Leaving out is a decision, so a
left-out frame is marked -- greyed with a grease-pencil X -- rather than
hidden, and everything is still ticked until somebody says otherwise.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .previews import PreviewLoader, scaled

TILE = 168
# Contact sheets want density first: extra width becomes extra columns at
# the base size, and tiles grow only with the slack that remains. The
# ceiling stays well under the 520px preview source.
TILE_MAX = 260
CAPTION = 16
# Enough to fill any window this application opens in, without laying out a
# whole shoot on a narrow one.
COLUMNS = 8

# A shoot can be thousands of frames, and building a tile for each would
# stall the window before it drew. What is left out is said out loud: a
# sheet that quietly stopped at a round number would read as the whole
# selection, which is exactly the thing this page exists to show.
LIMIT = 120


class _Glass(QWidget):
    """The left-out marking, as a child above the photograph.

    A parent's paintEvent runs before its children paint, so anything drawn
    there lands under the image. The glass is therefore its own widget,
    stacked on top and transparent to the mouse so the tile still takes the
    click that brings the frame back.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        glass = QColor(theme.INK)
        glass.setAlphaF(0.62)
        painter.fillRect(self.rect(), glass)
        pen = QPen(QColor(theme.MUTED))
        pen.setWidth(2)
        painter.setPen(pen)
        inset = self.rect().adjusted(16, 16, -16, -16)
        painter.drawLine(inset.topLeft(), inset.bottomRight())
        painter.drawLine(inset.topRight(), inset.bottomLeft())
        painter.end()


class Tile(QFrame):
    """One frame of the selection, named -- and, on a selectable sheet,
    leavable-out."""

    toggled = Signal(str)
    inspect_wanted = Signal(str)
    opened = Signal(str)

    def __init__(self, name: str, selectable: bool = False):
        super().__init__()
        self.name = name
        self.selectable = selectable
        self.opens = False
        self.included = True
        self._pixmap = None
        self._tile = TILE
        self.setObjectName("frame")
        self.setFixedSize(TILE + 10, TILE + CAPTION + 18)
        if selectable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setToolTip("Click to leave this frame out of the run.")
        self.glass = _Glass(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 4)
        layout.setSpacing(3)

        self.image = QLabel("")
        self.image.setObjectName("frameImage")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setFixedSize(TILE, TILE)
        layout.addWidget(self.image)

        caption = QLabel(name)
        caption.setObjectName("frameName")
        caption.setFont(theme.mono(7))
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setFixedHeight(CAPTION)
        layout.addWidget(caption)

        # An optional verdict worn on the photograph itself.
        self.badge = QLabel("", self)
        self.badge.setObjectName("tileBadge")
        self.badge.setFont(theme.body(8))
        self.badge.hide()

        # And, opposite it, a way into the reasoning behind that verdict.
        self.eye = QPushButton("\U0001F441", self)
        self.eye.setObjectName("tileEye")
        self.eye.setFont(theme.body(8))
        self.eye.setCursor(Qt.CursorShape.PointingHandCursor)
        self.eye.setToolTip(
            "Why this frame was rated as it was: the model's full "
            "response, every axis it judged.")
        self.eye.setFixedSize(24, 20)
        self.eye.clicked.connect(
            lambda _checked=False: self.inspect_wanted.emit(self.name))
        self.eye.hide()

    def set_inspectable(self, inspectable: bool) -> None:
        """Offer a way into this frame's reasoning, or do not."""
        self.eye.setVisible(bool(inspectable))
        if inspectable:
            self._place_eye()
            self.eye.raise_()

    def _place_eye(self) -> None:
        self.eye.move(9, 9)

    def set_badge(self, text: str, tip: str = "") -> None:
        """A small verdict over the image's corner; empty clears it."""
        self.badge.setText(text)
        self.badge.setToolTip(tip)
        self.badge.setVisible(bool(text))
        if text:
            self._place_badge()
            self.badge.raise_()

    def _place_badge(self) -> None:
        self.badge.adjustSize()
        self.badge.move(5 + self._tile - self.badge.width() - 4, 9)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self._render()

    def set_scale(self, tile: int) -> None:
        """Give the tile the size its row was dealt."""
        if tile == self._tile:
            return
        self._tile = tile
        self.setFixedSize(tile + 10, tile + CAPTION + 18)
        self.image.setFixedSize(tile, tile)
        if self.glass.isVisible():
            self.glass.setGeometry(5, 5, tile, tile)
        if self.badge.isVisible():
            self._place_badge()
        if self.eye.isVisible():
            self._place_eye()
        self._render()

    def rerender(self) -> None:
        """Redraw for the screen the window is on now."""
        self._render(force=True)

    def _render(self, force: bool = False) -> None:
        if self._pixmap is None:
            return
        state = (self._tile, self.devicePixelRatioF())
        if not force and state == getattr(self, "_rendered", None):
            return
        self._rendered = state
        self.image.setPixmap(scaled(
            self._pixmap, self._tile, self._tile,
            self.devicePixelRatioF()))

    def set_included(self, included: bool) -> None:
        self.included = included
        self.setProperty("left", not included)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setToolTip(
            "Left out. Click to bring it back."
            if not included else "Click to leave this frame out of the run.")
        # Over the photograph, not the caption: the name stays readable so
        # the left-out frame is still accountable.
        self.glass.setGeometry(5, 5, self._tile, self._tile)
        self.glass.setVisible(not included)
        self.glass.raise_()

    def set_marked(self, marked: bool) -> None:
        """Wear the safelight border of a frame chosen for what comes next."""
        self.setProperty("kept", "true" if marked else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            if self.selectable:
                self.toggled.emit(self.name)
            elif self.opens:
                self.opened.emit(self.name)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.selectable and event.key() in (
                Qt.Key.Key_Space, Qt.Key.Key_Return):
            self.toggled.emit(self.name)
            return
        super().keyPressEvent(event)




class ContactSheet(QWidget):
    """Every frame of a selection, as thumbnails.

    With ``selectable`` the sheet is also the prefilter: frames can be left
    out before the paid run, and ``chosen()`` is what the run will read.
    Frames past the draw cap have no tile to click, so they are always
    included -- a frame can only be left out where the leaving-out can be
    seen.
    """

    changed = Signal()
    inspect_wanted = Signal(str)
    opened = Signal(str)

    def __init__(self, names: list[str], loader: PreviewLoader,
                 columns: int = COLUMNS, limit: int = LIMIT,
                 selectable: bool = False, opens: bool = False,
                 hint: str = "Click a frame to leave it out.",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.selection = list(names)
        self.names = self.selection[:limit]
        self.selectable = selectable
        self.loader = loader
        self.tiles: dict[str, Tile] = {}
        self._left: set[str] = set()
        self.loader.ready.connect(self._painted)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        if selectable:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 2)
            row.setSpacing(8)
            # What a click means depends on what the sheet is for: leaving
            # a frame out of a run, or picking the few worth paying for.
            note = QLabel(hint)
            note.setObjectName("hint")
            note.setFont(theme.body(9))
            row.addWidget(note)
            row.addStretch(1)
            for label, included in (("Tick all", True), ("Untick all", False)):
                button = QPushButton(label)
                button.setObjectName("ghost")
                button.setFont(theme.body(9))
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(
                    lambda _=False, value=included: self.set_all(value))
                row.addWidget(button)
            layout.addLayout(row)

        dropped = len(self.selection) - len(self.names)
        if dropped:
            notice = QLabel(
                f"Showing the first {len(self.names)} of "
                f"{len(self.selection):,}. "
                + (f"All {len(self.selection):,} are included; only the "
                   "frames shown can be left out."
                   if selectable else "All of them are read."))
            notice.setObjectName("hint")
            notice.setFont(theme.body(9))
            layout.addWidget(notice)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        self.grid = QGridLayout(holder)
        self.grid.setContentsMargins(0, 0, 8, 0)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(10)
        self._layout_state = (0, 0)
        self._screen_hooked = False
        for position, name in enumerate(self.names):
            tile = Tile(name, selectable=selectable)
            tile.opens = opens
            if opens:
                tile.setCursor(Qt.CursorShape.PointingHandCursor)
                tile.setToolTip("Open this frame.")
            tile.toggled.connect(self.toggle)
            tile.inspect_wanted.connect(self.inspect_wanted)
            tile.opened.connect(self.opened)
            self.tiles[name] = tile
            self.grid.addWidget(
                tile, position // columns, position % columns)
            pixmap = self.loader.request(name, "thumb")
            if pixmap is not None:
                tile.set_pixmap(pixmap)
        self.scroll.setWidget(holder)
        layout.addWidget(self.scroll, 1)

    @staticmethod
    def sheet_geometry(
        available: int, count: int, spacing: int,
    ) -> tuple[int, int]:
        """How many columns, and how large a tile, for one width."""
        columns = max(1, (available + spacing) // (TILE + 10 + spacing))
        columns = int(min(columns, max(1, count)))
        total = (available - spacing * (columns - 1)) / columns
        tile = int(max(TILE, min(TILE_MAX, total - 10)))
        return columns, tile

    def _relayout(self) -> None:
        """Deal the tiles the width the window actually has."""
        if not self.names:
            return
        margins = self.grid.contentsMargins()
        # The grid's own margins are not width the tiles can use. Dealing
        # them out anyway overflows the viewport by exactly that much, and
        # a sheet that fits is shown with a scrollbar it does not need.
        available = (self.scroll.viewport().width()
                     - margins.left() - margins.right())
        if available <= 0:
            return
        columns, tile = self.sheet_geometry(
            available, len(self.names), self.grid.horizontalSpacing())
        if (columns, tile) == self._layout_state:
            return
        self._layout_state = (columns, tile)
        while self.grid.count():
            self.grid.takeAt(0)
        for column_index in range(self.grid.columnCount() + 1):
            self.grid.setColumnStretch(column_index, 0)
        for row_index in range(self.grid.rowCount() + 1):
            self.grid.setRowStretch(row_index, 0)
        rows = (len(self.names) + columns - 1) // columns
        for position, name in enumerate(self.names):
            widget = self.tiles[name]
            widget.set_scale(tile)
            self.grid.addWidget(
                widget, position // columns, position % columns)
        self.grid.setColumnStretch(columns, 1)
        self.grid.setRowStretch(rows, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._relayout()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        handle = self.window().windowHandle() if self.window() else None
        if handle is not None and not self._screen_hooked:
            self._screen_hooked = True
            handle.screenChanged.connect(self._screen_changed)
        self._relayout()

    def _screen_changed(self, _screen) -> None:
        for tile in self.tiles.values():
            tile.rerender()
        self._layout_state = (0, 0)
        self._relayout()

    def _painted(self, name: str, size: str, pixmap) -> None:
        tile = self.tiles.get(name)
        if tile is not None and size == "thumb":
            tile.set_pixmap(pixmap)

    # --- the prefilter ---------------------------------------------------

    def chosen(self) -> list[str]:
        """What the run will read: everything not deliberately left out."""
        return [name for name in self.selection if name not in self._left]

    def toggle(self, name: str) -> None:
        if not self.selectable or name not in self.tiles:
            return
        if name in self._left:
            self._left.discard(name)
        else:
            self._left.add(name)
        self.tiles[name].set_included(name not in self._left)
        self.changed.emit()

    def include_only(self, names) -> None:
        """Start from a chosen few rather than from everything.

        The sheet's own default is everything-in-unless-clicked, which is
        right for a run over a folder. A sheet that proposes a subset --
        the frames an assessment ranked highest, say -- needs to say so on
        the tiles rather than in a sentence above them.
        """
        if not self.selectable:
            return
        keep = set(names)
        self._left = {name for name in self.selection if name not in keep}
        for name, tile in self.tiles.items():
            tile.set_included(name not in self._left)
        self.changed.emit()

    def set_all(self, included: bool) -> None:
        """Tick or untick every drawn tile at once."""
        if not self.selectable:
            return
        self._left = set() if included else set(self.tiles)
        for name, tile in self.tiles.items():
            tile.set_included(name not in self._left)
        self.changed.emit()
