"""Building a personal style profile, from photographs you already edited.

The profile describes how this photographer edits, not what they shoot, so
it is read from finished work rather than from a shoot. It is deliberately
reachable from the window's chrome rather than from any one folder: it
belongs to the person, and one profile serves every project.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.style import StyleProfileError, StyleProfileStore

from . import theme
from .widgets import ElidedLabel, short_path

# The kernel reads at most this many examples, so asking for more spends
# nothing extra and says something untrue about what was read.
EXAMPLE_LIMIT = 64

# Set on the items themselves; a row's painted height comes from the
# stylesheet, which sizeHintForRow does not know about.
PROFILE_ROW = 32


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
        marks = {"reading": "reading…", "read": "read ✓"}
        self.badge.setText(marks.get(state, ""))
        self.badge.setVisible(bool(marks.get(state)))
        if marks.get(state):
            self.badge.adjustSize()
            self.badge.move(
                5 + self.SIZE - self.badge.width() - 4, 9)
            self.badge.raise_()
        self.setProperty("kept", "true" if state == "read" else "false")
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

        self.profiles = QListWidget()
        self.profiles.setObjectName("treatmentList")
        self.profiles.setFixedHeight(PROFILE_ROW + 10)
        self.profiles.currentRowChanged.connect(self._chose)
        layout.addWidget(self.profiles)

        scroll = QScrollArea()
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
        layout.addWidget(scroll, 1)

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
        layout.addWidget(self.examples_scroll, 1)
        self.examples: list[Path] = []
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

        self.name_button = QPushButton("Name it…")
        self.name_button.setObjectName("ghost")
        self.name_button.setFont(theme.body(10))
        self.name_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.name_button.setToolTip(
            "Call this profile what you call it. Several profiles otherwise "
            "all read as \"Personal style\".")
        self.name_button.clicked.connect(self.rename)
        actions.addWidget(self.name_button)

        self.forget_button = QPushButton("Stop using it")
        self.forget_button.setObjectName("ghost")
        self.forget_button.setFont(theme.body(10))
        self.forget_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forget_button.setToolTip(
            "Suggestions go back to three treatments. The profile stays on "
            "disk.")
        self.forget_button.clicked.connect(self.forget)
        actions.addWidget(self.forget_button)
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
                self.refresh()
                self._report(
                    "Your style profile is ready. Choose it to put it "
                    "to use.", "ok")
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
        self.profiles.blockSignals(True)
        self.profiles.clear()
        for item in self.available:
            mark = "✓" if item["path"] == selected else " "
            confidence = (
                f"   ·   {item['confidence']:.0%} confident"
                if item.get("confidence") is not None else "")
            group = str(item.get("group") or "")
            source = (
                f"   ·   from {group}"
                if group and group != item["name"] else "")
            entry = QListWidgetItem(
                f" {mark}  {item['name']}   ·   {item['examples']} "
                f"photograph{'' if item['examples'] == 1 else 's'}"
                f"{source}{confidence}")
            entry.setData(Qt.ItemDataRole.UserRole, item["path"])
            entry.setToolTip(
                f"{item.get('model_name') or item['name']}\n"
                f"{short_path(item['path'])}\n"
                "Click to see the photographs it was read from.")
            entry.setSizeHint(QSize(0, PROFILE_ROW))
            self.profiles.addItem(entry)
        self.profiles.blockSignals(False)
        self.profiles.setFixedHeight(
            max(self.profiles.count(), 1) * PROFILE_ROW + 10)
        if selected:
            for row, item in enumerate(self.available):
                if item["path"] == selected:
                    self.profiles.blockSignals(True)
                    self.profiles.setCurrentRow(row)
                    self.profiles.blockSignals(False)
                    break
        self.forget_button.setEnabled(bool(selected))
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
        self.build_button.setEnabled(bool(self.examples))

    def _chose(self, row: int) -> None:
        self.show_group(row)
        if not (0 <= row < len(self.available)):
            return
        try:
            self.store.select(self.available[row]["path"])
        except StyleProfileError as exc:
            self._report(str(exc), "alarm")
            return
        self.refresh()
        self._report(
            f"{self.available[row]['name']} is the profile suggestions will "
            "use.", "ok")

    def rename(self) -> None:
        """Name the highlighted profile, so several can be told apart."""
        row = self.profiles.currentRow()
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

    def forget(self) -> None:
        self.store.forget()
        self.refresh()
        self._report(
            "No profile is in use. The photographs and the profile itself are "
            "untouched.", "ok")

    # --- building -------------------------------------------------------

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
        known = {str(path) for path in self.examples}
        added = 0
        capped = False
        for value in chosen:
            path = Path(value).expanduser()
            if str(path) in known or not path.is_file():
                continue
            if len(self.examples) >= EXAMPLE_LIMIT:
                capped = True
                break
            self.examples.append(path)
            known.add(str(path))
            added += 1
        self.viewing = ""
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
        self.examples = []
        self.viewing = ""
        self.build_button.setText("Extract profile")
        self.show_examples()

    def drop_example(self, path: str) -> None:
        self.examples = [
            item for item in self.examples if str(item) != str(path)]
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
        self.examples_title.setText(
            "PHOTOGRAPHS TO READ" if count else "NO PHOTOGRAPHS CHOSEN YET")
        passes = -(-count // 8)
        self.examples_hint.setText(
            f"{count} chosen · read in {passes} "
            f"pass{'' if passes == 1 else 'es'} of up to eight, "
            "each pass refining what the last one learned. "
            "Double-click a photograph to take it out."
            if count else
            "Add finished photographs of your own -- edited the way you "
            "like them. Fifteen to forty that genuinely look alike teach "
            "a sharper profile than a hundred mixed ones.")
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
        chosen = [str(path) for path in self.examples]
        if not chosen:
            self._report(
                "Add some finished photographs first.", "alarm")
            return
        dropped = ""
        existing = self.store.selected()
        mode = "replace"
        if existing:
            answer = self._ask_mode(len(chosen))
            if answer == "cancel":
                return
            mode = answer
        try:
            self.jobs.add_style_profile(
                chosen, output=str(self.store.suggested_output(_stamp())),
                existing=existing if mode == "update" else "",
                mode=mode, provider_profile_id=provider,
                limit=EXAMPLE_LIMIT)
        except Exception as exc:
            self._report(str(exc), "alarm")
            return
        self._report(
            f"Reading your style from {len(chosen)} "
            f"photograph{'' if len(chosen) == 1 else 's'}.{dropped} It "
            "appears here when it finishes; choose it then to put it to use.",
            "ok")

    def _ask_mode(self, count: int) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("Refine or start again?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("You already have a style profile in use.")
        box.setInformativeText(
            f"These {count} photograph{'' if count == 1 else 's'} can refine "
            "what Darkimiya already knows about how you edit, or describe "
            "your style from scratch.\n\n"
            "Either way the existing profile stays on disk and can be chosen "
            "again.")
        refine = box.addButton("Refine the current one", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Start again", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(refine)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel:
            return "cancel"
        return "update" if clicked is refine else "replace"


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
