"""The Studio's Programs room: read, write, check and run Kimiya.

The application's own programs are on the left to read and duplicate;
the photographer's are there to edit. The compiler's word is one button
away and shown verbatim -- the editor never pretends to know Kimiya
better than Kimiya does -- and Run asks for the program's own params,
prefilled from its head, then queues the job on the same rails as every
built-in: provider bundle, key_env, hash pinned, log beside the output.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.programs import ProgramError, ProgramStore

from . import theme
from .widgets import tooltip

# --- the language's own colours ------------------------------------------
#
# kimiya-lang ships its grammar and palette in kimiya.highlight -- "the
# paper's typography": core keywords blue, the world-effecting extension
# magenta, strings brown, numbers cyan, comments gray. The editor speaks
# that, imported from the checkout when it is there so new keywords
# colour themselves the day the language grows them; the frozen copies
# below are the fallback for a machine without the checkout, taken from
# kimiya.lexer as of 2026-08-15.

_KEYWORDS = {
    "pool", "context", "schema", "effect", "domain", "preserve",
    "allow_loss", "param", "memo", "explore", "gen", "select", "judge",
    "check", "retry", "until", "budget", "panel", "paraphrase_prompts",
    "under", "by", "if", "then", "else", "forall", "in", "commit",
    "abstain", "print", "true", "false", "null", "and", "or", "not",
    "contradicts", "irreversible", "recoverable", "fn", "return", "use",
    "pyfn", "python", "agent",
}
_WKEYWORDS = {"act", "observe", "settle", "within", "inv", "compensate",
              "display"}
_TOKEN_RE = re.compile(
    r'(?P<cmt>--[^\n]*)|(?P<str>"(?:\\.|[^"\\])*")|'
    r'(?P<num>\b\d+(?:\.\d+)?\b)|(?P<word>\b[A-Za-z_][A-Za-z0-9_]*\b)')


def language_tokens(checkout: Path | None) -> tuple[set, set, re.Pattern]:
    """The lexer's own sets where the checkout is importable."""
    if checkout is not None and (Path(checkout) / "kimiya").is_dir():
        try:
            if str(checkout) not in sys.path:
                sys.path.insert(0, str(checkout))
            from kimiya.highlight import TOKEN_RE
            from kimiya.lexer import KEYWORDS, WKEYWORDS

            return set(KEYWORDS), set(WKEYWORDS), TOKEN_RE
        except Exception:                            # noqa: BLE001 - frozen
            pass
    return set(_KEYWORDS), set(_WKEYWORDS), _TOKEN_RE


def kim_spans(line: str, keywords: set, wkeywords: set,
              token_re: re.Pattern) -> list[tuple[int, int, str]]:
    """One line's coloured spans: (start, length, class)."""
    spans = []
    for match in token_re.finditer(line):
        kind = match.lastgroup
        text = match.group(0)
        if kind == "word":
            if text in wkeywords:
                kind = "wkw"
            elif text in keywords:
                kind = "kw"
            else:
                continue
        spans.append((match.start(), len(text), str(kind)))
    return spans


class KimiyaHighlighter(QSyntaxHighlighter):
    """The paper's typography, live in the editor."""

    def __init__(self, document, checkout: Path | None = None):
        super().__init__(document)
        self.keywords, self.wkeywords, self.token_re = language_tokens(
            checkout)
        self.formats: dict[str, QTextCharFormat] = {}
        for kind, colour, bold, italic in (
                ("kw", "#1e3c82", True, False),
                ("wkw", "#8c1e5a", True, False),
                ("str", "#783c14", False, False),
                ("num", "#0e7490", False, False),
                ("cmt", "#6e6e6e", False, True)):
            made = QTextCharFormat()
            made.setForeground(QColor(colour))
            if bold:
                made.setFontWeight(QFont.Weight.DemiBold)
            made.setFontItalic(italic)
            self.formats[kind] = made

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt naming
        for start, length, kind in kim_spans(
                text, self.keywords, self.wkeywords, self.token_re):
            style = self.formats.get(kind)
            if style is not None:
                self.setFormat(start, length, style)


