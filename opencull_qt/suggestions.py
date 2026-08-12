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

The page also chooses. It used to open on "mark some in the assessment
first", which is a page telling somebody to go and use a different page --
so the frames are picked here instead, ranked as the assessment ranked
them, and the tick written is the same mark the assessment shows.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.development import BUILTIN_STYLES
from opencull_gui.directions import verdict_of
from opencull_gui.scenes import capture_time, photos_root_of
from opencull_gui.shortlist import settled_order, tier_rank

from . import theme
from .previews import scaled
from .sheet import ContactSheet
from .widgets import Stamp

PHOTO_ROW = 30

# Ticked by default when nobody has chosen yet: the tiers the assessment
# placed at strong or above. Everything else stays unticked, because a
# default that spends a call on every promising frame is a default that
# spends money the photographer did not agree to.
DEFAULT_TIER = tier_rank("strong")

# The chooser draws thumbnails, and past a certain number they stop being
# a sheet somebody reads and become a wall. The best-ranked are offered.
CHOOSABLE = 120

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

        chosen = entry.get("personal_style") if style == "personal" else None
        chosen = chosen if isinstance(chosen, dict) else None
        kind = QLabel(
            f"{style.upper()} · {chosen['profile_name'].upper()}"
            if chosen and chosen.get("profile_name") else style.upper())
        kind.setObjectName("axisName")
        kind.setFont(theme.display(7))
        kind.setWordWrap(True)
        head.addWidget(kind, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        # Which of the photographer's styles this scene asked for, and what
        # about the scene asked for it. A personal treatment that cannot
        # name the taste it came from is not personal to anybody.
        if chosen and str(chosen.get("reason") or "").strip():
            because = QLabel(
                f"Chosen from {chosen.get('offered', 0)} of your styles — "
                + str(chosen["reason"]).strip())
            because.setObjectName("hint")
            because.setWordWrap(True)
            because.setFont(theme.body(9))
            layout.addWidget(because)

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
    why_wanted = Signal(str)        # the frame whose story is asked for

    def __init__(self, shortlist, reviews, directions, loader=None,
                 styles=(), parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.shortlist = shortlist
        self.reviews = reviews
        self.directions = directions
        self.loader = loader
        # The personal styles a run may speak in, by name, for saying so
        # before the run is paid for. The choice among them is the model's,
        # made per scene.
        self.styles = list(styles)
        self.current = ""
        self.photos: list[str] = []
        self.sheet: ContactSheet | None = None

        self._build()
        self.refresh()

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.bar = self._bar()
        outer.addWidget(self.bar)

        # Two faces of one page: the treatments, and the choice of which
        # frames to ask about. The choice is not a dialog, because it is
        # revisited -- a shoot is developed in passes, not in one sitting.
        self.faces = QStackedWidget()
        outer.addWidget(self.faces, 1)

        treatments = QWidget()
        treatments.setObjectName("page")
        split = QHBoxLayout(treatments)
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

        head = QHBoxLayout()
        head.setSpacing(10)
        self.heading = QLabel("")
        self.heading.setObjectName("clusterTitle")
        self.heading.setFont(theme.display(17))
        head.addWidget(self.heading)
        self.stamp = Stamp()
        head.addWidget(self.stamp)
        why = QPushButton("Why?")
        why.setObjectName("ghost")
        why.setFont(theme.body(9))
        why.setCursor(Qt.CursorShape.PointingHandCursor)
        why.setToolTip("The recorded story of this frame.")
        why.clicked.connect(lambda: self.why_wanted.emit(self.current))
        head.addWidget(why)
        head.addStretch(1)
        column.addLayout(head)

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
        self.choose_button = QPushButton("Choose frames…")
        self.choose_button.setObjectName("ghost")
        self.choose_button.setFont(theme.body(10))
        self.choose_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.choose_button.setToolTip(
            "Change which assessed frames are marked to develop.")
        self.choose_button.clicked.connect(lambda: self.show_chooser())
        actions.addWidget(self.choose_button)
        actions.addStretch(1)
        column.addLayout(actions)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        column.addWidget(self.status)

        split.addWidget(right, 1)
        self.faces.addWidget(treatments)
        self.chooser = self._chooser()
        self.faces.addWidget(self.chooser)

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

        self.title = QLabel("AI EDITING")
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

    def _chooser(self) -> QWidget:
        """The face that picks which frames are worth a call."""
        face = QWidget()
        face.setObjectName("page")
        column = QVBoxLayout(face)
        column.setContentsMargins(22, 16, 22, 14)
        column.setSpacing(10)

        self.chooser_heading = QLabel("Choose the frames to develop")
        self.chooser_heading.setObjectName("clusterTitle")
        self.chooser_heading.setFont(theme.display(17))
        column.addWidget(self.chooser_heading)

        self.chooser_lead = QLabel("")
        self.chooser_lead.setObjectName("hint")
        self.chooser_lead.setWordWrap(True)
        self.chooser_lead.setFont(theme.body(10))
        column.addWidget(self.chooser_lead)

        self.chooser_holder = QVBoxLayout()
        self.chooser_holder.setContentsMargins(0, 0, 0, 0)
        column.addLayout(self.chooser_holder, 1)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.mark_button = QPushButton("Mark these frames")
        self.mark_button.setObjectName("primary")
        self.mark_button.setFont(theme.body(10))
        self.mark_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mark_button.clicked.connect(lambda: self.apply_marks())
        row.addWidget(self.mark_button)
        self.chooser_back = QPushButton("Back to the treatments")
        self.chooser_back.setObjectName("ghost")
        self.chooser_back.setFont(theme.body(10))
        self.chooser_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chooser_back.clicked.connect(lambda: self.show_treatments())
        row.addWidget(self.chooser_back)
        row.addStretch(1)
        column.addLayout(row)

        self.chooser_status = QLabel("")
        self.chooser_status.setObjectName("hint")
        self.chooser_status.setWordWrap(True)
        self.chooser_status.setFont(theme.body(9))
        column.addWidget(self.chooser_status)
        return face

    # --- choosing what to ask about ---------------------------------------

    def candidates(self) -> list[str]:
        """The assessed frames, best-ranked first."""
        marks = self.reviews.public_state().get("entries", {})
        tiers = {
            photo: str(entry.get("tier") or "")
            for photo, entry in marks.items()
            if isinstance(entry, dict) and entry.get("tier")}
        return settled_order(self.shortlist.entries, tiers)

    def _tier_of(self, photo: str) -> str:
        """The photographer's tier for a frame, or the assessment's."""
        entry = self.reviews.public_state().get("entries", {}).get(photo)
        if isinstance(entry, dict) and entry.get("tier"):
            return str(entry["tier"])
        return str((self.shortlist.entry_by_photo.get(photo) or {}).get(
            "tier") or "")

    def show_chooser(self) -> None:
        """Show the frames of this assessment, ranked, ready to be picked."""
        while self.chooser_holder.count():
            item = self.chooser_holder.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.sheet = None

        ranked = self.candidates()
        offered = ranked[:CHOOSABLE]
        if not offered or self.loader is None:
            self.chooser_lead.setText(
                "There are no assessed frames to choose from yet."
                if not offered else
                "The frames cannot be shown here. Mark them in the "
                "assessment instead.")
            self.mark_button.setEnabled(False)
            self.chooser_back.setVisible(bool(self.photos))
            self.faces.setCurrentWidget(self.chooser)
            return

        marked = {
            photo for photo, entry in
            self.reviews.public_state().get("entries", {}).items()
            if isinstance(entry, dict) and entry.get("interesting") is True}
        # Nobody has chosen yet, so the assessment's own verdict proposes:
        # strong and above, and nothing further down. A default that ticked
        # every promising frame would spend a call on each of them.
        start = marked or {
            photo for photo in offered
            if tier_rank(self._tier_of(photo)) >= DEFAULT_TIER}

        lead = (
            f"{len(offered)} assessed frames, best first. Ticked frames are "
            "marked worth developing; click a frame to change it. Marking "
            "costs nothing — asking for suggestions is what spends a call "
            "per frame.")
        if len(ranked) > len(offered):
            lead += (f" The {len(ranked) - len(offered)} lowest-ranked are "
                     "not offered here; mark those in the assessment.")
        self.chooser_lead.setText(lead)

        self.sheet = ContactSheet(
            offered, self.loader, selectable=True, limit=CHOOSABLE,
            hint="Click a frame to tick or untick it.")
        self.sheet.include_only(start)
        self.sheet.changed.connect(self._recount)
        self.chooser_holder.addWidget(self.sheet, 1)
        self._recount()
        self.chooser_status.setText("")
        self.faces.setCurrentWidget(self.chooser)

    def show_treatments(self) -> None:
        self.faces.setCurrentIndex(0)

    def _recount(self) -> None:
        chosen = len(self.sheet.chosen()) if self.sheet is not None else 0
        self.mark_button.setText(
            "Mark no frames" if not chosen else
            f"Mark {chosen} frame{'' if chosen == 1 else 's'}")
        self.mark_button.setEnabled(True)
        self.chooser_back.setVisible(bool(self.photos))

    def apply_marks(self) -> None:
        """Write the ticks as marks, one frame at a time.

        A frame the store refuses is recorded and named rather than
        abandoning the rest: nineteen frames marked and one explained beats
        twenty frames unmarked and one exception.
        """
        if self.sheet is None:
            return
        wanted = set(self.sheet.chosen())
        offered = set(self.sheet.selection)
        state = self.reviews.public_state()
        entries = state.get("entries", {})
        already = {
            photo for photo, entry in entries.items()
            if isinstance(entry, dict) and entry.get("interesting") is True}
        changing = sorted(
            (wanted - already) | ((already & offered) - wanted))
        if not changing:
            self.chooser_status.setText(
                f"{len(wanted)} frames were already marked; nothing changed.")
            self.refresh()
            if self.photos:
                self.show_treatments()
            return

        refused: list[str] = []
        for photo in changing:
            entry = entries.get(photo) if isinstance(
                entries.get(photo), dict) else {}
            try:
                state = self.reviews.update(
                    photo,
                    self._tier_of(photo) or "ordinary",
                    bool(entry.get("edit_raw", False)),
                    str(entry.get("note", "")),
                    # A tick is a decision about a frame, not a claim to
                    # have read its assessment. Whether it was reviewed
                    # stays whatever the assessment page recorded.
                    bool(entry.get("reviewed", False)),
                    state.get("revision"),
                    photo in wanted)
                entries = state.get("entries", {})
            except Exception as exc:                 # noqa: BLE001 - reported
                refused.append(f"{photo} ({exc})")

        self.refresh()
        marked = len(self.photos)
        if refused:
            self.chooser_status.setText(
                f"{marked} frames are marked. These could not be: "
                + "; ".join(refused))
            return
        told = (f"{marked} frame{'' if marked == 1 else 's'} marked worth "
                "developing. Nothing has been spent yet.")
        if not marked:
            # Unmarking everything leaves nothing to read on the other
            # face, so the page stays where the work is.
            self.chooser_status.setText(
                "Nothing is marked now, so there is nothing to ask about.")
            return
        self.show_treatments()
        self._report(told)

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
            verdict = verdict_of(self.entry_for(photo))
            mark = ("!" if verdict == "unverified"
                    else "✕" if verdict == "unreadable"
                    else "✓" if photo in answered else " ")
            item = QListWidgetItem(f" {mark}  {photo}")
            item.setSizeHint(QSize(0, PHOTO_ROW))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.progress.setText(
            f"{len(answered)} of {len(self.photos)} marked frames answered"
            if self.photos else "nothing marked to develop")
        self.ask_button.setEnabled(bool(self.photos))
        self.choose_button.setVisible(bool(self.photos))
        if self.photos:
            target = self.current if self.current in self.photos else self.photos[0]
            self.list.setCurrentRow(self.photos.index(target))
            self.show_photo(target)
        else:
            self._show_nothing()
            # Nothing marked is the state this page can fix itself, so it
            # opens on the choice rather than on an instruction to go
            # somewhere else.
            self.show_chooser()

    def _chose_row(self, row: int) -> None:
        if 0 <= row < len(self.photos):
            self.show_photo(self.photos[row])

    def show_photo(self, photo: str) -> None:
        self.current = photo
        self.heading.setText(photo)
        entry = self.entry_for(photo)
        self.stamp.set_verdict(verdict_of(entry))
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
        shared_from = str(entry.get("derived_from") or "")
        if shared_from:
            # An inherited answer must say so where it is read, or a
            # photographer will credit this frame with a judgement that was
            # made about another one.
            origin = QLabel(
                f"Treatments shared from {shared_from} — same scene, "
                "developed as one edit.")
            origin.setObjectName("hint")
            origin.setWordWrap(True)
            origin.setFont(theme.body(9))
            self.body.addWidget(origin)
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
            "marked as worth developing. Choose them here or in the "
            "assessment; it is the same mark either way.")
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
        launch_suggestions(self, self.payload())

    def _ask_scope(self, waiting: int, done: int, plan=()) -> str:
        return ask_suggestion_scope(
            self, waiting, done, plan, loader=self.loader,
            photos_root=photos_root_of(self.shortlist),
            styles=self.styles)

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


class SceneStrip(QFrame):
    """One scene, as its photographs, with the one that speaks for it."""

    THUMB = 104
    GAP = 6

    def __init__(self, scene: dict, loader, when: str,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("decision")
        self.loader = loader
        self.photos = [str(photo) for photo in scene.get("photos", [])]
        self.representative = str(scene.get("representative") or "")
        self.frames: dict[str, QLabel] = {}
        self._columns = 0
        loader.ready.connect(self._painted)

        column = QVBoxLayout(self)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(8)

        title = QLabel(
            f"{len(self.photos)} frame{'' if len(self.photos) == 1 else 's'}"
            + (f"   ·   {when}" if when else "")
            + f"   ·   one call, answered by {self.representative}")
        title.setObjectName("axisName")
        title.setFont(theme.display(7))
        column.addWidget(title)

        # A long scene wraps into rows. Sideways scrolling would hide the
        # very frames the photographer is being asked to judge the grouping
        # by, which is the whole point of showing them.
        self.strip = QGridLayout()
        self.strip.setSpacing(self.GAP)
        for photo in self.photos:
            frame = QLabel("")
            frame.setObjectName("frameImage")
            frame.setFixedSize(self.THUMB, self.THUMB)
            frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
            speaks = photo == self.representative
            frame.setToolTip(
                f"{photo} — its treatment is written, and shared with the "
                f"rest of this scene." if speaks else
                f"{photo} — developed with the treatment written for "
                f"{self.representative}.")
            if speaks:
                frame.setStyleSheet(
                    f"border: 2px solid {theme.SAFELIGHT}; border-radius: 4px;")
            self.frames[photo] = frame
            pixmap = loader.request(photo, "thumb")
            if pixmap is not None:
                self._paint(photo, pixmap)
        column.addLayout(self.strip)
        self._deal(1)

    def _deal(self, columns: int) -> None:
        columns = max(1, min(columns, len(self.photos) or 1))
        if columns == self._columns:
            return
        self._columns = columns
        while self.strip.count():
            self.strip.takeAt(0)
        for column in range(self.strip.columnCount() + 1):
            self.strip.setColumnStretch(column, 0)
        for index, photo in enumerate(self.photos):
            self.strip.addWidget(
                self.frames[photo], index // columns, index % columns)
        self.strip.setColumnStretch(columns, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        margins = self.layout().contentsMargins()
        available = self.width() - margins.left() - margins.right()
        self._deal((available + self.GAP) // (self.THUMB + self.GAP))

    def _painted(self, name: str, size: str, pixmap) -> None:
        if size == "thumb" and name in self.frames:
            self._paint(name, pixmap)

    def _paint(self, name: str, pixmap) -> None:
        self.frames[name].setPixmap(scaled(
            pixmap, self.THUMB, self.THUMB, self.devicePixelRatioF()))


def scene_when(photos, photos_root) -> str:
    """The clock times a scene spans, as a photographer would say them."""
    if photos_root is None:
        return ""
    stamps = sorted(
        stamp for stamp in
        (capture_time(Path(photos_root) / photo) for photo in photos)
        if stamp)
    if not stamps:
        return ""
    first = datetime.fromtimestamp(stamps[0])
    last = datetime.fromtimestamp(stamps[-1])
    if first.strftime("%H:%M") == last.strftime("%H:%M"):
        return first.strftime("%d %b, %H:%M")
    return f"{first:%d %b, %H:%M}–{last:%H:%M}"


class ScenePlanDialog(QDialog):
    """What sharing a treatment across a scene would actually mean.

    The saving used to be offered as a number -- "1 call instead of 39" --
    and accepting it meant trusting a grouping nobody could see. A scene
    that is wrong is not a saving, it is one photograph's treatment applied
    to another photograph, so the grouping is shown before it is agreed to.
    """

    def __init__(self, parent, plan, waiting: int, done: int, loader,
                 photos_root, styles=()):
        super().__init__(parent)
        self.setWindowTitle("Ask for editing directions?")
        self.setStyleSheet(theme.STYLESHEET)
        self.choice = "cancel"
        covered = sum(len(scene.get("photos", [])) for scene in plan)

        column = QVBoxLayout(self)
        column.setContentsMargins(22, 18, 22, 16)
        column.setSpacing(12)

        heading = QLabel(
            f"{covered} frames, {len(plan)} "
            f"scene{'' if len(plan) == 1 else 's'}")
        heading.setObjectName("clusterTitle")
        heading.setFont(theme.display(17))
        column.addWidget(heading)

        lead = QLabel(
            "A scene is frames from the same place and light, grouped by "
            "when they were taken. One call per scene writes a treatment "
            "for the frame the assessment ranked highest and shares it with "
            "the rest, so a scene develops as one edit. One call per frame "
            "asks about each separately."
            + (f" {done} of these frames already have directions."
               if done else ""))
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        lead.setFont(theme.body(10))
        column.addWidget(lead)

        # No style is picked here, because the right one depends on the
        # scene: each call is shown every style and says which the
        # photograph asked for.
        styles = list(styles)
        if styles:
            self.styles_note = QLabel(
                f"Every scene is offered all {len(styles)} of your personal "
                "styles and uses the one that suits it — "
                + ", ".join(str(name) for name in styles[:4])
                + (f" and {len(styles) - 4} more." if len(styles) > 4 else "."))
            self.styles_note.setObjectName("hint")
            self.styles_note.setWordWrap(True)
            self.styles_note.setFont(theme.body(9))
            column.addWidget(self.styles_note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 8, 0)
        inner.setSpacing(8)
        for scene in plan:
            inner.addWidget(SceneStrip(
                scene, loader,
                scene_when(scene.get("photos", []), photos_root)))
        inner.addStretch(1)
        scroll.setWidget(holder)
        column.addWidget(scroll, 1)

        row = QHBoxLayout()
        row.setSpacing(10)
        share = QPushButton(
            f"One per scene ({len(plan)} "
            f"call{'' if len(plan) == 1 else 's'})")
        share.setObjectName("primary")
        share.setFont(theme.body(10))
        share.setCursor(Qt.CursorShape.PointingHandCursor)
        share.clicked.connect(lambda: self._choose("scene"))
        row.addWidget(share)
        # Three shapes of the same question. Asking again for frames that
        # already have answers is the one that spends money for nothing, so
        # it never wears the primary button and always says its price.
        covered = sum(len(scene.get("photos", [])) for scene in plan)
        if waiting and done:
            buttons = [(f"Only the {waiting} without ({waiting} calls)",
                        "missing"),
                       (f"Redo all {done + waiting} ({done + waiting} calls)",
                        "all")]
        elif waiting:
            buttons = [(f"Every frame ({waiting} calls)", "all")]
        else:
            buttons = [(f"Redo every frame ({covered} calls)", "all")]
        for label, choice in buttons:
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setFont(theme.body(10))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _=False, value=choice: self._choose(value))
            row.addWidget(button)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ghost")
        cancel.setFont(theme.body(10))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        column.addLayout(row)
        self.resize(980, 720)

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()


def ask_suggestion_scope(parent, waiting: int, done: int, plan=(),
                         loader=None, photos_root=None, styles=()) -> str:
    """What to spend, put as the choice it is.

    "scene" asks once per scene and shares the answer; "missing" asks for
    every frame without directions; "all" pays again for answers already
    given, and is styled as the destructive act it is.

    Where there is a scene plan and the frames can be shown, the grouping
    is put on screen rather than summarised as a number.
    """
    plan = list(plan)
    if plan and loader is not None:
        dialog = ScenePlanDialog(parent, plan, waiting, done, loader,
                                 photos_root, styles)
        dialog.exec()
        return dialog.choice
    scene_count = len(plan)
    box = QMessageBox(parent)
    box.setWindowTitle("Ask for editing directions?")
    box.setIcon(QMessageBox.Icon.Question)
    scene = only = None
    if waiting and done:
        box.setText(
            f"{done} of these frames already have editing directions.")
        box.setInformativeText(
            f"Asking again for all of them costs another call per frame "
            f"for answers you already have.\n\n"
            f"{waiting} frame{'' if waiting == 1 else 's'} "
            f"{'has' if waiting == 1 else 'have'} no directions yet.")
        if scene_count:
            scene = box.addButton(
                f"The {waiting} without — one per scene "
                f"({scene_count} call{'' if scene_count == 1 else 's'})",
                QMessageBox.ButtonRole.AcceptRole)
        only = box.addButton(
            f"Only the {waiting} without",
            QMessageBox.ButtonRole.AcceptRole)
        box.addButton(
            "Redo all of them", QMessageBox.ButtonRole.DestructiveRole)
    elif waiting:
        box.setText(
            f"{waiting} marked frame{'' if waiting == 1 else 's'} fall into "
            f"{scene_count} scene{'' if scene_count == 1 else 's'}.")
        box.setInformativeText(
            "One call per scene writes a treatment for each scene's "
            "best-ranked frame and shares it with the rest, so a scene "
            "develops as one edit rather than several that disagree. One "
            "call per frame asks about every frame separately.")
        scene = box.addButton(
            f"One per scene ({scene_count} "
            f"call{'' if scene_count == 1 else 's'})",
            QMessageBox.ButtonRole.AcceptRole)
        only = box.addButton(
            f"Every frame ({waiting} calls)",
            QMessageBox.ButtonRole.AcceptRole)
    else:
        box.setText("Every marked frame already has editing directions.")
        box.setInformativeText(
            "Asking again replaces answers you already have, and costs "
            "another call for each frame.")
        box.addButton("Ask again", QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(scene or only or cancel)
    box.exec()
    clicked = box.clickedButton()
    if clicked is cancel:
        return "cancel"
    if scene is not None and clicked is scene:
        return "scene"
    return "missing" if only is not None and clicked is only else "all"


def launch_suggestions(page, payload: dict) -> None:
    """Ask for editing directions, having first asked what to spend.

    Shared by the assessment page and the suggestions page, which are two
    doors into the same decision. Every frame is a separate call to a
    model, so the scope -- and whether scenes share one answer -- is put to
    the photographer rather than assumed.
    """
    from opencull_gui import scenes

    marked = set(payload.get("selected_photos") or [])
    done = set(payload.get("processed_photos") or [])
    waiting = sorted(marked - done)
    if not marked:
        return
    targets = waiting if waiting else sorted(marked)
    plan = scenes.plan_for(page.shortlist, targets)
    # A scene plan is only worth offering when it actually saves calls.
    offer = plan if 0 < len(plan) < len(targets) else []

    if done and waiting:
        choice = page._ask_scope(len(waiting), len(done), offer)
    elif done:
        # Asking again for a shoot that already has answers is still a
        # shoot made of scenes. Withholding the scene plan here charged a
        # call per frame for work seven calls would have redone.
        choice = page._ask_scope(0, len(done), offer)
    elif offer:
        choice = page._ask_scope(len(targets), 0, offer)
    else:
        choice = "all"
    if choice == "cancel":
        return

    if choice == "scene":
        scenes.write_plan(
            page.directions.recipes, page.shortlist.path,
            int(payload.get("selection_revision") or 0), plan)
        photos = sorted(scene["representative"] for scene in plan)
        covered = sum(len(scene["photos"]) for scene in plan)
        page._report(
            f"Asking about {len(photos)} scene"
            f"{'' if len(photos) == 1 else 's'}; the answers will be shared "
            f"across {covered} frames.")
    else:
        photos = waiting if choice == "missing" else sorted(marked)
        page._report(
            f"Asking for editing directions for {len(photos)} "
            f"frame{'' if len(photos) == 1 else 's'}.")
    page.suggested.emit(str(payload.get("default_path", "")), photos)
