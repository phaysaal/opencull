"""One window for one shoot, with its phases along the top.

Every phase of a shoot used to be reached from a different place: culling
from a card, the assessment from a second card, development from a menu on
the same card, suggestions from a button inside the assessment, export from
a button inside development. The order was real but invisible, and nothing
on screen said what could be done next or why not.

The shell puts the pipeline where it can be read. One row of numbered
phases, in order, above whichever phase is open. A phase that has happened
is marked. A phase that cannot be entered is dimmed and says what is
missing, because dimming a control without a reason tells somebody they are
wrong without telling them how to be right.

The bar is clickable rather than a wizard: a photographer who sees a render
and wants to re-mark the shortlist should not have to walk back through
three screens to do it.

The shell owns the window chrome -- back, the shoot's name -- so a hosted
page hides its own. Each page keeps its own right-hand indicator, which is
moved into the phase row rather than duplicated: only the page knows what
it counts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from opencull_gui import phases

from . import theme
from .widgets import Paragraph, tooltip

# Blocked and ready must not be told apart by colour alone: the two dimmest
# readable greys in the palette are nearly the same shade, and the only ones
# a disabled control could use while still clearing AA. So the states carry
# a mark as well, and blocked is the one with nothing to offer.
MARKS = {"done": "✓", "running": "…", "ready": "▸"}


class PhaseButton(QPushButton):
    """One phase of the pipeline, in the order it happens."""

    def __init__(self, phase: dict[str, Any]):
        mark = MARKS.get(phase["state"], "")
        super().__init__(
            f"{phase['number']}  {phase['title'].upper()}"
            f"{'  ' + mark if mark else ''}")
        self.phase = phase
        self.setObjectName("phase")
        self.setFont(theme.display(9))
        self.setFlat(True)
        self.setProperty("state", phase["state"])
        self.setProperty("current", False)
        blocked = phase["state"] == "blocked"
        self.setCursor(
            Qt.CursorShape.ForbiddenCursor if blocked
            else Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip(phase["reason"] if blocked else (
            f"{phase['purpose']}\n{phase['detail']}".strip())))

    def set_current(self, current: bool) -> None:
        self.setProperty("current", current)
        self.style().unpolish(self)
        self.style().polish(self)


class PhaseBar(QFrame):
    """The pipeline, as a row.

    Choosing a blocked phase is not silently ignored. It reports the reason,
    which is the only thing the photographer actually wants at that moment.
    """

    chosen = Signal(str)
    refused = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("phaseBar")
        self.setFixedHeight(38)
        self._buttons: dict[str, PhaseButton] = {}
        self._current = ""
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(14, 0, 22, 0)
        self._row.setSpacing(2)
        # Built once and always re-added last, so replacing the phases does
        # not destroy a page's counter along with them.
        self._tail = QWidget(self)
        self._tail_row = QHBoxLayout(self._tail)
        self._tail_row.setContentsMargins(0, 0, 0, 0)

    def show_plan(self, plan: list[dict[str, Any]]) -> None:
        while self._row.count():
            widget = self._row.takeAt(0).widget()
            if widget is not None and widget is not self._tail:
                widget.setParent(None)
        self._buttons = {}
        for phase in plan:
            button = PhaseButton(phase)
            button.clicked.connect(
                lambda _=False, key=phase["id"]: self._pressed(key))
            self._buttons[phase["id"]] = button
            self._row.addWidget(button)
        self._row.addStretch(1)
        self._row.addWidget(self._tail)
        self.set_current(self._current)

    def set_indicator(self, widget: QWidget | None) -> None:
        """Hold the open page's own counter at the end of the row."""
        while self._tail_row.count():
            held = self._tail_row.takeAt(0).widget()
            if held is not None:
                held.setParent(None)
        if widget is not None:
            self._tail_row.addWidget(widget)

    def set_current(self, key: str) -> None:
        self._current = key
        for phase_id, button in self._buttons.items():
            button.set_current(phase_id == key)

    def _pressed(self, key: str) -> None:
        button = self._buttons.get(key)
        if button is not None and button.phase["state"] == "blocked":
            self.refused.emit(button.phase["reason"])
            return
        self.chosen.emit(key)


