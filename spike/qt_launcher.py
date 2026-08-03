#!/usr/bin/env python3
"""Spike: the Darkimiya launcher as a native Qt window.

The point of this spike is not the pixels. It is that there is no HTTP
server, no port, no session token, no polling loop and no web view between
the interface and the application: the widgets call ProjectCatalog and
JobManager directly, and the file chooser is the platform's own.

Run:
    .venv/bin/python spike/qt_launcher.py [--state DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.jobs import JobManager  # noqa: E402
from opencull_gui.macos import MacOSPaths  # noqa: E402
from opencull_gui.project_catalog import ProjectCatalog  # noqa: E402
from opencull_gui.providers import ProviderStore  # noqa: E402

# The same darkroom palette as the web launcher: a warm near-black wall,
# warm paper white, and safelight amber meaning "work in flight".
INK = "#131211"
SURFACE = "#1B1918"
RAISED = "#232120"
EDGE = "#302C29"
EDGE_SOFT = "#262322"
PAPER = "#EDE7DE"
MUTED = "#98908A"
FAINT = "#6B645F"
SAFELIGHT = "#FF9B47"
FIXED = "#86CFA6"
ALARM = "#E8735A"

DISPLAY_FAMILIES = [
    "Ubuntu Condensed", "SF Compact Display", "Roboto Condensed",
    "Inter Tight", "DejaVu Sans Condensed",
]
MONO_FAMILIES = ["Ubuntu Mono", "JetBrains Mono", "DejaVu Sans Mono", "monospace"]

STYLESHEET = f"""
QMainWindow, QWidget#page {{ background: {INK}; }}
QWidget {{ color: {PAPER}; }}

QFrame#chrome {{
    background: {SURFACE};
    border-bottom: 1px solid {EDGE_SOFT};
}}
QLabel#chromeTitle {{ letter-spacing: 2px; }}
QLabel#eyebrow {{ color: {FAINT}; letter-spacing: 3px; }}
QLabel#heroCount {{ color: {PAPER}; }}
QLabel#heroSub {{ color: {MUTED}; }}
QLabel#bandTitle {{ color: {FAINT}; letter-spacing: 3px; }}
QLabel#rowName {{ font-weight: 600; }}
QLabel#rowPath {{ color: {FAINT}; }}
QLabel#rowState {{ color: {FAINT}; letter-spacing: 1px; }}
QLabel#rowState[tone="running"] {{ color: {SAFELIGHT}; }}
QLabel#rowState[tone="ready"] {{ color: {FIXED}; }}
QLabel#rowState[tone="failed"] {{ color: {ALARM}; }}

QPushButton#primary {{
    background: {SAFELIGHT};
    color: #241203;
    border: 1px solid #FFAE68;
    border-radius: 8px;
    padding: 9px 18px;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: #FFAC63; }}
QPushButton#primary:pressed {{ background: #F08D3B; }}

QPushButton#ghost {{
    background: transparent;
    border: 1px solid {EDGE};
    border-radius: 8px;
    padding: 8px 16px;
}}
QPushButton#ghost:hover {{ background: {RAISED}; border-color: #3B3633; }}
QPushButton#ghost:pressed {{ background: {EDGE_SOFT}; }}

QFrame#rows {{
    background: {SURFACE};
    border: 1px solid {EDGE_SOFT};
    border-radius: 10px;
}}
QFrame#row {{ background: transparent; border-bottom: 1px solid {EDGE_SOFT}; }}
QFrame#row:hover {{ background: {RAISED}; }}
QFrame#rowLast {{ background: transparent; border: none; }}

QFrame#empty {{
    background: {SURFACE};
    border: 1px dashed {EDGE};
    border-radius: 10px;
}}

