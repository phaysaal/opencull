"""Phase 8: the shoot read back, in its own numbers.

Renders what `opencull_gui.debrief.aggregate` counted: the shoot's filter
ladder with real numbers, the tier distributions, and every recorded
disagreement between the photographer and the models. All of it is local;
the page says so, and says plainly that the advice-writing memo is not
built.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.debrief import TIER_ORDER, disagreements, ladder

from . import theme
from .widgets import Paragraph


class DebriefPage(QWidget):
    """The shoot's numbers, oldest filter first."""

    closed = Signal()

    def __init__(self, numbers: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.numbers = numbers
        self._build()

    def _build(self) -> None:
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
        column.setSpacing(20)

        lead = Paragraph(
            "Every number below is a count over this shoot's own records. "
            "Nothing was computed to say it, and no model was asked.")
        lead.setObjectName("hint")
        lead.setFont(theme.body(10))
        lead.setMaximumWidth(760)
        column.addWidget(lead)

        # --- the ladder -------------------------------------------------
        title = QLabel("THE SHOOT'S OWN LADDER")
        title.setObjectName("bandTitle")
        title.setFont(theme.display(8))
        column.addWidget(title)

        frame = QFrame()
        frame.setObjectName("decision")
        grid = QGridLayout(frame)
        grid.setContentsMargins(16, 13, 16, 14)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(7)
        for row, (stage, count, who) in enumerate(ladder(self.numbers)):
            name = QLabel(stage)
            name.setFont(theme.body(10))
            grid.addWidget(name, row, 0)
            value = QLabel(f"{count:,}")
            value.setObjectName("clusterTitle")
            value.setFont(theme.display(12))
            grid.addWidget(value, row, 1)
            deciders = QLabel(who)
            deciders.setObjectName("rowPath")
            deciders.setFont(theme.body(9))
            grid.addWidget(deciders, row, 2)
        grid.setColumnStretch(3, 1)
        column.addWidget(frame)

        # --- the tiers ----------------------------------------------------
        tiers = self.numbers.get("tiers") or {}
        yours = self.numbers.get("your_tiers") or {}
        if tiers or yours:
            title = QLabel("HOW IT WAS JUDGED")
            title.setObjectName("bandTitle")
            title.setFont(theme.display(8))
            column.addWidget(title)

            frame = QFrame()
            frame.setObjectName("decision")
            grid = QGridLayout(frame)
            grid.setContentsMargins(16, 13, 16, 14)
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(7)
            heading = QLabel("")
            grid.addWidget(heading, 0, 0)
            if tiers:
                models = QLabel("THE MODELS")
                models.setObjectName("axisName")
                models.setFont(theme.display(7))
                grid.addWidget(models, 0, 1)
            if yours:
                you = QLabel("YOU")
                you.setObjectName("axisName")
                you.setFont(theme.display(7))
                grid.addWidget(you, 0, 2)
            for row, tier in enumerate(TIER_ORDER, start=1):
                name = QLabel(tier.title())
                name.setFont(theme.body(10))
                grid.addWidget(name, row, 0)
                if tiers:
                    grid.addWidget(
                        QLabel(str(tiers.get(tier, 0))), row, 1)
                if yours:
                    grid.addWidget(
                        QLabel(str(yours.get(tier, 0))), row, 2)
            grid.setColumnStretch(3, 1)
            column.addWidget(frame)

        # --- the disagreements -------------------------------------------
        title = QLabel("WHERE YOU PARTED WAYS")
        title.setObjectName("bandTitle")
        title.setFont(theme.display(8))
        column.addWidget(title)
        frame = QFrame()
        frame.setObjectName("decision")
        rows = QVBoxLayout(frame)
        rows.setContentsMargins(16, 13, 16, 14)
        rows.setSpacing(6)
        for line in disagreements(self.numbers):
            body = Paragraph(line)
            body.setObjectName("axisBody")
            body.setFont(theme.body(9))
            rows.addWidget(body)
        seed = Paragraph(
            "These counts are the seeds of the taste ledger in the Studio: "
            "each one is already recorded beside the evidence it disagreed "
            "with.")
        seed.setObjectName("rowPath")
        seed.setFont(theme.body(8))
        rows.addWidget(seed)
        column.addWidget(frame)

        # --- the memo that is not built ----------------------------------
        memo = Paragraph(
            "Turning these numbers into advice — what to do differently on "
            "the next shoot — needs a model to read them, and that kernel "
            "is not built yet. The numbers will be waiting.")
        memo.setObjectName("hint")
        memo.setFont(theme.body(9))
        memo.setMaximumWidth(760)
        column.addWidget(memo)

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

        title = QLabel("DEBRIEF")
        title.setObjectName("chromeTitle")
        title.setFont(theme.display(11))
        layout.addWidget(title)
        layout.addStretch(1)

        self.progress = QLabel("")
        self.progress.setObjectName("hint")
        self.progress.setFont(theme.body(9))
        layout.addWidget(self.progress)
        self.indicator = self.progress
        return bar
