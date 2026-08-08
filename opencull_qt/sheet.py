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
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .previews import PreviewLoader, scaled

TILE = 168
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

    def __init__(self, name: str, selectable: bool = False):
        super().__init__()
        self.name = name
        self.selectable = selectable
        self.included = True
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

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.image.setPixmap(scaled(pixmap, TILE, TILE))

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
        self.glass.setGeometry(5, 5, TILE, TILE)
        self.glass.setVisible(not included)
        self.glass.raise_()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.selectable and event.button() == Qt.MouseButton.LeftButton:
            self.toggled.emit(self.name)
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

    def __init__(self, names: list[str], loader: PreviewLoader,
                 columns: int = COLUMNS, limit: int = LIMIT,
                 selectable: bool = False,
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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 8, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        for position, name in enumerate(self.names):
            tile = Tile(name, selectable=selectable)
            tile.toggled.connect(self.toggle)
            self.tiles[name] = tile
            grid.addWidget(tile, position // columns, position % columns)
            pixmap = self.loader.request(name, "thumb")
            if pixmap is not None:
                tile.set_pixmap(pixmap)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

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

    def set_all(self, included: bool) -> None:
        """Tick or untick every drawn tile at once."""
        if not self.selectable:
            return
        self._left = set() if included else set(self.tiles)
        for name, tile in self.tiles.items():
            tile.set_included(name not in self._left)
        self.changed.emit()