QScrollArea {{ border: none; background: {INK}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {EDGE}; border-radius: 5px; min-height: 40px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


class ElidedLabel(QLabel):
    """A label that shortens its text rather than widening its row."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full = text
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        metrics = self.fontMetrics()
        elided = metrics.elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, self.width())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect(), int(self.alignment()), elided)
        painter.end()


def display_font(size: int, weight: QFont.Weight = QFont.Weight.Bold) -> QFont:
    font = QFont()
    font.setFamilies(DISPLAY_FAMILIES)
    font.setPointSize(size)
    font.setWeight(weight)
    return font


def mono_font(size: int) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setPointSize(size)
    return font


class SprocketEdge(QWidget):
    """Film perforations down the leading edge of a row.

    Painted rather than styled, so the same widget can advance them while a
    cull runs -- film moving through the machine.
    """

    PITCH = 16
    HOLE = 5
    WIDTH = 3

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedWidth(13)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._offset = 0.0
        self._running = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def set_running(self, running: bool) -> None:
        if running == self._running:
            return
        self._running = running
        if running:
            self._timer.start(60)
        else:
            self._timer.stop()
            self._offset = 0.0
        self.update()

    def _advance(self) -> None:
        self._offset = (self._offset + 1.0) % self.PITCH
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(SAFELIGHT) if self._running else QColor(PAPER)
        colour.setAlphaF(0.85 if self._running else 0.16)
        painter.setBrush(colour)
        painter.setPen(Qt.PenStyle.NoPen)
        x = (self.width() - self.WIDTH) / 2
        y = -self.PITCH + self._offset
        while y < self.height() + self.PITCH:
            path = QPainterPath()
            path.addRoundedRect(x, y, self.WIDTH, self.HOLE, 1.2, 1.2)
            painter.fillPath(path, colour)
            y += self.PITCH
        painter.end()


class Row(QFrame):
    """One folder or job."""

    def __init__(
        self, name: str, path: str, state_label: str, tone: str,
        actions: list[tuple[str, object]], last: bool = False,
    ):
        super().__init__()
        self.setObjectName("rowLast" if last else "row")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 14, 0)
        layout.setSpacing(14)

        self.sprocket = SprocketEdge()
        self.sprocket.set_running(tone == "running")
        layout.addWidget(self.sprocket)

        column = QVBoxLayout()
        column.setContentsMargins(4, 12, 0, 12)
        column.setSpacing(2)
        title = QLabel(name)
        title.setObjectName("rowName")
        location = ElidedLabel(path)
        location.setObjectName("rowPath")
        location.setFont(mono_font(8))
        location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(title)
        column.addWidget(location)
        layout.addLayout(column, 1)

        badge = QLabel(state_label.upper())
        badge.setObjectName("rowState")
        badge.setProperty("tone", tone)
        badge.setFont(display_font(8))
        layout.addWidget(badge)

        for label, handler in actions:
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            layout.addWidget(button)


class Launcher(QMainWindow):
    refreshed = Signal()

    def __init__(self, paths: MacOSPaths):
        super().__init__()
        self.paths = paths
        self.catalog = ProjectCatalog(paths.support / "projects.json")
        self.providers = ProviderStore(
            paths.providers, Path(__file__).resolve().parent.parent,
            generated_root=paths.generated / "providers")
        self.jobs = JobManager(paths.jobs, Path(__file__).resolve().parent.parent,
                               providers=self.providers)

        self.setWindowTitle("Darkimiya")
        self.resize(1040, 720)
        self.setMinimumSize(760, 540)
        self.setStyleSheet(STYLESHEET)
        self._build()
        self.refresh()

        # No polling loop over HTTP: this reads local state directly, and only
        # while a job is actually running.
        self._tick = QTimer(self)
        self._tick.timeout.connect(self.refresh)
        self._tick.start(1500)

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("page")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._chrome())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("page")
        self.page = QVBoxLayout(body)
        self.page.setContentsMargins(28, 0, 28, 36)
        self.page.setSpacing(0)
        self.page.addWidget(self._hero())
        self.page.addWidget(self._band("Folders", "library"))
        self.page.addWidget(self._band("In progress", "queue"))
        self.page.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.setCentralWidget(central)

    def _chrome(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("chrome")
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(28, 0, 22, 0)
        layout.setSpacing(10)

        mark = QLabel()
        mark.setFixedSize(9, 9)
        mark.setStyleSheet(f"background: {SAFELIGHT}; border-radius: 2px;")
        layout.addWidget(mark)

        title = QLabel("DARKIMIYA")
        title.setObjectName("chromeTitle")
        title.setFont(display_font(11))
        layout.addWidget(title)
        layout.addStretch(1)

        providers = QPushButton("Providers")
        providers.setObjectName("ghost")
        providers.setCursor(Qt.CursorShape.PointingHandCursor)
        providers.clicked.connect(self.show_providers)
        layout.addWidget(providers)
        return bar

    def _hero(self) -> QWidget:
        hero = QWidget()
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(0, 36, 0, 30)
        layout.setSpacing(0)

        eyebrow = QLabel("LIBRARY")
        eyebrow.setObjectName("eyebrow")
        eyebrow.setFont(display_font(8))
        layout.addWidget(eyebrow)

        self.hero_count = QLabel("—")
        self.hero_count.setObjectName("heroCount")
        self.hero_count.setFont(display_font(46))
        layout.addSpacing(6)
        layout.addWidget(self.hero_count)

        self.hero_sub = QLabel("Reading the library…")
        self.hero_sub.setObjectName("heroSub")
        self.hero_sub.setWordWrap(True)
        self.hero_sub.setMaximumWidth(560)
        layout.addWidget(self.hero_sub)
        layout.addSpacing(22)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        cull = QPushButton("Cull a folder")
        cull.setObjectName("primary")
        cull.setCursor(Qt.CursorShape.PointingHandCursor)
        cull.clicked.connect(self.choose_folder)
        actions.addWidget(cull)
        actions.addStretch(1)
        layout.addLayout(actions)
        return hero

    def _band(self, title: str, key: str) -> QWidget:
        band = QWidget()
        layout = QVBoxLayout(band)
        layout.setContentsMargins(0, 22, 0, 0)
        layout.setSpacing(10)

        heading = QLabel(title.upper())
        heading.setObjectName("bandTitle")
        heading.setFont(display_font(8))
        layout.addWidget(heading)

        rows = QFrame()
        rows.setObjectName("rows")
        inner = QVBoxLayout(rows)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)
        layout.addWidget(rows)

        setattr(self, f"{key}_band", band)
        setattr(self, f"{key}_rows", inner)
        return band

    # --- state ----------------------------------------------------------

    def refresh(self) -> None:
        queue = self.jobs.public()
        catalog = self.catalog.public(queue)
        projects = catalog["projects"]
        running = [
            job for job in queue["jobs"]
            if job.get("status") in {"running", "queued"}
        ]

        empty = not projects
        self.hero_count.setVisible(not empty)
        self.library_band.setVisible(not empty)
        if empty:
            self.hero_sub.setText(
                "Start with a folder. Darkimiya groups the near-duplicate "
                "frames and proposes keepers. Nothing is moved or deleted.")
        else:
            reviewed = sum(1 for p in projects if p.get("report_available"))
            self.hero_count.setText(str(len(projects)))
            parts = ["folder" if len(projects) == 1 else "folders"]
            if running:
                parts.append(f"{len(running)} culling now")
            elif reviewed:
                parts.append(f"{reviewed} reviewed")
            self.hero_sub.setText(" · ".join(parts))

        self._fill(self.library_rows, [
            self._project_row(project, index == len(projects) - 1)
            for index, project in enumerate(projects)
        ])
        self.queue_band.setVisible(bool(running))
        self._fill(self.queue_rows, [
            Row(job.get("name") or job.get("kind", "Job"),
                str(job.get("photos", "")), "Culling", "running",
                [("Cancel", lambda _=False, j=job: self.cancel(j))],
                last=index == len(running) - 1)
            for index, job in enumerate(running)
        ])

    def _project_row(self, project: dict, last: bool) -> Row:
        cull = project.get("culling") or {}
        status = str(cull.get("status", ""))
        if status in {"running", "queued"}:
            label, tone = ("Culling" if status == "running" else "Queued"), "running"
        elif project.get("report_available"):
            label, tone = "Reviewed", "ready"
        elif not project.get("available"):
            label, tone = "Folder offline", "failed"
        else:
            label, tone = "Not culled", ""
        actions: list[tuple[str, object]] = []
        if project.get("report_available"):
            actions.append(("Open review", lambda _=False: self.not_yet("Review")))
        if tone != "running" and project.get("available"):
            actions.append(("Cull", lambda _=False: self.not_yet("Culling")))
        return Row(
            str(project.get("name", "")), self._short(str(project.get("photos", ""))),
            label, tone, actions, last=last)

    @staticmethod
    def _short(path: str) -> str:
        home = str(Path.home())
        return f"~{path[len(home):]}" if path.startswith(home) else path

    @staticmethod
    def _fill(layout: QVBoxLayout, widgets: list[QWidget]) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for widget in widgets:
            layout.addWidget(widget)

    # --- actions --------------------------------------------------------

    def choose_folder(self) -> None:
        # The platform's own chooser. No zenity, kdialog, osascript or Tk.
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder of photographs to cull", str(Path.home()))
        if not folder:
            return
        try:
            self.catalog.add(folder)
        except Exception as exc:
            QMessageBox.warning(self, "Darkimiya", str(exc))
        self.refresh()

    def cancel(self, job: dict) -> None:
        try:
            self.jobs.action(str(job.get("id", "")), "cancel")
        except Exception as exc:
            QMessageBox.warning(self, "Darkimiya", str(exc))
        self.refresh()

    def show_providers(self) -> None:
        state = self.providers.public()
        storage = state.get("credential_storage", {})
        profiles = state.get("profiles", [])
        stored = profiles and profiles[0].get("credential") == "stored"
        QMessageBox.information(
            self, "Providers",
            (f"{len(profiles)} profile(s).\n"
             f"Credential: {'stored' if stored else 'not set'}\n"
             f"Kept in {storage.get('label', 'unknown')}."))

    def not_yet(self, what: str) -> None:
        QMessageBox.information(
            self, "Darkimiya",
            f"{what} is wired to the same domain layer; this spike only "
            "demonstrates the shell.")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.jobs.shutdown()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, help="alternate application state root")
    args = parser.parse_args(argv)

    application = QApplication(sys.argv[:1])
    application.setApplicationName("Darkimiya")
    application.setApplicationDisplayName("Darkimiya")
    paths = MacOSPaths.create(args.state.resolve() if args.state else None)
    window = Launcher(paths)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
