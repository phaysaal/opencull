"""What the model proposed doing to each frame, in the words it used.

Until now the suggestion pass was a button on the assessment and its answer
was visible only as a list of treatment names in the develop page. The
reasoning -- what each treatment is trying to do, the instructions that
follow from it, and the guardrails it must not cross -- was written, stored
and never shown.

This page shows it. A photographer deciding between four treatments is
deciding between four arguments about the photograph, and the argument is
the part worth reading. The recipe is shown too, because that is the only
thing the renderer will actually execute: intent that did not compile into
a recipe is a treatment that cannot be rendered, and saying so is more
useful than quietly offering three where the model wrote four.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.development import BUILTIN_STYLES

from . import theme

PHOTO_ROW = 30

# Prose is read in a column, not across a whole window. The treatments stay
# at a readable measure however wide the window is opened.
MEASURE = 900

# The order treatments are read in: least interpreted first, so the personal
# one -- the only one that needed a profile -- is last and unmistakable.
STYLES = tuple(style for style in BUILTIN_STYLES if style != "calibrated")

SECTIONS = (
    ("intent", "WHAT IT IS TRYING TO DO"),
    ("instructions", "HOW"),
    ("recipe", "WHAT WILL BE EXECUTED"),
)


class Treatment(QFrame):
    """One proposed treatment of one photograph."""

    def __init__(self, style: str, entry: dict[str, Any]):
        super().__init__()
        self.setObjectName("decision")
        self.setMaximumWidth(MEASURE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel(str(entry.get(f"{style}_title") or style.title()))
        title.setObjectName("clusterTitle")
        title.setFont(theme.display(13))
        title.setWordWrap(True)
        head.addWidget(title, 1)

        kind = QLabel(style.upper())
        kind.setObjectName("axisName")
        kind.setFont(theme.display(7))
        head.addWidget(kind, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        rendered = bool(str(entry.get(f"{style}_recipe") or "").strip())
        for field, label in SECTIONS:
            text = str(entry.get(f"{style}_{field}") or "").strip()
            if not text:
                continue
            name = QLabel(label)
            name.setObjectName("axisName")
            name.setFont(theme.display(7))
            layout.addWidget(name)
            body = QLabel(text)
            body.setObjectName("axisBody")
            body.setWordWrap(True)
            body.setFont(theme.mono(8) if field == "recipe" else theme.body(9))
            layout.addWidget(body)

        if not rendered:
            warning = QLabel(
                "No recipe compiled for this treatment, so it cannot be "
                "rendered. Development will not offer it.")
            warning.setObjectName("status")
            warning.setProperty("tone", "alarm")
            warning.setWordWrap(True)
            warning.setFont(theme.body(9))
            layout.addWidget(warning)


class SuggestionsPage(QWidget):
    """The treatments asked for, and the ones still to ask for."""

    closed = Signal()
    suggested = Signal(str, list)   # output path, photographs to ask about

    def __init__(self, shortlist, reviews, directions,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.shortlist = shortlist
        self.reviews = reviews
        self.directions = directions
        self.current = ""
        self.photos: list[str] = []

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
        self.list.setFixedWidth(238)
        self.list.currentRowChanged.connect(self._chose_row)
        split.addWidget(self.list)

        right = QWidget()
        right.setObjectName("page")
        column = QVBoxLayout(right)
        column.setContentsMargins(22, 16, 22, 14)
        column.setSpacing(12)

        self.heading = QLabel("")
        self.heading.setObjectName("clusterTitle")
        self.heading.setFont(theme.display(17))
        column.addWidget(self.heading)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        # Without this the holder grows to its widest child and every
        # wrapped paragraph then lays out at that width, off screen.
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("page")
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setSpacing(12)
        self.scroll.setWidget(holder)
        column.addWidget(self.scroll, 1)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.ask_button = QPushButton("Ask for suggestions…")
        self.ask_button.setObjectName("primary")
        self.ask_button.setFont(theme.body(10))
        self.ask_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ask_button.setToolTip(
            "Every marked frame is a separate call to a model, so this costs.")
        self.ask_button.clicked.connect(self.suggest)
        actions.addWidget(self.ask_button)
        actions.addStretch(1)
        column.addLayout(actions)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        column.addWidget(self.status)

        split.addWidget(right, 1)
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

        self.title = QLabel("EDITING SUGGESTIONS")
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

    # --- state ------------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        try:
            return self.directions.payload()
        except Exception as exc:                     # noqa: BLE001 - reported
            self._report(f"The editing directions could not be read: {exc}",
                         "alarm")
            return {}

    def entry_for(self, photo: str) -> dict[str, Any]:
        directions = self.payload().get("directions") or {}
        for entry in directions.get("entries", []) or []:
            if isinstance(entry, dict) and entry.get("photo") == photo:
                return entry
        return {}

    def refresh(self) -> None:
        payload = self.payload()
        self.photos = list(payload.get("selected_photos") or [])
        answered = set(payload.get("processed_photos") or [])
        self.list.blockSignals(True)
        self.list.clear()
        for photo in self.photos:
            mark = "✓" if photo in answered else " "
            item = QListWidgetItem(f" {mark}  {photo}")
            item.setSizeHint(QSize(0, PHOTO_ROW))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.progress.setText(
            f"{len(answered)} of {len(self.photos)} marked frames answered"
            if self.photos else "nothing marked to develop")
        self.ask_button.setEnabled(bool(self.photos))
        if self.photos:
            target = self.current if self.current in self.photos else self.photos[0]
            self.list.setCurrentRow(self.photos.index(target))
            self.show_photo(target)
        else:
            self._show_nothing()

    def _chose_row(self, row: int) -> None:
        if 0 <= row < len(self.photos):
            self.show_photo(self.photos[row])

    def show_photo(self, photo: str) -> None:
        self.current = photo
        self.heading.setText(photo)
        entry = self.entry_for(photo)
        self._clear()
        if not entry:
            waiting = QLabel(
                "Nothing has been suggested for this frame yet. Ask for "
                "suggestions, and its treatments appear here.")
            waiting.setObjectName("hint")
            waiting.setWordWrap(True)
            waiting.setFont(theme.body(10))
            waiting.setMaximumWidth(MEASURE)
            self.body.addWidget(waiting)
            self.body.addStretch(1)
            return
        for style in STYLES:
            if not any(str(entry.get(f"{style}_{field}") or "").strip()
                       for field, _label in SECTIONS):
                continue
            self.body.addWidget(Treatment(style, entry))
        guardrails = str(entry.get("guardrails") or "").strip()
        if guardrails:
            frame = QFrame()
            frame.setObjectName("notice")
            frame.setMaximumWidth(MEASURE)
            inner = QVBoxLayout(frame)
            inner.setContentsMargins(14, 10, 14, 10)
            inner.setSpacing(4)
            name = QLabel("GUARDRAILS · TRUE OF EVERY TREATMENT")
            name.setObjectName("axisName")
            name.setFont(theme.display(7))
            inner.addWidget(name)
            body = QLabel(guardrails)
            body.setWordWrap(True)
            body.setFont(theme.body(9))
            inner.addWidget(body)
            self.body.addWidget(frame)
        self.body.addStretch(1)

    def _show_nothing(self) -> None:
        self.heading.setText("Nothing marked")
        self._clear()
        empty = QLabel(
            "Suggestions are asked for one frame at a time, for the frames "
            "you marked as worth developing. Mark some in the assessment "
            "first.")
        empty.setObjectName("hint")
        empty.setWordWrap(True)
        empty.setFont(theme.body(10))
        empty.setMaximumWidth(MEASURE)
        self.body.addWidget(empty)
        self.body.addStretch(1)

    def _clear(self) -> None:
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    # --- asking -----------------------------------------------------------

    def suggest(self) -> None:
        """Ask for editing directions, having first asked what to spend.

        Every frame is a separate call to a model, so redoing frames that
        already have directions costs again for an answer already given.
        The choice is put to the photographer rather than assumed.
        """
        payload = self.payload()
        marked = set(payload.get("selected_photos") or [])
        done = set(payload.get("processed_photos") or [])
        waiting = sorted(marked - done)
        if not marked:
            return
        if done and waiting:
            choice = self._ask_scope(len(waiting), len(done))
            if choice == "cancel":
                return
            photos = waiting if choice == "missing" else sorted(marked)
        elif done:
            if self._ask_scope(0, len(done)) == "cancel":
                return
            photos = sorted(marked)
        else:
            photos = sorted(marked)
        self._report(
            f"Asking for editing directions for {len(photos)} "
            f"frame{'' if len(photos) == 1 else 's'}.")
        self.suggested.emit(str(payload.get("default_path", "")), photos)

    def _ask_scope(self, waiting: int, done: int) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("Ask for editing directions?")
        box.setIcon(QMessageBox.Icon.Question)
        only = None
        if waiting:
            box.setText(
                f"{done} of these frames already have editing directions.")
            box.setInformativeText(
                f"Asking again for all of them costs another call per frame "
                f"for answers you already have.\n\n"
                f"{waiting} frame{'' if waiting == 1 else 's'} "
                f"{'has' if waiting == 1 else 'have'} no directions yet.")
            only = box.addButton(
                f"Only the {waiting} without", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(
                "Redo all of them", QMessageBox.ButtonRole.DestructiveRole)
        else:
            box.setText("Every marked frame already has editing directions.")
            box.setInformativeText(
                "Asking again replaces answers you already have, and costs "
                "another call for each frame.")
            box.addButton("Ask again", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(only or cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel:
            return "cancel"
        return "missing" if only is not None and clicked is only else "all"

    def step(self, delta: int) -> None:
        if not self.photos:
            return
        row = (self.list.currentRow() + delta) % len(self.photos)
        self.list.setCurrentRow(row)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_J):
            self.step(1)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_K):
            self.step(-1)
        elif key == Qt.Key.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(event)
