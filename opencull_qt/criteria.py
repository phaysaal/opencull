"""Choosing the bar a shoot is judged against, before paying to judge it.

The stance is asked for at the moment it matters -- the press that spends
the money -- rather than buried in a settings pane nobody opens. It is one
question with four answers and a sentence each, because the difference
between them is a sentence, not a paragraph.

The dialog doubles as the cost confirmation it replaced: the count is on
the button, so choosing the bar and agreeing to the spend are one act
instead of two prompts in a row.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from opencull_gui import criteria

from . import theme


class Strictness(QFrame):
    """One bar height, and what it means for a frame to clear it."""

    def __init__(self, item: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("decision")
        self.item = item
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 9, 14, 10)
        layout.setSpacing(3)

        self.choice = QRadioButton(f"{item['name']} — {item['bar']}")
        self.choice.setFont(theme.body(11))
        self.choice.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.choice)

        detail = QLabel(item["detail"])
        detail.setObjectName("hint")
        detail.setWordWrap(True)
        detail.setFont(theme.body(9))
        detail.setContentsMargins(23, 0, 0, 0)
        layout.addWidget(detail)


class Lens(QFrame):
    """One kind of merit that counts. Several can count at once."""

    def __init__(self, item: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("decision")
        self.item = item
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 9, 14, 10)
        layout.setSpacing(3)

        self.choice = QCheckBox(item["name"])
        self.choice.setFont(theme.body(11))
        self.choice.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.choice)

        detail = QLabel(item["detail"])
        detail.setObjectName("hint")
        detail.setWordWrap(True)
        detail.setFont(theme.body(9))
        detail.setContentsMargins(23, 0, 0, 0)
        layout.addWidget(detail)


class CriteriaDialog(QDialog):
    """Ask what the shoot is being judged by, and what that will cost."""

    def __init__(self, name: str, frames: int, culled: bool,
                 stance="", parent: QWidget | None = None):
        super().__init__(parent)
        self.frames = frames
        self.setWindowTitle("Judge this shoot by what?")
        self.setMinimumWidth(560)
        self.setStyleSheet(theme.STYLESHEET)
        self._accepted = False
        self._build(name, culled, criteria.normalise_choice(stance))

    def _build(self, name: str, culled: bool, chosen: dict) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)

        heading = QLabel("WHAT IS THE BAR?")
        heading.setObjectName("bandTitle")
        heading.setFont(theme.display(8))
        layout.addWidget(heading)

        lead = QLabel(
            "The same ten axes are judged whatever you choose. What changes "
            "is what counts as strong — the same frame is a keeper to a "
            "family editor and an ordinary one to a gallery. One strictness, "
            "any lenses; it all composes into a single run at the same cost.")
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        lead.setFont(theme.body(10))
        layout.addWidget(lead)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.strictness_options: list[Strictness] = []
        for index, item in enumerate(criteria.STRICTNESS):
            widget = Strictness(item)
            widget.choice.setChecked(item["id"] == chosen["strictness"])
            self.group.addButton(widget.choice, index)
            self.strictness_options.append(widget)
            layout.addWidget(widget)

        lenses_title = QLabel("WHAT COUNTS AS MERIT? CHOOSE ANY.")
        lenses_title.setObjectName("bandTitle")
        lenses_title.setFont(theme.display(8))
        layout.addWidget(lenses_title)

        self.lens_options: list[Lens] = []
        for item in criteria.LENSES:
            widget = Lens(item)
            widget.choice.setChecked(item["id"] in chosen["lenses"])
            widget.choice.toggled.connect(self._lenses_changed)
            self.lens_options.append(widget)
            layout.addWidget(widget)

        self.words = QLineEdit(chosen.get("words", ""))
        self.words.setPlaceholderText(
            "In your own words, optional — e.g. quiet light, honest "
            "moments, nothing posed")
        self.words.setFont(theme.body(10))
        self.words.setMaxLength(criteria.WORDS_LIMIT)
        layout.addWidget(self.words)

        note = QLabel(
            f"{self.frames} frames are read, one model call each."
            if culled else
            f"{name} has not been culled, so the selection is every frame in "
            f"it — about {self.frames} model calls rather than one per keeper.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        note.setFont(theme.body(9))
        layout.addWidget(note)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ghost")
        cancel.setFont(theme.body(10))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        actions.addStretch(1)

        self.run = QPushButton(f"Assess {self.frames} frames")
        self.run.setObjectName("primary")
        self.run.setFont(theme.body(10))
        self.run.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run.setDefault(True)
        self.run.clicked.connect(self.accept)
        actions.addWidget(self.run)
        layout.addLayout(actions)
        self._lenses_changed()

    def _lenses_changed(self, _checked: bool = False) -> None:
        any_lens = any(w.choice.isChecked() for w in self.lens_options)
        self.run.setEnabled(any_lens)
        self.run.setToolTip(
            "" if any_lens
            else "Choose at least one kind of merit to judge by.")

    def choice(self) -> dict:
        """The composed parts, for remembering and for the record."""
        index = self.group.checkedId()
        strictness = (
            criteria.STRICTNESS[index]["id"]
            if 0 <= index < len(criteria.STRICTNESS)
            else criteria.DEFAULT_CHOICE["strictness"])
        return criteria.normalise_choice({
            "strictness": strictness,
            "lenses": [w.item["id"] for w in self.lens_options
                       if w.choice.isChecked()],
            "words": self.words.text(),
        })

    def stance(self) -> str:
        """One description for one run: the composed bar."""
        return criteria.compose(self.choice())
