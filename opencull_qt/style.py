"""Building a personal style profile, from photographs you already edited.

The profile describes how this photographer edits, not what they shoot, so
it is read from finished work rather than from a shoot. It is deliberately
reachable from the window's chrome rather than from any one folder: it
belongs to the person, and one profile serves every project.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
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

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.build_button = QPushButton("Build from photographs…")
        self.build_button.setObjectName("primary")
        self.build_button.setFont(theme.body(10))
        self.build_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.build_button.setToolTip(
            "Choose finished photographs. Every one is shown to a vision "
            "model, so this costs.")
        self.build_button.clicked.connect(self.build)
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
            entry = QListWidgetItem(
                f" {mark}  {item['name']}   ·   {item['examples']} "
                f"photograph{'' if item['examples'] == 1 else 's'}{confidence}")
            entry.setData(Qt.ItemDataRole.UserRole, item["path"])
            entry.setToolTip(short_path(item["path"]))
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

    def _chose(self, row: int) -> None:
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

    def build(self) -> None:
        """Read a new profile from photographs the photographer chooses."""
        provider = self.provider_id()
        if not provider:
            self._report(
                "Configure a model provider first: reading a style means "
                "showing the photographs to a vision model.", "alarm")
            return
        chosen, _filter = QFileDialog.getOpenFileNames(
            self, "Choose photographs you have already edited",
            str(Path.home()),
            "Photographs (*.jpg *.jpeg *.png *.tif *.tiff)")
        if not chosen:
            return
        dropped = ""
        if len(chosen) > EXAMPLE_LIMIT:
            # Said in the message that survives, not in one the success
            # message then overwrites: quietly reading 64 of 74 would look
            # like all of them were read.
            dropped = (
                f" You chose {len(chosen)}; the first {EXAMPLE_LIMIT} are "
                "read, which is as many as the profile pass looks at.")
            chosen = chosen[:EXAMPLE_LIMIT]
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
