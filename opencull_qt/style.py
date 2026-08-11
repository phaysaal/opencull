"""Building a personal style profile, from photographs you already edited.

The profile describes how this photographer edits, not what they shoot, so
it is read from finished work rather than from a shoot. It is deliberately
reachable from the window's chrome rather than from any one folder: it
belongs to the person, and one profile serves every project.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.style import (
    StyleProfileError,
    StyleProfileStore,
    profile_summary,
    read_profile,
)

from . import theme
from .widgets import ElidedLabel, Filmstrip, short_path

# The kernel reads at most this many examples, so asking for more spends
# nothing extra and says something untrue about what was read.
EXAMPLE_LIMIT = 64

# Set on the items themselves; a row's painted height comes from the
# stylesheet, which sizeHintForRow does not know about.
PROFILE_ROW = 32


class ProfileCard(QFrame):
    """One profile, shown by the photographs that taught it."""

    chosen = Signal(int)
    rename_wanted = Signal(int)
    retire_wanted = Signal(int)

    WIDTH = 236

    def __init__(self, index: int, item: dict, in_use: bool,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.index = index
        self.item = item
        self.setObjectName("card")
        self.setFixedWidth(self.WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("kept", "true" if in_use else "false")
        self.setToolTip(
            f"{item.get('model_name') or item['name']}\n"
            "Click to read how this profile was extracted.")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.strip = Filmstrip(3)
        self.strip.setFixedHeight(96)
        self.strip.clicked.connect(lambda: self.chosen.emit(self.index))
        self.strip.set_opens(True, "Read how this profile was extracted.")
        layout.addWidget(self.strip)

        body = QVBoxLayout()
        body.setContentsMargins(11, 8, 11, 9)
        body.setSpacing(3)
        title = ElidedLabel(str(item["name"]))
        title.setObjectName("cardName")
        title.setFont(theme.body(10, weight=QFont.Weight.DemiBold))
        body.addWidget(title)
        group = str(item.get("group") or "")
        if group and group != item["name"]:
            source = ElidedLabel(group)
            source.setObjectName("rowPath")
            source.setFont(theme.mono(8))
            body.addWidget(source)
        confidence = (
            f" · {item['confidence']:.0%} confident"
            if item.get("confidence") is not None else "")
        count = QLabel(
            f"{item['examples']} photograph"
            f"{'' if item['examples'] == 1 else 's'}{confidence}")
        count.setObjectName("cardCount")
        count.setFont(theme.body(9))
        body.addWidget(count)

        # What can be done to one profile belongs on that profile,
        # rather than on a button that acts on whichever was last
        # touched.
        row = QHBoxLayout()
        row.setSpacing(7)
        for label, tip, signal in (
            ("Rename", "Call this profile something of your own.",
             self.rename_wanted),
            ("Remove", "Take it off the shelf. The profile itself is "
                       "kept, so renders made under it can still be "
                       "explained.", self.retire_wanted),
        ):
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setProperty("slim", True)
            button.setFont(theme.body(9))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tip)
            button.clicked.connect(
                lambda _checked=False, emit=signal: emit.emit(self.index))
            row.addWidget(button)
        row.addStretch(1)
        body.addLayout(row)
        layout.addLayout(body)

    def paint_samples(self) -> None:
        """Three of its photographs, spread across the set."""
        paths = [
            Path(value) for value in self.item.get("example_paths") or []
            if Path(value).is_file()]
        if not paths:
            return
        step = max(1, len(paths) // 3)
        for slot, path in enumerate(paths[::step][:3]):
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and max(size.width(), size.height()) > 220:
                scale = 220 / max(size.width(), size.height())
                reader.setScaledSize(QSize(
                    max(1, int(size.width() * scale)),
                    max(1, int(size.height() * scale))))
            image = reader.read()
            if not image.isNull():
                self.strip.set_frame(slot, QPixmap.fromImage(image))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.chosen.emit(self.index)
        super().mousePressEvent(event)


class ExampleTile(QFrame):
    """One chosen photograph, waiting to be read or already read."""

    dropped = Signal(str)

    SIZE = 132

    def __init__(self, path: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.path = Path(path)
        self.setObjectName("frame")
        self.setFixedSize(self.SIZE + 10, self.SIZE + 26)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 3)
        layout.setSpacing(2)
        self.image = QLabel("…")
        self.image.setObjectName("frameImage")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setFixedSize(self.SIZE, self.SIZE)
        layout.addWidget(self.image)
        caption = QLabel(self.path.name)
        caption.setObjectName("frameName")
        caption.setFont(theme.mono(7))
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(caption)
        self.badge = QLabel("", self)
        self.badge.setObjectName("tileBadge")
        self.badge.setFont(theme.body(8))
        self.badge.hide()
        self.setToolTip(str(self.path))

    def paint_thumbnail(self) -> None:
        """Decode at thumbnail size, which a finished export needs."""
        reader = QImageReader(str(self.path))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > self.SIZE:
            scale = self.SIZE / max(size.width(), size.height())
            reader.setScaledSize(QSize(
                max(1, int(size.width() * scale)),
                max(1, int(size.height() * scale))))
        image = reader.read()
        if image.isNull():
            self.image.setText("unreadable")
            return
        self.image.setPixmap(QPixmap.fromImage(image))
        self.image.setText("")

    def set_state(self, state: str) -> None:
        """Waiting, being read now, or already read."""
        marks = {"reading": "reading…", "read": "read ✓", "new": "new"}
        self.badge.setText(marks.get(state, ""))
        self.badge.setVisible(bool(marks.get(state)))
        if marks.get(state):
            self.badge.adjustSize()
            self.badge.move(
                5 + self.SIZE - self.badge.width() - 4, 9)
            self.badge.raise_()
        self.setProperty(
            "kept", "true" if state in {"read", "new"} else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt
        self.dropped.emit(str(self.path))
        super().mouseDoubleClickEvent(event)


class StylePanel(QWidget):
    """Choose which profile is in use, and build a new one.

    The same panel serves two homes: a dialog opened from the window's
    chrome, and the profile phase of a shoot. It is one thing in both,
    because the profile is one thing -- the photographer's, not a copy per
    project.
    """

    done = Signal()

    def __init__(self, store: StyleProfileStore, jobs, providers,
                 parent: QWidget | None = None, closable: bool = True):
        super().__init__(parent)
        self.store = store
        self.jobs = jobs
        self.providers = providers
        self.closable = closable
        self.setObjectName("page")
        self._build()
        self.refresh()
        self.show_examples()

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        heading = QLabel("PERSONAL STYLE")
        heading.setObjectName("bandTitle")
        heading.setFont(theme.display(8))
        layout.addWidget(heading)

        lead = QLabel(
            "Darkimiya reads photographs you have already edited the way you "
            "like them, and writes down how you edit. The suggestion pass "
            "then offers a fourth treatment in your own hand, beside the "
            "standard, signature and creative ones.")
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        lead.setFont(theme.body(10))
        layout.addWidget(lead)

        # A photographer knows a body of work by its pictures, so the
        # profiles are shown as their pictures rather than as a line of
        # text about them.
        self.profiles_scroll = QScrollArea()
        self.profiles_scroll.setWidgetResizable(True)
        self.profiles_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.profiles_scroll.setFixedHeight(196)
        self.profiles_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        profiles_holder = QWidget()
        profiles_holder.setObjectName("page")
        self.profiles_row = QHBoxLayout(profiles_holder)
        self.profiles_row.setContentsMargins(0, 2, 0, 2)
        self.profiles_row.setSpacing(12)
        self.profiles_scroll.setWidget(profiles_holder)
        layout.addWidget(self.profiles_scroll)
        self.cards: list[ProfileCard] = []
        self.current_profile = -1

        self.detail_scroll = scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # Without this the holder grows to its widest child -- a full path --
        # and every wrapped paragraph then lays out at that width, off screen.
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QFrame()
        holder.setObjectName("decision")
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(14, 12, 14, 12)
        self.body.setSpacing(9)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 4)

        # The examples a new profile will be read from: chosen, seen,
        # and then watched as they are read. A set of photographs is
        # easier to judge as pictures than as a count.
        examples_head = QHBoxLayout()
        examples_head.setSpacing(10)
        self.examples_title = QLabel("")
        self.examples_title.setObjectName("bandTitle")
        self.examples_title.setFont(theme.display(8))
        examples_head.addWidget(self.examples_title)
        examples_head.addStretch(1)
        self.add_button = QPushButton("Add photographs…")
        self.add_button.setObjectName("ghost")
        self.add_button.setFont(theme.body(9))
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.setToolTip(
            "Add finished photographs of your own. Add from several "
            "folders if the look lives across them.")
        self.add_button.clicked.connect(
            lambda _checked=False: self.add_examples())
        examples_head.addWidget(self.add_button)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("ghost")
        self.clear_button.setFont(theme.body(9))
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.setToolTip("Take every chosen photograph out.")
        self.clear_button.clicked.connect(
            lambda _checked=False: self.clear_examples())
        examples_head.addWidget(self.clear_button)
        layout.addLayout(examples_head)

        self.examples_hint = QLabel("")
        self.examples_hint.setObjectName("hint")
        self.examples_hint.setWordWrap(True)
        self.examples_hint.setFont(theme.body(9))
        layout.addWidget(self.examples_hint)

        # With nothing chosen, the page offers the act rather than
        # describing its absence.
        start = QHBoxLayout()
        start.setContentsMargins(0, 6, 0, 6)
        self.create_button = QPushButton("Create new profile")
        self.create_button.setObjectName("primary")
        self.create_button.setFont(theme.body(10))
        self.create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_button.setToolTip(
            "Choose finished photographs of your own -- edited the way "
            "you like them -- and read a style from them.")
        self.create_button.clicked.connect(
            lambda _checked=False: self.add_examples())
        start.addWidget(self.create_button)
        start.addStretch(1)
        layout.addLayout(start)

        self.examples_scroll = QScrollArea()
        self.examples_scroll.setWidgetResizable(True)
        self.examples_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.examples_scroll.setMinimumHeight(190)
        examples_holder = QWidget()
        examples_holder.setObjectName("page")
        self.examples_grid = QGridLayout(examples_holder)
        self.examples_grid.setContentsMargins(0, 4, 6, 4)
        self.examples_grid.setSpacing(10)
        self.examples_scroll.setWidget(examples_holder)
        layout.addWidget(self.examples_scroll, 4)
        self.examples: list[Path] = []
        self.staged: list[Path] = []
        self.showing = -1
        self.viewing = ""
        self.tiles: dict[str, ExampleTile] = {}
        self._pending_thumbnails: list[str] = []

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.build_button = QPushButton("Extract profile")
        self.build_button.setObjectName("primary")
        self.build_button.setFont(theme.body(10))
        self.build_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.build_button.setToolTip(
            "Read your style from the photographs above. Every one is "
            "shown to a vision model, so this costs.")
        self.build_button.clicked.connect(
            lambda _checked=False: self.build())
        actions.addWidget(self.build_button)


        actions.addStretch(1)

        if self.closable:
            close = QPushButton("Done")
            close.setObjectName("ghost")
            close.setFont(theme.body(10))
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            close.clicked.connect(self.done)
            actions.addWidget(close)
        layout.addLayout(actions)

        # A run that says nothing reads as a run that died -- and this
        # one did die twice in silence. The program narrates each step
        # it takes; the meter and the line beneath it are that
        # narration, shown while the run lasts and gone once it ends.
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(4)
        self.meter.hide()
        layout.addWidget(self.meter)
        self.stage = QLabel("")
        self.stage.setObjectName("hint")
        self.stage.setWordWrap(True)
        self.stage.setFont(theme.body(9))
        self.stage.hide()
        layout.addWidget(self.stage)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        layout.addWidget(self.status)
        # Whatever height is left over belongs at the foot of the page.
        # Without this the spare space is shared out between the labels,
        # and a page with both scrolling panes put away drifts apart.
        layout.addStretch(1)

    # --- state ----------------------------------------------------------

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def show_run(self, job: dict | None) -> None:
        """What the profile run is doing, while it is doing it.

        And what became of it once, when it ends: a profile that is
        ready says so and appears in the list, a run that stopped says
        that instead.
        """
        active = bool(job) and job.get("status") in {
            "running", "queued", "stopping", "detached"}
        self.meter.setVisible(active)
        self.stage.setVisible(active)
        self.build_button.setEnabled(not active)
        if not active:
            settled = str((job or {}).get("id") or "")
            if not job or settled == getattr(self, "_settled", ""):
                return
            self._settled = settled
            # A run that has ended is reported once, when it ends --
            # not on every poll for the rest of the session.
            for tile in self.tiles.values():
                tile.set_state("")
            if job.get("status") == "completed":
                # The photographs have been read; keeping them staged
                # would invite reading them again.
                self.staged = []
                self.examples = []
                self.viewing = ""
                self.showing = -1
                self.build_button.setText("Extract profile")
                self.refresh()
                self.show_examples()
                self._report(
                    "Your style profile is ready. It is on the shelf "
                    "above.", "ok")
            elif job.get("status") == "failed":
                self._report(
                    "That profile run stopped before it finished. "
                    + str(job.get("message") or ""), "alarm")
            return
        progress = job.get("progress") or {}
        self.meter.setValue(round(100 * float(progress.get("fraction", 0))))
        said = str(progress.get("stage") or "").strip()
        self.stage.setText(
            f"{said}." if said else "The profile worker is starting.")
        self._mark_examples_read(job)

    def _mark_examples_read(self, job: dict) -> None:
        """Show which photographs have gone to the model, and which are going.

        The run reads them eight at a time and records each pass as it
        lands, so the tiles can say what has been seen rather than
        leaving a meter to stand for all of it.
        """
        from opencull_gui.shortlist import trace_for

        if not self.examples:
            return
        trace = trace_for(job)
        passes = 0
        if trace is not None and Path(trace).is_file():
            try:
                lines = Path(trace).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            passes = sum(
                1 for line in lines
                if '"kind": "gen"' in line or '"kind":"gen"' in line)
        read = passes * 8
        for index, path in enumerate(self.examples):
            tile = self.tiles.get(str(path))
            if tile is None:
                continue
            tile.set_state(
                "read" if index < read
                else "reading" if index < read + 8 else "")

    def refresh(self) -> None:
        state = self.store.public()
        self.available = state["available"]
        selected = state["selected"]
        while self.profiles_row.count():
            item = self.profiles_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.cards = []
        for index, item in enumerate(self.available):
            card = ProfileCard(index, item, item["path"] == selected)
            card.chosen.connect(self.show_profile)
            card.rename_wanted.connect(self.rename)
            card.retire_wanted.connect(self.retire)
            self.profiles_row.addWidget(card)
            self.cards.append(card)
        self.profiles_row.addStretch(1)
        self.profiles_scroll.setVisible(bool(self.cards))
        QTimer.singleShot(0, self._paint_next_card)
        if selected:
            for row, item in enumerate(self.available):
                if item["path"] == selected:
                    self.current_profile = row
                    break
        if not self.available:
            # With nothing on the shelf there is nothing to open, so the
            # page says what a profile is for instead of showing a gap.
            self._show(None, "")
            self.detail_scroll.show()
        elif self.showing < 0:
            self.detail_scroll.hide()
        elif 0 <= self.showing < len(self.available):
            self._show(state["profile"], selected)

    def _show(self, summary, selected: str) -> None:
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if summary is None:
            empty = QLabel(
                "No style profile is in use. Suggestions offer the standard, "
                "signature and creative treatments only."
                if not self.available else
                "Choose a profile above to use it.")
            empty.setObjectName("hint")
            empty.setWordWrap(True)
            empty.setFont(theme.body(10))
            self.body.addWidget(empty)
            self.body.addStretch(1)
            return
        title = QLabel(summary["name"])
        title.setObjectName("clusterTitle")
        title.setFont(theme.display(15))
        self.body.addWidget(title)

        # Elided, not wrapped: a path is read from both ends and a wrapped
        # one would be the widest thing on the page.
        where = ElidedLabel(short_path(selected))
        where.setObjectName("rowPath")
        where.setFont(theme.mono(8))
        self.body.addWidget(where)

        for label, text in summary["fields"]:
            name = QLabel(label.upper())
            name.setObjectName("axisName")
            name.setFont(theme.display(7))
            self.body.addWidget(name)
            body = QLabel(text)
            body.setObjectName("axisBody")
            body.setWordWrap(True)
            body.setFont(theme.body(9))
            self.body.addWidget(body)
        self.body.addStretch(1)

    def _paint_next_card(self) -> None:
        for card in self.cards:
            if not getattr(card, "_painted", False):
                card._painted = True
                card.paint_samples()
                QTimer.singleShot(0, self._paint_next_card)
                return

    def show_profile(self, row: int) -> None:
        """Open a profile's reading, or close it if it is already open.

        The page opens as a shelf of profiles and nothing else. What a
        profile says is worth a click, and worth a second click to put
        away again.
        """
        if not (0 <= row < len(self.available)):
            return
        if row == self.showing:
            self.hide_profile()
            return
        self.current_profile = row
        self.showing = row
        item = self.available[row]
        self.show_group(row)
        try:
            summary = profile_summary(read_profile(Path(item["path"])))
        except StyleProfileError as exc:
            self._report(str(exc), "alarm")
            return
        summary["name"] = item["name"]
        self._show(summary, item["path"])
        self.detail_scroll.show()

    def hide_profile(self) -> None:
        """Put a profile's reading away, and the staged set back."""
        self.showing = -1
        self.detail_scroll.hide()
        self.viewing = ""
        self.examples = list(self.staged)
        self.build_button.setText("Extract profile")
        self.show_examples()

    def show_group(self, row: int) -> None:
        """The photographs an existing profile was read from."""
        if not (0 <= row < len(self.available)):
            return
        item = self.available[row]
        paths = [Path(value) for value in item.get("example_paths") or []]
        missing = [path for path in paths if not path.is_file()]
        self.viewing = item["path"]
        self.examples = [path for path in paths if path.is_file()]
        self.show_examples()
        self.examples_title.setText(
            f"PHOTOGRAPHS BEHIND {str(item['name']).upper()}")
        self.examples_hint.setText(
            f"{len(paths)} photograph{'' if len(paths) == 1 else 's'} read "
            f"into this profile"
            + (f", {len(missing)} no longer on disk" if missing else "")
            + ". Add more to refine it, or Clear to start a set of your "
            "own.")
        self.build_button.setText("Refine this profile")
        self.build_button.setEnabled(False)
        self.build_button.setToolTip(
            "Add photographs to refine this profile. Its own are already "
            "in it, so only the new ones are read.")

    def _chose(self, row: int) -> None:
        if not (0 <= row < len(self.available)):
            return
        self.show_group(row)
        try:
            self.store.select(self.available[row]["path"])
        except StyleProfileError as exc:
            self._report(str(exc), "alarm")
            return
        self.refresh()
        self._report(
            f"{self.available[row]['name']} is the profile suggestions will "
            "use.", "ok")

    def retire(self, row: int) -> None:
        """Take one profile off the shelf, keeping the profile itself."""
        if not (0 <= row < len(self.available)):
            return
        item = self.available[row]
        if not self._confirm_retire(str(item["name"])):
            return
        try:
            self.store.retire(item["path"])
        except StyleProfileError as exc:
            self._report(str(exc), "alarm")
            return
        self.showing = -1
        self.current_profile = -1
        self.detail_scroll.hide()
        self.refresh()
        self._report(
            f"{item['name']} is off the shelf, and kept.", "ok")

    def _confirm_retire(self, name: str) -> bool:
        """Ask before taking a profile off the shelf."""
        box = QMessageBox(self)
        box.setWindowTitle("Remove this profile?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Remove {name} from the shelf?")
        box.setInformativeText(
            "It stops being offered and stops being used. The profile "
            "itself is kept in a Retired profiles folder, so any render "
            "made under it can still be explained.")
        remove = box.addButton(
            "Remove", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep it", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is remove

    def rename(self, row: int = -1) -> None:
        """Name one profile, so several can be told apart."""
        if row < 0:
            row = self.current_profile
        if not (0 <= row < len(self.available)):
            self._report("Choose a profile to name first.", "alarm")
            return
        item = self.available[row]
        name, accepted = QInputDialog.getText(
            self, "Name this profile",
            "What should this profile be called?",
            text=str(item.get("name") or ""))
        if not accepted:
            return
        try:
            self.store.set_name(item["path"], name)
        except StyleProfileError as exc:
            self._report(str(exc), "alarm")
            return
        self.refresh()
        self._report(
            f"It is called {name.strip()} now." if name.strip()
            else "Back to the profile's own name.", "ok")

    def provider_id(self) -> str:
        profiles = (self.providers.public().get("profiles") or []
                    if self.providers is not None else [])
        return str(profiles[0]["id"]) if profiles else ""

    def add_examples(self) -> None:
        """Stage finished photographs, from as many folders as it takes."""
        chosen, _filter = QFileDialog.getOpenFileNames(
            self, "Choose photographs you have already edited",
            str(Path.home()),
            "Photographs (*.jpg *.jpeg *.png *.tif *.tiff)")
        if not chosen:
            return
        known = {str(path) for path in self.staged}
        if self.viewing:
            # Photographs added while a profile is open refine that
            # profile; its own are already in it, so only the new ones
            # are read.
            known |= {str(path) for path in self.examples}
        added = 0
        capped = False
        for value in chosen:
            path = Path(value).expanduser()
            if str(path) in known or not path.is_file():
                continue
            if len(self.staged) >= EXAMPLE_LIMIT:
                capped = True
                break
            self.staged.append(path)
            known.add(str(path))
            added += 1
        if self.viewing:
            self.examples = self.examples + [
                path for path in self.staged if path not in self.examples]
        else:
            self.examples = list(self.staged)
            self.build_button.setText("Extract profile")
        if capped:
            # Said where the cap bites, rather than after a run has
            # quietly read a subset of what was chosen.
            self._report(
                f"{EXAMPLE_LIMIT} photographs is as many as one profile "
                f"reads, so {len(chosen) - added} were left out. Clear "
                "some to put others in.", "alarm")
        elif added:
            self._report("", "")
        self.show_examples()

    def clear_examples(self) -> None:
        self.staged = []
        self.hide_profile()

    def drop_example(self, path: str) -> None:
        self.examples = [
            item for item in self.examples if str(item) != str(path)]
        if not self.viewing:
            self.staged = list(self.examples)
        self.show_examples()

    def show_examples(self) -> None:
        """Lay out the staged photographs, and say what they will cost."""
        while self.examples_grid.count():
            item = self.examples_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.tiles = {}
        count = len(self.examples)
        # Nothing chosen and nothing being read: the page offers to
        # start, and says nothing else.
        starting = not count and not self.viewing
        self.create_button.setVisible(starting)
        for widget in (self.examples_title, self.examples_hint,
                       self.examples_scroll, self.add_button,
                       self.clear_button, self.build_button):
            widget.setVisible(not starting)
        if starting:
            self._paint_thumbnails = []
            return
        if self.viewing:
            fresh = len(self.staged)
            self.build_button.setEnabled(bool(fresh))
            self.build_button.setText(
                f"Refine with {fresh} new photograph"
                f"{'' if fresh == 1 else 's'}" if fresh
                else "Refine this profile")
            for path in self.staged:
                tile = self.tiles.get(str(path))
                if tile is not None:
                    tile.set_state("new")
            return
        self.examples_title.setText("PHOTOGRAPHS TO READ")
        passes = -(-count // 8)
        self.examples_hint.setText(
            f"{count} chosen · read in {passes} "
            f"pass{'' if passes == 1 else 'es'} of up to eight, "
            "each pass refining what the last one learned. "
            "Double-click a photograph to take it out.")
        self.build_button.setEnabled(bool(count))
        columns = max(1, self.examples_scroll.viewport().width()
                      // (ExampleTile.SIZE + 20))
        for index, path in enumerate(self.examples):
            tile = ExampleTile(path)
            tile.dropped.connect(self.drop_example)
            self.examples_grid.addWidget(
                tile, index // columns, index % columns)
            self.tiles[str(path)] = tile
        self.examples_grid.setColumnStretch(columns, 1)
        self._pending_thumbnails = [str(path) for path in self.examples]
        QTimer.singleShot(0, self._paint_next_thumbnail)

    def _paint_next_thumbnail(self) -> None:
        """One at a time, so a folder of large exports does not freeze."""
        while self._pending_thumbnails:
            key = self._pending_thumbnails.pop(0)
            tile = self.tiles.get(key)
            if tile is None:
                continue
            tile.paint_thumbnail()
            QTimer.singleShot(0, self._paint_next_thumbnail)
            return

    def build(self) -> None:
        """Read a new profile from the photographs staged above."""
        provider = self.provider_id()
        if not provider:
            self._report(
                "Configure a model provider first: reading a style means "
                "showing the photographs to a vision model.", "alarm")
            return
        # Which act this is was settled by which button was pressed, so
        # nothing needs asking. A photographer has more than one taste,
        # and each profile stands on its own: refining one never
        # disturbs another, and starting one never replaces anything.
        if self.viewing:
            chosen = [str(path) for path in self.staged]
            if not chosen:
                self._report(
                    "Add the photographs that should refine this profile. "
                    "Its own are already in it.", "alarm")
                return
            existing, mode = self.viewing, "update"
        else:
            chosen = [str(path) for path in self.staged]
            if not chosen:
                self._report(
                    "Add some finished photographs first.", "alarm")
                return
            existing, mode = "", "replace"
        dropped = ""
        try:
            self.jobs.add_style_profile(
                chosen, output=str(self.store.suggested_output(_stamp())),
                existing=existing, mode=mode, provider_profile_id=provider,
                limit=EXAMPLE_LIMIT)
        except Exception as exc:
            self._report(str(exc), "alarm")
            return
        many = f"{len(chosen)} photograph{'' if len(chosen) == 1 else 's'}"
        self._report(
            (f"Refining that profile with {many}. The refined profile "
             "appears here when it finishes; the one it came from is kept."
             if mode == "update" else
             f"Reading a new profile from {many}.{dropped} It appears here "
             "when it finishes, beside the ones you already have."),
            "ok")

def _stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


class StyleDialog(QDialog):
    """The profile panel, opened from the window's chrome."""

    def __init__(self, store: StyleProfileStore, jobs, providers,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Personal style")
        self.setMinimumSize(660, 620)
        self.setStyleSheet(theme.STYLESHEET)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = StylePanel(store, jobs, providers, self)
        self.panel.done.connect(self.accept)
        layout.addWidget(self.panel)
