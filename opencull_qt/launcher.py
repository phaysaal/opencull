"""The Darkimiya launcher: library, queue and providers, as a native window.

There is no HTTP server, port, session token or web view between this window
and the application. The widgets call ProjectCatalog and JobManager directly,
and the folder chooser is Qt's own.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.project_catalog import ProjectCatalogError

from . import theme
from .providers import ProvidersDialog
from .widgets import Row, band, replace_rows

ACTIVE = {"running", "queued"}


def job_progress(job: dict) -> int | None:
    """Read a percentage out of a worker's most recent progress line."""
    import re

    matches = re.findall(r"(\d{1,3})\s*%", str(job.get("log_tail") or ""))
    if matches:
        return max(0, min(100, int(matches[-1])))
    return 0 if job.get("status") == "running" else None


def project_state(project: dict) -> tuple[str, str]:
    """Return the label and tone for one folder."""
    status = str((project.get("culling") or {}).get("status", ""))
    if status == "running":
        return "Culling", "running"
    if status == "queued":
        return "Queued", "running"
    if status == "failed":
        return "Failed", "failed"
    if project.get("report_available"):
        return "Reviewed", "ready"
    if not project.get("available"):
        return "Folder offline", "failed"
    return "Not culled", ""


def short_path(value: str) -> str:
    home = str(Path.home())
    return f"~{value[len(home):]}" if value.startswith(home) else value


def place_on_active_screen(window: QWidget, prefer: str = "") -> None:
    """Centre the window on the primary screen.

    Left to the window manager, a new window can land on whichever monitor
    the layout puts first. On a desktop with a second display that is off,
    asleep, or simply not the one being watched, that reads as the
    application failing to open: an icon appears in the dock and nothing
    else does.

    The primary screen is chosen deliberately over the one under the
    pointer. The pointer can be resting on a monitor that is powered down,
    whereas the primary screen is the one carrying the panel and dock -- the
    screen a person is looking at when they start an application.
    """
    screen = None
    if prefer:
        screen = next(
            (item for item in QGuiApplication.screens()
             if item.name() == prefer), None)
    screen = screen or QGuiApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    size = window.frameGeometry().size().boundedTo(available.size())
    window.resize(size)
    frame = window.frameGeometry()
    frame.moveCenter(available.center())
    window.move(frame.topLeft())