class RunDialog(QDialog):
    """The program's own parameters, asked before anything is spent."""

    def __init__(self, name: str, parameters: list[dict[str, Any]],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Run {name}")
        self.setMinimumWidth(520)
        column = QVBoxLayout(self)
        told = QLabel(
            "These are the program's own parameters, read from its head. "
            "The run goes through the queue like every built-in, with "
            "the provider profile's models and key handling.")
        told.setWordWrap(True)
        told.setFont(theme.body(9))
        column.addWidget(told)
        form = QFormLayout()
        self.fields: dict[str, QLineEdit] = {}
        for parameter in parameters:
            field = QLineEdit(str(parameter.get("default") or ""))
            field.setFont(theme.mono(9))
            form.addRow(f"{parameter['name']} ({parameter['type']})", field)
            self.fields[parameter["name"]] = field
        column.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        run = QPushButton("Run")
        run.setObjectName("primary")
        run.clicked.connect(self.accept)
        row.addWidget(run)
        column.addLayout(row)

    def values(self) -> dict[str, str]:
        return {name: field.text() for name, field in self.fields.items()}


class ProgramsDialog(QDialog):
    """Write any Kimiya program; check it; run it on the app's rails."""

    ran = Signal(str, dict)     # program name, parameters

    def __init__(self, store: ProgramStore,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.current = ""
        self.setWindowTitle("Kimiya programs")
        self.resize(1240, 780)

        column = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        side = QVBoxLayout(left)
        side.setContentsMargins(0, 0, 8, 0)
        self.listing = QListWidget()
        self.listing.setObjectName("treatmentList")
        self.listing.currentRowChanged.connect(self._chose)
        side.addWidget(self.listing, 1)
        actions = QHBoxLayout()
        new = QPushButton("New")
        new.setToolTip(tooltip("An empty program under your own name."))
        new.clicked.connect(self._new)
        actions.addWidget(new)
        self.duplicate_button = QPushButton("Duplicate…")
        self.duplicate_button.setToolTip(tooltip(
            "Copy this program under a new name and edit the copy. This "
            "is how editing a built-in starts; the original never "
            "changes."))
        self.duplicate_button.clicked.connect(self._duplicate)
        actions.addWidget(self.duplicate_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete)
        actions.addWidget(self.delete_button)
        side.addLayout(actions)
        split.addWidget(left)

        right = QWidget()
        body = QVBoxLayout(right)
        body.setContentsMargins(8, 0, 0, 0)
        self.title = QLabel("")
        self.title.setFont(theme.display(10))
        body.addWidget(self.title)
        self.editor = QPlainTextEdit()
        self.editor.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.editor.setTabStopDistance(32)
        self.highlighter = KimiyaHighlighter(
            self.editor.document(), store.kimiya_checkout)
        self.editor.textChanged.connect(self._edited)
        body.addWidget(self.editor, 1)
        self.compiler = QPlainTextEdit()
        self.compiler.setReadOnly(True)
        self.compiler.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.compiler.setFixedHeight(150)
        self.compiler.setPlaceholderText(
            "The compiler's word appears here, verbatim.")
        body.addWidget(self.compiler)
        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setFont(theme.body(9))
        row.addWidget(self.status, 1)
        self.check_button = QPushButton("Check")
        self.check_button.setToolTip(tooltip(
            "Run `kimiya check` on this program, kernels resolved the "
            "way a run resolves them."))
        self.check_button.clicked.connect(self._check)
        row.addWidget(self.check_button)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        row.addWidget(self.save_button)
        self.run_button = QPushButton("Run…")
        self.run_button.setObjectName("primary")
        self.run_button.setToolTip(tooltip(
            "Check, then ask for the program's parameters, then queue "
            "the run. Model calls cost; the run's certificate says what "
            "left the machine."))
        self.run_button.clicked.connect(self._run)
        row.addWidget(self.run_button)
        body.addLayout(row)
        split.addWidget(right)
        split.setSizes([340, 900])
        column.addWidget(split)

        self.refresh()

    # --- the listing -------------------------------------------------------

    def refresh(self, select: str = "") -> None:
        wanted = select or self.current
        self.listing.blockSignals(True)
        self.listing.clear()
        chosen_row = 0
        for row, item in enumerate(self.store.catalogue()):
            label = ("  " + item["name"]
                     + ("   · built-in" if item["kind"] == "built-in" else ""))
            entry = QListWidgetItem(label)
            entry.setData(Qt.ItemDataRole.UserRole, item["name"])
            entry.setToolTip(tooltip(item["purpose"]))
            self.listing.addItem(entry)
            if item["name"] == wanted:
                chosen_row = row
        self.listing.blockSignals(False)
        if self.listing.count():
            self.listing.setCurrentRow(chosen_row)
            self._chose(chosen_row)

    def _is_builtin(self, name: str) -> bool:
        return any(item["name"] == name and item["kind"] == "built-in"
                   for item in self.store.catalogue())

    def _chose(self, row: int) -> None:
        entry = self.listing.item(row)
        if entry is None:
            return
        name = str(entry.data(Qt.ItemDataRole.UserRole))
        self.current = name
        builtin = self._is_builtin(name)
        try:
            text = self.store.read(name)
        except ProgramError as exc:
            self.status.setText(str(exc))
            return
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.setReadOnly(builtin)
        self.editor.blockSignals(False)
        self.title.setText(
            f"{name}  —  built-in, read-only; duplicate to edit"
            if builtin else name)
        self.save_button.setEnabled(not builtin)
        self.delete_button.setEnabled(not builtin)
        self.compiler.setPlainText("")
        self.status.setText("")

    def _edited(self) -> None:
        if self.current and not self.editor.isReadOnly():
            self.status.setText("edited — not saved")

    # --- the verbs ----------------------------------------------------------

    def _new(self) -> None:
        name, said = QInputDialog.getText(
            self, "New program",
            "Name (lowercase, digits, - and _; .kim is added):")
        if not said or not name.strip():
            return
        try:
            kept = self.store.save(name, _STARTER)
        except ProgramError as exc:
            self.status.setText(str(exc))
            return
        self.refresh(select=kept.name)

    def _duplicate(self) -> None:
        if not self.current:
            return
        name, said = QInputDialog.getText(
            self, "Duplicate program",
            f"Copy {self.current} as (new name):")
        if not said or not name.strip():
            return
        try:
            kept = self.store.duplicate(self.current, name)
        except ProgramError as exc:
            self.status.setText(str(exc))
            return
        self.refresh(select=kept.name)

    def _delete(self) -> None:
        if not self.current or self._is_builtin(self.current):
            return
        agreed = QMessageBox.question(
            self, "Delete program",
            f"Delete {self.current}? Its past runs' bundles and logs "
            "stay; only the program goes.")
        if agreed != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete(self.current)
        except ProgramError as exc:
            self.status.setText(str(exc))
            return
        self.current = ""
        self.refresh()

    def _save(self) -> bool:
        if not self.current or self._is_builtin(self.current):
            return False
        try:
            self.store.save(self.current, self.editor.toPlainText())
        except ProgramError as exc:
            self.status.setText(str(exc))
            return False
        self.status.setText("saved")
        return True

    def _check(self) -> bool:
        if not self.current:
            return False
        if not self.editor.isReadOnly() and not self._save():
            return False
        try:
            ok, said = self.store.check(self.current)
        except Exception as exc:                     # noqa: BLE001 - shown
            self.compiler.setPlainText(str(exc))
            self.status.setText("the check could not run")
            return False
        self.compiler.setPlainText(said)
        self.status.setText(
            "the compiler accepts it" if ok else "the compiler refuses it")
        return ok

    def _run(self) -> None:
        if not self.current:
            return
        if not self._check():
            return
        try:
            parameters = self.store.parameters(self.current)
        except ProgramError as exc:
            self.status.setText(str(exc))
            return
        asked = RunDialog(self.current, parameters, self)
        if asked.exec() != QDialog.DialogCode.Accepted:
            return
        self.ran.emit(self.current, asked.values())


_STARTER = '''-- What this program is for, in one line: it becomes the listing's
-- description. Check compiles it; Run asks for the params below.

param photos: text = "."
param output: text = "result.json"

use "agents.kim"
-- use python "treatment_kernel.py"

-- Your program. The built-ins on the left are worked examples of the
-- whole grammar: gen, observe, check, judge panels, act and commit.

report := "nothing yet"
check len(report) > 0
act file.overwrite(output, report)
settle until check file_exists(output) within 5
commit(report)
'''
