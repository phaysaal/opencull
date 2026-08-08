"""The Studio: the photographer's own things, as distinct from any shoot's.

Two kinds of state live in this application. A shoot's -- its cull, its
marks, its renders -- belongs to a folder and travels with it. Yours -- the
profiles that describe how you edit, the providers you pay through, and in
time the ledger of every place you overruled a model -- belongs to you and
serves every folder alike. The chrome used to scatter yours across two
buttons; the Studio gives it one room.

Phase 3 of a shoot still appears on the phase bar, because the profile is
an input to the suggestion pass, but it opens the same panel this page
holds: there is one profile machinery, not a copy per door.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .style import StylePanel
from .widgets import Paragraph


class StudioPage(QWidget):
    """Profiles, the taste ledger's reserved place, and providers."""

    closed = Signal()

    def __init__(self, style_profiles, jobs, providers,
                 open_providers, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._open_providers = open_providers
        self._build(style_profiles, jobs, providers)

    def _build(self, style_profiles, jobs, providers) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.bar = self._bar()
        outer.addWidget(self.bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        column = QVBoxLayout(holder)
        column.setContentsMargins(28, 20, 28, 30)
        column.setSpacing(22)

        lead = Paragraph(
            "What lives here is yours rather than any shoot's: it serves "
            "every folder alike, and removing a folder never touches it.")
        lead.setObjectName("hint")
        lead.setFont(theme.body(10))
        lead.setMaximumWidth(760)
        column.addWidget(lead)

        # --- profiles ---------------------------------------------------
        profiles_title = QLabel("PERSONAL PROFILES")
        profiles_title.setObjectName("bandTitle")
        profiles_title.setFont(theme.display(8))
        column.addWidget(profiles_title)

        self.panel = StylePanel(
            style_profiles, jobs, providers, closable=False)
        column.addWidget(self.panel)

        # --- the taste ledger, reserved ---------------------------------
        ledger_title = QLabel("THE TASTE LEDGER")
        ledger_title.setObjectName("bandTitle")
        ledger_title.setFont(theme.display(8))
        column.addWidget(ledger_title)

        ledger = QFrame()
        ledger.setObjectName("decision")
        ledger_column = QVBoxLayout(ledger)
        ledger_column.setContentsMargins(16, 13, 16, 14)
        ledger_column.setSpacing(6)
        ledger_lead = Paragraph(
            "Every tier you overrule and every slider you pull back is "
            "already recorded beside the evidence. This is where those "
            "records will be read back as one thing: your taste, written "
            "down — shown to you, and in time fed into the prompts so the "
            "models argue with you less each shoot.")
        ledger_lead.setObjectName("hint")
        ledger_lead.setFont(theme.body(9))
        ledger_column.addWidget(ledger_lead)
        planned = QLabel("Not built yet. The records it will read already are.")
        planned.setObjectName("rowPath")
        planned.setFont(theme.body(9))
        ledger_column.addWidget(planned)
        column.addWidget(ledger)

        # --- providers --------------------------------------------------
        providers_title = QLabel("PROVIDERS")
        providers_title.setObjectName("bandTitle")
        providers_title.setFont(theme.display(8))
        column.addWidget(providers_title)

        row = QHBoxLayout()
        row.setSpacing(12)
        providers_hint = Paragraph(
            "Who runs the models, and with which credential. Every paid "
            "step in the pipeline goes through the profile chosen here.")
        providers_hint.setObjectName("hint")
        providers_hint.setFont(theme.body(9))
        row.addWidget(providers_hint, 1)

        edit = QPushButton("Providers…")
        edit.setObjectName("ghost")
        edit.setFont(theme.body(10))
        edit.clicked.connect(lambda: self._open_providers())
        row.addWidget(edit)
        column.addLayout(row)

        column.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

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
        back.clicked.connect(self.closed)
        layout.addWidget(back)

        title = QLabel("THE STUDIO")
        title.setObjectName("chromeTitle")
        title.setFont(theme.display(11))
        layout.addWidget(title)
        layout.addStretch(1)
        return bar
