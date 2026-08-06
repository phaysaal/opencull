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
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
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


class Tile(QFrame):
    """One frame of the selection, named."""

    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self.setObjectName("frame")
        self.setFixedSize(TILE + 10, TILE + CAPTION + 18)
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


class ContactSheet(QWidget):
    """Every frame of a selection, as thumbnails."""

    chosen = Signal(str)

    def __init__(self, names: list[str], loader: PreviewLoader,
                 columns: int = COLUMNS, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.names = list(names)
        self.loader = loader
        self.tiles: dict[str, Tile] = {}
        self.loader.ready.connect(self._painted)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

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
            tile = Tile(name)
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
