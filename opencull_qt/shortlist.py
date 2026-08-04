"""The assessment page: read what the models saw, and say which frames matter.

This is the second of the two human gates. The cull decides which frames
survive; this decides which of the survivors are worth developing. Only the
frames marked here reach the suggestion pass, so the mark is the gate on
everything downstream -- and on what the downstream costs.

The assessment itself is evidence and is never edited. What a person writes
here goes to the shortlist review sidecar beside it, the same separation the
culling report and its review already keep.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.shortlist import ASSESSMENT_FIELDS
from opencull_gui.shortlist_reviews import ShortlistReviewError

from . import theme
from .previews import PreviewLoader, scaled

THUMB = 320
ROW = 32

# The models' own ranking, strongest first. A tier is a judgement, so it is
# shown as a word rather than only as a colour.
TIER_ORDER = ("exceptional", "strong", "promising", "ordinary", "reject")

# What each assessment axis is called when a person reads it.
AXIS_LABELS = {
    "composition": "Composition",
    "angle_and_perspective": "Angle and perspective",
    "subject_presentation": "Subject",
    "pose_and_expression": "Pose and expression",
    "moment_and_emotion": "Moment",
    "light_and_tonality": "Light and tonality",
    "surroundings": "Surroundings",
    "irrecoverable_defects": "Irrecoverable defects",
    "raw_editing_opportunities": "RAW editing opportunities",
    "distinctiveness": "Distinctiveness",
}


class Axis(QWidget):
    """One line of the assessment: what was looked at, and what was seen."""

    def __init__(self, label: str, text: str):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        heading = QLabel(label.upper())
        heading.setObjectName("axisName")
        heading.setFont(theme.display(7))
        layout.addWidget(heading)

        body = QLabel(text)
        body.setObjectName("axisBody")
        body.setWordWrap(True)
        body.setFont(theme.body(9))
        layout.addWidget(body)


class ShortlistPage(QWidget):
    """Assessed frames on the left, one frame's evidence on the right."""

    closed = Signal()
    suggested = Signal(str, list)   # output path, photographs to ask about

    def __init__(self, shortlist, reviews, loader: PreviewLoader,
                 directions=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.shortlist = shortlist
        self.reviews = reviews
        self.loader = loader
        self.directions = directions
        self.entries = list(shortlist.entries)
        self.current = str(self.entries[0]["photo"]) if self.entries else ""

        self.loader.ready.connect(self._painted)
        self._build()
        self._fill_entries()
        if self.current:
            self.show_entry(self.current)

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._bar())

        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)

        self.list = QListWidget()
        self.list.setObjectName("clusterList")
        self.list.setFixedWidth(330)
        self.list.currentRowChanged.connect(self._chose_row)
        split.addWidget(self.list)

        right = QWidget()
        right.setObjectName("page")
        column = QVBoxLayout(right)
        column.setContentsMargins(22, 18, 22, 16)
        column.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(14)
        self.frame = QLabel("…")
        self.frame.setObjectName("frameImage")
        self.frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame.setFixedSize(THUMB, int(THUMB * 0.72))
        header.addWidget(self.frame)

        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.heading = QLabel("")
        self.heading.setObjectName("clusterTitle")
        self.heading.setFont(theme.display(18))
        titles.addWidget(self.heading)

        self.verdict = QLabel("")
        self.verdict.setObjectName("rowState")
        self.verdict.setFont(theme.display(9))
        titles.addWidget(self.verdict)

        self.rationale = QLabel("")
        self.rationale.setObjectName("hint")
        self.rationale.setWordWrap(True)
        self.rationale.setFont(theme.body(10))
        titles.addWidget(self.rationale)
        titles.addStretch(1)
        header.addLayout(titles, 1)
        column.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        self.axes = QVBoxLayout(holder)
        self.axes.setContentsMargins(0, 4, 12, 4)
        self.axes.setSpacing(10)
        scroll.setWidget(holder)
        column.addWidget(scroll, 1)

        column.addWidget(self._decision())
        split.addWidget(right, 1)
        outer.addLayout(split, 1)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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

        self.title = QLabel("ASSESSMENT")
        self.title.setObjectName("chromeTitle")
        self.title.setFont(theme.display(11))
        layout.addWidget(self.title)
        layout.addStretch(1)

        self.progress = QLabel("")
        self.progress.setObjectName("hint")
        self.progress.setFont(theme.body(9))
        layout.addWidget(self.progress)
        return bar

    def _decision(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("decision")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.interesting = QCheckBox("Worth developing  (I)")
        self.interesting.setObjectName("gate")
        self.interesting.setFont(theme.body(10))
        self.interesting.setCursor(Qt.CursorShape.PointingHandCursor)
        self.interesting.setToolTip(
            "Only frames marked here are sent for editing suggestions.")
        self.interesting.clicked.connect(self.set_interesting)
        row.addWidget(self.interesting)

        self.edit_raw = QCheckBox("Develop from the RAW")
        self.edit_raw.setFont(theme.body(10))
        self.edit_raw.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_raw.clicked.connect(lambda: self.save())
        row.addWidget(self.edit_raw)
        row.addStretch(1)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setFont(theme.body(9))
        row.addWidget(self.status)
        layout.addLayout(row)

        self.note = QPlainTextEdit()
        self.note.setObjectName("note")
        self.note.setFont(theme.body(9))
        self.note.setFixedHeight(54)
        self.note.setPlaceholderText(
            "Your note on this frame. It goes to the suggestion pass with it.")
        self.note.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.note)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.selected = QLabel("")
        self.selected.setObjectName("hint")
        self.selected.setWordWrap(True)
        self.selected.setFont(theme.body(9))
        actions.addWidget(self.selected, 1)

        save_note = QPushButton("Save note")
        save_note.setObjectName("ghost")
        save_note.setFont(theme.body(9))
        save_note.clicked.connect(lambda: self.save())
        actions.addWidget(save_note)

        self.suggest_button = QPushButton("Suggest edits →")
        self.suggest_button.setObjectName("primary")
        self.suggest_button.setFont(theme.body(10))
        self.suggest_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.suggest_button.setToolTip(
            "Ask the models how to develop each marked frame. One call per "
            "frame, so this costs.")
        self.suggest_button.clicked.connect(self.suggest)
        actions.addWidget(self.suggest_button)
        layout.addLayout(actions)
        return panel

    # --- entries --------------------------------------------------------

    def _state(self) -> dict:
        return self.reviews.public_state()

    def _fill_entries(self) -> None:
        state = self._state()
        marks = state.get("entries", {})
        self.title.setText(self.shortlist.path.stem.split(".")[0].upper())
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self.entries:
            photo = str(entry["photo"])
            mark = "✓" if marks.get(photo, {}).get("interesting") else " "
            # One line: the default item delegate does not wrap, it replaces a
            # newline with an ellipsis, so a second line silently truncates
            # the first.
            item = QListWidgetItem(
                f" {mark}  {entry['rank']:>2}.  {photo}"
                f"   ·   {entry['tier']}  {float(entry['score']):.0f}")
            item.setData(Qt.ItemDataRole.UserRole, photo)
            item.setSizeHint(QSize(0, ROW))
            self.list.addItem(item)
        self.list.blockSignals(False)
        summary = state.get("summary", {})
        chosen = int(summary.get("interesting", 0))
        self.progress.setText(
            f"{chosen} of {len(self.entries)} worth developing")
        done = len(self._already_suggested())
        if not chosen:
            self.selected.setText(
                "Nothing is marked yet. The suggestion pass reads exactly the "
                "frames marked here, so it would have nothing to read.")
        elif done >= chosen:
            self.selected.setText(
                f"{chosen} marked, and all of them already have editing "
                "directions.")
        else:
            waiting = chosen - done
            self.selected.setText(
                f"{chosen} frame{'' if chosen == 1 else 's'} marked, "
                f"{waiting} still without editing directions."
                if done else
                f"{chosen} frame{'' if chosen == 1 else 's'} marked. The "
                "suggestion pass reads exactly these.")
        self.suggest_button.setEnabled(
            bool(chosen) and self.directions is not None)

    def _chose_row(self, row: int) -> None:
        if 0 <= row < len(self.entries):
            self.show_entry(str(self.entries[row]["photo"]))

    def entry_for(self, photo: str) -> dict:
        return self.shortlist.entry_by_photo.get(photo, {})

    def show_entry(self, photo: str) -> None:
        self.current = photo
        entry = self.entry_for(photo)
        self.loader.abandon()
        self.frame.setText("…")
        pixmap = self.loader.request(photo, "detail")
        if pixmap is not None:
            self._set_frame(pixmap)

        self.heading.setText(photo)
        tier = str(entry.get("tier", ""))
        score = float(entry.get("score", 0))
        confidence = float(entry.get("confidence", 0))
        self.verdict.setText(
            f"{tier.upper()}   ·   {score:.0f}/100   ·   "
            f"{confidence:.0%} confident")
        self.verdict.setProperty("tone", _tone(tier))
        self.verdict.style().unpolish(self.verdict)
        self.verdict.style().polish(self.verdict)
        self.rationale.setText(str(entry.get("rationale", "")))

        while self.axes.count():
            item = self.axes.takeAt(0)
            # Held once: reparenting can release the layout item's own
            # reference, so asking it a second time can answer None.
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        assessment = entry.get("assessment", {}) or {}
        for field in ASSESSMENT_FIELDS:
            text = str(assessment.get(field, "")).strip()
            if text:
                self.axes.addWidget(Axis(AXIS_LABELS.get(field, field), text))
        self.axes.addStretch(1)

        mark = self._state().get("entries", {}).get(photo, {})
        self.interesting.setChecked(bool(mark.get("interesting")))
        self.edit_raw.setChecked(bool(mark.get("edit_raw")))
        self.note.setPlainText(str(mark.get("note", "")))

        row = [item["photo"] for item in self.entries].index(photo)
        if self.list.currentRow() != row:
            self.list.blockSignals(True)
            self.list.setCurrentRow(row)
            self.list.blockSignals(False)
        self._report("")

    def _set_frame(self, pixmap) -> None:
        self.frame.setPixmap(
            scaled(pixmap, self.frame.width(), self.frame.height()))
        self.frame.setText("")

    def _painted(self, photo: str, size: str, pixmap) -> None:
        if photo == self.current and size == "detail":
            self._set_frame(pixmap)

    # --- decisions ------------------------------------------------------

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def set_interesting(self, interesting: bool) -> None:
        self.save(interesting=interesting)

    def toggle_interesting(self) -> None:
        self.set_interesting(not self.interesting.isChecked())

    def save(self, interesting: bool | None = None) -> None:
        """Record this frame's decision, keeping the models' tier as given.

        The tier is the assessment's own word and is not something this page
        argues with; what a person decides here is whether the frame is worth
        developing, and why.
        """
        if not self.current:
            return
        state = self._state()
        entry = self.entry_for(self.current)
        chosen = (self.interesting.isChecked() if interesting is None
                  else interesting)
        try:
            self.reviews.update(
                self.current, str(entry.get("tier", "ordinary")),
                self.edit_raw.isChecked(), self.note.toPlainText(),
                True, state.get("revision"), chosen)
        except ShortlistReviewError as exc:
            self._report(str(exc), "alarm")
            self.interesting.setChecked(bool(
                state.get("entries", {}).get(self.current, {}).get(
                    "interesting")))
            return
        self.interesting.setChecked(chosen)
        self._fill_entries()
        self._report("Saved." if chosen else "Not marked.", "ok")

    # --- asking for suggestions -----------------------------------------

    def _already_suggested(self) -> list[str]:
        """Marked frames that already have directions worth keeping."""
        if self.directions is None:
            return []
        try:
            return list(self.directions.payload().get(
                "processed_photos") or [])
        except Exception:
            return []

    def suggest(self) -> None:
        """Ask for editing directions, having first asked what to spend.

        Every frame is a separate call to a model, so redoing frames that
        already have directions costs again for an answer already given.
        The choice is put to the photographer rather than assumed.
        """
        if self.directions is None:
            return
        payload = self.directions.payload()
        marked = {
            photo for photo, entry in self._state().get("entries", {}).items()
            if isinstance(entry, dict) and entry.get("interesting") is True
        }
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
        if not self.entries:
            return
        names = [str(item["photo"]) for item in self.entries]
        row = names.index(self.current) + delta
        if 0 <= row < len(names):
            self.show_entry(names[row])

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if self.note.hasFocus():
            super().keyPressEvent(event)
        elif key == Qt.Key.Key_I:
            self.toggle_interesting()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.step(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.step(-1)
        elif key == Qt.Key.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(event)


def _tone(tier: str) -> str:
    """Colour the verdict by what it says, not by where it ranks."""
    if tier in {"exceptional", "strong"}:
        return "ready"
    if tier == "reject":
        return "failed"
    return ""