class Launcher(QMainWindow):
    def __init__(self, services, poll_interval: int = 1500, screen: str = ""):
        super().__init__()
        self.services = services
        self.preferred_screen = screen
        self.setWindowTitle("Darkimiya")
        self.resize(1040, 720)
        self.setMinimumSize(760, 540)
        self._placed = False
        self.setStyleSheet(theme.STYLESHEET)
        icon = Path(__file__).resolve().parent.parent / "assets" / "opencull-icon.svg"
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self._build()
        self.refresh()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(poll_interval)

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
        page = QWidget()
        page.setObjectName("page")
        column = QVBoxLayout(page)
        column.setContentsMargins(28, 0, 28, 36)
        column.setSpacing(0)
        column.addWidget(self._hero())

        self.library_band, self.library_rows = band("Folders")
        self.queue_band, self.queue_rows = band("In progress")
        column.addWidget(self.library_band)
        column.addWidget(self.queue_band)
        column.addStretch(1)
        scroll.setWidget(page)
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
        mark.setStyleSheet(f"background: {theme.SAFELIGHT}; border-radius: 2px;")
        layout.addWidget(mark)

        title = QLabel("DARKIMIYA")
        title.setObjectName("chromeTitle")
        title.setFont(theme.display(11))
        layout.addWidget(title)
        layout.addStretch(1)

        self.providers_button = QPushButton("Providers")
        self.providers_button.setObjectName("ghost")
        self.providers_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.providers_button.setFont(theme.body(10))
        self.providers_button.clicked.connect(self.edit_providers)
        layout.addWidget(self.providers_button)
        return bar

    def _hero(self) -> QWidget:
        hero = QWidget()
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(0, 36, 0, 28)
        layout.setSpacing(0)

        eyebrow = QLabel("LIBRARY")
        eyebrow.setObjectName("eyebrow")
        eyebrow.setFont(theme.display(8))
        layout.addWidget(eyebrow)
        layout.addSpacing(6)

        self.count = QLabel("—")
        self.count.setObjectName("heroCount")
        self.count.setFont(theme.display(46))
        layout.addWidget(self.count)

        self.lead = QLabel("Start with a folder.")
        self.lead.setObjectName("heroCount")
        self.lead.setFont(theme.display(30))
        self.lead.hide()
        layout.addWidget(self.lead)

        self.summary = QLabel("Reading the library…")
        self.summary.setObjectName("heroSub")
        self.summary.setWordWrap(True)
        self.summary.setMaximumWidth(560)
        self.summary.setFont(theme.body(10))
        layout.addWidget(self.summary)
        layout.addSpacing(20)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.cull_button = QPushButton("Cull a folder")
        self.cull_button.setObjectName("primary")
        self.cull_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cull_button.setFont(theme.body(10))
        self.cull_button.clicked.connect(self.cull_folder)
        actions.addWidget(self.cull_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.notice = QFrame()
        self.notice.setObjectName("notice")
        notice_layout = QHBoxLayout(self.notice)
        notice_layout.setContentsMargins(12, 9, 12, 9)
        self.notice_text = QLabel("")
        self.notice_text.setWordWrap(True)
        self.notice_text.setFont(theme.body(9))
        notice_layout.addWidget(self.notice_text)
        self.notice.hide()
        layout.addSpacing(14)
        layout.addWidget(self.notice)
        return hero

    # --- state ----------------------------------------------------------

    def report(self, message: str, tone: str = "") -> None:
        if not message:
            self.notice.hide()
            return
        self.notice_text.setText(message)
        self.notice.setProperty("tone", tone)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)
        self.notice.show()

    def refresh(self) -> None:
        queue = self.services.jobs.public()
        projects = self.services.projects.public(queue)["projects"]
        active = [job for job in queue["jobs"] if job.get("status") in ACTIVE]

        empty = not projects
        self.count.setVisible(not empty)
        self.lead.setVisible(empty)
        self.library_band.setVisible(not empty)
        if empty:
            self.summary.setText(
                "Point Darkimiya at a shoot. It groups the near-duplicate "
                "frames and proposes keepers. Nothing is moved or deleted.")
        else:
            self.count.setText(f"{len(projects):,}")
            reviewed = sum(1 for item in projects if item.get("report_available"))
            parts = ["folder" if len(projects) == 1 else "folders"]
            if active:
                parts.append(f"{len(active)} culling now")
            elif reviewed:
                parts.append(f"{reviewed} reviewed")
            self.summary.setText(" · ".join(parts))

        replace_rows(self.library_rows, [
            self._folder_row(project, index == len(projects) - 1)
            for index, project in enumerate(projects)
        ])
        self.queue_band.setVisible(bool(active))
        replace_rows(self.queue_rows, [
            Row(str(job.get("name") or job.get("kind") or "Job"),
                short_path(str(job.get("photos", ""))),
                "Culling" if job.get("status") == "running" else "Queued",
                "running",
                [("Cancel", lambda job=job: self.cancel(job))],
                progress=job_progress(job),
                last=index == len(active) - 1)
            for index, job in enumerate(active)
        ])

    def _folder_row(self, project: dict, last: bool) -> Row:
        label, tone = project_state(project)
        actions: list[tuple[str, object]] = []
        if project.get("report_available"):
            actions.append(
                ("Open review", lambda p=project: self.open_review(p)))
        if tone != "running" and project.get("available"):
            actions.append((
                "Cull again" if project.get("report_available") else "Cull",
                lambda p=project: self.cull_project(p)))
        return Row(
            str(project.get("name", "")),
            short_path(str(project.get("photos", ""))),
            label, tone, actions,
            progress=job_progress(project["culling"]) if tone == "running" else None,
            last=last)

    # --- actions --------------------------------------------------------

    def cull_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder of photographs to cull", str(Path.home()))
        if not folder:
            return
        try:
            record = self.services.projects.add(folder)
            self.report("")
        except (ProjectCatalogError, ValueError) as exc:
            self.report(str(exc), "alarm")
            self.refresh()
            return
        project = (record.get("projects") or [{}])[-1] if isinstance(
            record, dict) else {}
        self.cull_project(project or {"photos": folder})

    def cull_project(self, project: dict) -> None:
        photos = str(project.get("photos", ""))
        try:
            self.services.jobs.add(photos, "", 2, True, "family", "", None, 5, 4)
            self.report(f"Culling {Path(photos).name}.")
        except Exception as exc:
            # A missing provider credential surfaces here; it is the most
            # common reason a cull cannot start, so say so plainly.
            self.report(f"{exc}", "alarm")
        self.refresh()

    def open_review(self, project: dict) -> None:
        report = Path(str(project.get("report", "")))
        photos = Path(str(project.get("photos", "")))
        if not report.is_file():
            self.report("That report is no longer on disk.", "alarm")
            return
        try:
            opened = self.services.open_review(report, photos)
        except Exception as exc:
            self.report(f"The review could not be opened: {exc}", "alarm")
            return
        # The review workspace is still the web interface; the launcher hands
        # it over rather than duplicating it.
        webbrowser.open(str(opened["url"]))
        self.report(f"Opened {project.get('name', 'the review')} in your browser.")

    def cancel(self, job: dict) -> None:
        try:
            self.services.jobs.action(str(job.get("id", "")), "cancel")
            self.report("")
        except Exception as exc:
            self.report(str(exc), "alarm")
        self.refresh()

    def edit_providers(self) -> None:
        dialog = ProvidersDialog(self.services.providers, self)
        dialog.exec()
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._placed:
            self._placed = True
            place_on_active_screen(self, self.preferred_screen)
            # A window the manager put behind others is as invisible as one on
            # a monitor that is off.
            self.raise_()
            self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        self.services.close()
        super().closeEvent(event)


def show_message(parent: QWidget | None, title: str, message: str) -> None:
    QMessageBox.information(parent, title, message)