class Invitation(QWidget):
    """A phase with nothing in it yet, and the offer to start it.

    A cull that has not been run has no report to review, and an assessment
    that has not been run has nothing to mark. Showing an empty page in
    either case would be accurate and useless. This says what the phase
    does, what it will cost, and offers to run it.

    Where the run has a selection to work from, ``shows`` carries the
    photographs in it. Knowing that a run will read twenty-three frames is
    not the same as seeing which twenty-three, and the second is what a
    photographer is actually deciding about.
    """

    def __init__(self, title: str, body: str, action: str, on_action,
                 note: str = "", shows: QWidget | None = None,
                 instead: tuple[str, object] | None = None,
                 asks: tuple[str, str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.shows = shows
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 26)
        card = QFrame()
        card.setObjectName("decision")

        if shows is None:
            # Held in the middle of the window rather than pinned to its top
            # corner: a short message against a wall of empty black reads as
            # a page that failed to load rather than one with nothing in it.
            outer.addStretch(1)
            row = QHBoxLayout()
            row.addStretch(1)
            card.setMaximumWidth(680)
            row.addWidget(card, 3)
            row.addStretch(1)
            outer.addLayout(row)
            outer.addStretch(2)
        else:
            # With the selection on the page there is nothing to centre: the
            # words explain the frames underneath them.
            outer.addWidget(card)
            outer.addSpacing(14)
            outer.addWidget(shows, 1)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(30, 24, 30, 22)
        layout.setSpacing(10)

        heading = QLabel(title)
        heading.setObjectName("clusterTitle")
        heading.setFont(theme.display(20))
        heading.setWordWrap(True)
        layout.addWidget(heading)

        lead = Paragraph(body)
        lead.setObjectName("heroSub")
        lead.setFont(theme.body(11))
        lead.setMaximumWidth(900)
        layout.addWidget(lead)

        if note:
            caution = Paragraph(note)
            caution.setObjectName("hint")
            caution.setFont(theme.body(9))
            caution.setMaximumWidth(900)
            layout.addWidget(caution)

        # Somewhere to say what a model cannot work out by looking. An
        # assessment of a partial eclipse called the crescent a moon and
        # the light nocturnal, and judged the photographs it thought it
        # was looking at -- carefully, and about the wrong subject.
        self.asks = None
        if asks is not None:
            placeholder, said = asks
            prompt = QLabel("What is this shoot?")
            prompt.setObjectName("eyebrow")
            prompt.setFont(theme.display(8))
            layout.addSpacing(4)
            layout.addWidget(prompt)
            self.asks = QPlainTextEdit(said)
            self.asks.setObjectName("about")
            self.asks.setFont(theme.body(10))
            self.asks.setPlaceholderText(placeholder)
            self.asks.setFixedHeight(62)
            self.asks.setToolTip(tooltip(
                "One or two sentences about the subject and the occasion. "
                "It is given to the models that read these frames, which "
                "can describe what is in front of them and cannot know "
                "what it was."))
            layout.addWidget(self.asks)

        layout.addSpacing(6)
        actions = QHBoxLayout()
        self.button = QPushButton(action)
        self.button.setObjectName("primary")
        self.button.setFont(theme.body(10))
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Qt's clicked signal carries a checked flag, and PySide hands it
        # to any slot willing to take an argument -- silently overwriting
        # the first bound default of a lambda that captured, say, a job id.
        # A phase's handler takes no arguments, so none are passed on.
        self.button.clicked.connect(lambda _checked=False: on_action())
        actions.addWidget(self.button)

        # A second way through, for the photographer who wants the phase
        # without the run. Offered beside the primary rather than hidden
        # behind it: not paying is a choice, not a fallback.
        self.other = None
        if instead is not None:
            label, handler = instead
            self.other = QPushButton(label)
            self.other.setObjectName("ghost")
            self.other.setFont(theme.body(10))
            self.other.setCursor(Qt.CursorShape.PointingHandCursor)
            self.other.clicked.connect(lambda _checked=False: handler())
            actions.addWidget(self.other)

        actions.addStretch(1)
        layout.addLayout(actions)

    def said(self) -> str:
        """What the photographer wrote about the shoot, if they were asked."""
        if self.asks is None:
            return ""
        return " ".join(self.asks.toPlainText().split())


class ProjectShell(QWidget):
    """One shoot, its phases, and whichever one is open.

    Pages are built on demand and then kept: rebuilding a development page
    would throw away a loaded photograph and a running render for nothing.
    """

    closed = Signal()
    opened = Signal(str)

    def __init__(self, project: dict, build: Callable[[str], QWidget | None]):
        super().__init__()
        self.setObjectName("page")
        self.project = project
        self._build_page = build
        self._pages: dict[str, QWidget] = {}
        self.current = ""
        self._compose()

    # --- construction ---------------------------------------------------

    def _compose(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._chrome())

        self.bar = PhaseBar()
        self.bar.chosen.connect(self.open_phase)
        self.bar.refused.connect(lambda reason: self.report(reason, "alarm"))
        outer.addWidget(self.bar)

        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        self.status.setContentsMargins(22, 4, 22, 6)
        self.status.hide()
        outer.addWidget(self.status)

    def _chrome(self) -> QWidget:
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

        self.title = QLabel(str(self.project.get("name", "")).upper())
        self.title.setObjectName("chromeTitle")
        self.title.setFont(theme.display(11))
        layout.addWidget(self.title)
        layout.addStretch(1)

        self.subtitle = QLabel("")
        self.subtitle.setObjectName("hint")
        self.subtitle.setFont(theme.body(9))
        layout.addWidget(self.subtitle)
        return bar

    # --- state ------------------------------------------------------------

    def show_plan(self, plan: list[dict[str, Any]]) -> None:
        self.plan = plan
        self._retire_invitations(plan)
        self.bar.show_plan(plan)
        self.bar.set_current(self.current)

    def _retire_invitations(self, plan: list[dict[str, Any]]) -> None:
        """Replace an invitation once the thing it invited has happened.

        A phase that had nothing to show offered to start it instead. When
        that run finishes there is something to show, and leaving the
        invitation up would say a cull had not happened while its report sat
        on disk.
        """
        for item in plan:
            page = self._pages.get(item["id"])
            if not isinstance(page, Invitation) or item["state"] != "done":
                continue
            del self._pages[item["id"]]
            self.pages.removeWidget(page)
            page.deleteLater()
            if self.current == item["id"]:
                self.current = ""
                self.open_phase(item["id"])

    def report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setVisible(bool(message))

    def phase(self, key: str) -> dict[str, Any]:
        return next(
            (item for item in getattr(self, "plan", []) if item["id"] == key),
            {})

    def open_phase(self, key: str) -> bool:
        """Show one phase, building its page the first time it is asked for."""
        if self.phase(key).get("state") == "blocked":
            self.report(self.phase(key)["reason"], "alarm")
            return False
        page = self._pages.get(key)
        if page is None:
            page = self._build_page(key)
            if page is None:
                return False
            self._adopt(page)
            self._pages[key] = page
            self.pages.addWidget(page)
        self.pages.setCurrentWidget(page)
        self.current = key
        self.bar.set_current(key)
        self.bar.set_indicator(getattr(page, "indicator", None))
        self.report("")
        self.opened.emit(key)
        page.setFocus()
        return True

    @staticmethod
    def _adopt(page: QWidget) -> None:
        """Take over the page's chrome, and leave it its own counter.

        A page whose bar carried verbs is asked to surface them on its
        body first: hiding the bar must never hide the buttons.
        """
        surface = getattr(page, "surface_actions", None)
        if callable(surface):
            surface()
        bar = getattr(page, "bar", None)
        if bar is not None:
            bar.hide()

    def page_for(self, key: str) -> QWidget | None:
        return self._pages.get(key)

    def drop(self, key: str) -> None:
        """Forget a built page, so the next opening builds it afresh.

        Used when what a page was built from has changed underneath it --
        an invitation whose run has now happened is not worth keeping.
        """
        page = self._pages.pop(key, None)
        if page is None:
            return
        if hasattr(page, "shutdown"):
            page.shutdown()
        self.pages.removeWidget(page)
        page.deleteLater()
        if self.current == key:
            self.current = ""

    def rebuild(self, key: str) -> None:
        """Drop a built page and, if it was the one showing, open it afresh.

        Dropping the current page clears `current`, so a caller that
        checks afterwards finds nothing showing and leaves the shell
        blank. Asking for the rebuild as one act removes the ordering
        from the caller's hands.
        """
        showing = self.current == key
        self.drop(key)
        if showing:
            self.open_phase(key)

    def shutdown(self) -> None:
        for page in self._pages.values():
            if hasattr(page, "shutdown"):
                page.shutdown()


def first_open(plan: list[dict[str, Any]]) -> str:
    """Which phase to land on when a folder is opened with no phase named.

    The earliest phase with work left in it, so a fresh folder opens on the
    cull and a culled one opens on the assessment. A folder with nothing
    outstanding opens on its last finished phase rather than on nothing.

    The profile is skipped: it is always enterable and belongs to the
    photographer, so landing on it would mean every folder opened on a page
    that is not about that folder.
    """
    ready = [item for item in plan
             if item["state"] in {"ready", "running"}
             and item["id"] != phases.PROFILE]
    if ready:
        return ready[0]["id"]
    done = [item for item in plan if item["state"] == "done"]
    return done[-1]["id"] if done else phases.CULL
