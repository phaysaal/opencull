"""The story of one frame, told in the window.

The drawer renders what `opencull_gui.provenance.frame_story` assembled:
each recorded decision as a chapter, oldest first, in the words already on
record. It computes nothing and costs nothing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .widgets import Paragraph


class ProvenanceDialog(QDialog):
    """Why this frame is where it is, chapter by chapter."""

    def __init__(self, photo: str, story: list[dict],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Why {photo}?")
        self.setMinimumSize(520, 460)
        self.setStyleSheet(theme.STYLESHEET)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)

        heading = QLabel(photo)
        heading.setObjectName("clusterTitle")
        heading.setFont(theme.display(16))
        layout.addWidget(heading)

        lead = Paragraph(
            "Every line below is a decision already on record — nothing "
            "was computed to answer this, and no model was asked.")
        lead.setObjectName("hint")
        lead.setFont(theme.body(9))
        layout.addWidget(lead)

        scroll = QScrollArea()
        scroll.setObjectName("controlScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("controls")
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 2, 8, 2)
        column.setSpacing(10)
        for chapter in story:
            column.addWidget(self._chapter(chapter))
        column.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("ghost")
        close.setFont(theme.body(10))
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)

    @staticmethod
    def _chapter(chapter: dict) -> QWidget:
        frame = QFrame()
        frame.setObjectName("decision")
        column = QVBoxLayout(frame)
        column.setContentsMargins(13, 10, 13, 11)
        column.setSpacing(4)

        label = QLabel(str(chapter.get("label", "")))
        label.setObjectName("status" if chapter.get("tone") else "axisName")
        if chapter.get("tone"):
            label.setProperty("tone", chapter["tone"])
        label.setFont(theme.display(7))
        column.addWidget(label)

        for line in chapter.get("lines", []):
            body = Paragraph(str(line))
            body.setObjectName("axisBody")
            body.setFont(theme.body(9))
            column.addWidget(body)
        return frame
