"""The Darkimiya launcher: library, queue and providers, as a native window.

There is no HTTP server, port, session token or web view between this window
and the application. The widgets call ProjectCatalog and JobManager directly,
and the folder chooser is Qt's own.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.photos import PhotoStore
from opencull_gui.project_catalog import ProjectCatalogError
from opencull_gui.report import load_report
from opencull_gui.reviews import ReviewStore, default_review_path
from scan import classify_folder

from . import theme
from .previews import LibraryPreviewLoader, PreviewLoader
from .providers import ProvidersDialog
from .review import ReviewPage
from .widgets import ProjectCard, Row, band, replace_rows

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
        return "Culled", "ready"
    if not project.get("available"):
        return "Folder offline", "failed"
    return "Not culled", ""


def treatment_label(kind: str) -> str:
    """Name the treatment a folder's contents can receive.

    RAW files are developed. Rendered bitmaps can only be edited. A folder
    holding both can do either, so it offers both.
    """
    return {
        "raw": "Develop",
        "bitmap": "Edit",
        "mixed": "Develop / Edit",
    }.get(kind, "")


RECULL_WARNING = (
    "Culling {name} again discards the current selection and asks the models "
    "to choose from scratch. It costs another full run.\n\n"
    "Your own keeper and reject marks are kept, but the AI recommendations "
    "they sit beside will change.\n\n"
    "Only do this if the previous cull was wrong in a large way. To disagree "
    "with a few frames, open the review and change them there.")


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
        self._contents: dict[str, dict] = {}
        self._loader: PreviewLoader | None = None
        self._cards: dict[str, ProjectCard] = {}
        self._library_previews = LibraryPreviewLoader(services.paths.cache, self)
        self._library_previews.ready.connect(self._library_painted)
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

        self.library_band, self.library_grid = self._card_band("Folders")
        self.queue_band, self.queue_rows = band("In progress")
        column.addWidget(self.library_band)
        column.addWidget(self.queue_band)
        column.addStretch(1)
        scroll.setWidget(page)
        outer.addWidget(scroll, 1)

        # One window, two pages: the library, and the review of one folder.
        self.pages = QStackedWidget()
        self.pages.addWidget(central)
        self.setCentralWidget(self.pages)
        self.projects_page = central
        self.review_page: ReviewPage | None = None

    @staticmethod
    def _card_band(title: str) -> tuple[QWidget, QGridLayout]:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 22, 0, 0)
        layout.setSpacing(12)
        heading = QLabel(title.upper())
        heading.setObjectName("bandTitle")
        heading.setFont(theme.display(8))
        layout.addWidget(heading)
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(holder)
        return section, grid

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
        self.cull_button = QPushButton("Open a folder")
        self.cull_button.setObjectName("primary")
        self.cull_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cull_button.setFont(theme.body(10))
        self.cull_button.clicked.connect(self.open_folder)
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
        if self.pages.currentWidget() is not self.projects_page:
            return
        queue = self.services.jobs.public()
        projects = self.services.projects.public(queue)["projects"]
        active = [job for job in queue["jobs"] if job.get("status") in ACTIVE]

        empty = not projects
        self.count.setVisible(not empty)
        self.lead.setVisible(empty)
        self.library_band.setVisible(not empty)
        if empty:
            self.summary.setText(
                "Open a folder of photographs. Darkimiya can cull it, "
                "grouping the near-duplicate frames and proposing keepers, or "
                "develop it directly. Nothing is moved or deleted.")
        else:
            self.count.setText(f"{len(projects):,}")
            reviewed = sum(1 for item in projects if item.get("report_available"))
            parts = ["folder" if len(projects) == 1 else "folders"]
            if active:
                parts.append(f"{len(active)} culling now")
            elif reviewed:
                parts.append(f"{reviewed} reviewed")
            self.summary.setText(" · ".join(parts))

        self._fill_library(projects)
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

    def folder_contents(self, project: dict) -> dict:
        """Classify a folder's photographs, walking it at most once."""
        photos = str(project.get("photos", ""))
        if photos not in self._contents:
            if not project.get("available"):
                self._contents[photos] = {"kind": "empty", "total": 0}
            else:
                try:
                    self._contents[photos] = classify_folder(Path(photos))
                except OSError:
                    self._contents[photos] = {"kind": "empty", "total": 0}
        return self._contents[photos]

    def _fill_library(self, projects: list[dict]) -> None:
        while self.library_grid.count():
            item = self.library_grid.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self._cards.clear()
        # Three across matches the window's default width; the grid rewraps
        # rather than scrolling sideways when it is narrower.
        columns = max(1, (self.width() - 56 + 16) // (316 + 16))
        for index, project in enumerate(projects):
            card = self._folder_card(project)
            self._cards[str(project.get("photos", ""))] = card
            self.library_grid.addWidget(card, index // columns, index % columns)

    def _folder_card(self, project: dict) -> ProjectCard:
        label, tone = project_state(project)
        culled = bool(project.get("report_available"))
        running = tone == "running"
        actions: list[tuple[str, object]] = []
        if culled:
            actions.append(("Review", lambda p=project: self.open_review(p)))
        contents = self.folder_contents(project)
        treatment = treatment_label(contents["kind"])
        if treatment and not running and project.get("available"):
            actions.append((treatment, lambda p=project: self.develop(p)))
        if not running and project.get("available"):
            actions.append((
                "Re-cull" if culled else "Cull",
                lambda p=project, again=culled: self.cull_project(p, again)))

        total = int(contents.get("total") or 0)
        subtitle = (
            f"{total:,} photograph{'' if total == 1 else 's'}" if total
            else "no readable photographs")
        card = ProjectCard(
            str(project.get("name", "")),
            short_path(str(project.get("photos", ""))),
            subtitle, label, tone, actions,
            progress=job_progress(project["culling"]) if running else None)

        root = str(project.get("photos", ""))
        for position, name in enumerate(contents.get("samples") or []):
            pixmap = self._library_previews.request_in(root, name)
            if pixmap is not None:
                card.strip.set_frame(position, pixmap)
        return card

    def _library_painted(self, key: str, size: str, pixmap) -> None:
        root, _, name = key.partition("\x1f")
        card = self._cards.get(root)
        if card is None:
            return
        samples = (self._contents.get(root) or {}).get("samples") or []
        if name in samples:
            card.strip.set_frame(samples.index(name), pixmap)

    def _folder_row(self, project: dict, last: bool) -> Row:
        label, tone = project_state(project)
        culled = bool(project.get("report_available"))
        running = tone == "running"
        actions: list[tuple[str, object]] = []

        if culled:
            actions.append(
                ("Open review", lambda p=project: self.open_review(p)))

        # What a folder holds decides what can be done to it.
        treatment = treatment_label(self.folder_contents(project)["kind"])
        if treatment and not running and project.get("available"):
            actions.append(
                (treatment, lambda p=project: self.develop(p)))

        if not running and project.get("available"):
            actions.append((
                "Re-cull" if culled else "Cull",
                lambda p=project, again=culled: self.cull_project(p, again)))

        return Row(
            str(project.get("name", "")),
            short_path(str(project.get("photos", ""))),
            label, tone, actions,
            progress=job_progress(project["culling"]) if running else None,
            last=last)

    # --- actions --------------------------------------------------------

    def open_folder(self) -> None:
        """Add a folder to the library. Choosing what to do with it comes next."""
        folder = QFileDialog.getExistingDirectory(
            self, "Open a folder of photographs", str(Path.home()))
        if not folder:
            return
        try:
            self.services.projects.add(folder)
        except (ProjectCatalogError, ValueError) as exc:
            self.report(str(exc), "alarm")
            self.refresh()
            return
        try:
            contents = classify_folder(Path(folder))
        except OSError:
            contents = {"kind": "empty", "total": 0}
        self._contents[folder] = contents
        name = Path(folder).name
        if contents["total"] == 0:
            self.report(
                f"{name} holds no photographs Darkimiya can read.", "alarm")
        else:
            treatment = treatment_label(contents["kind"])
            self.report(
                f"Added {name}: {contents['total']} photographs. "
                f"Cull it, or go straight to {treatment.lower()}.")
        self.refresh()

    def cull_project(self, project: dict, again: bool = False) -> None:
        name = str(project.get("name") or Path(str(project.get("photos", ""))).name)
        if again and not self.confirm_recull(name):
            return
        photos = str(project.get("photos", ""))
        try:
            self.services.jobs.add(photos, "", 2, True, "family", "", None, 5, 4)
        except Exception as exc:
            # A missing provider credential surfaces here, and is the most
            # common reason a cull cannot start, so say so plainly.
            self.report(str(exc), "alarm")
            self.refresh()
            return
        self.report(
            f"Culling {name}. It will be marked Culled when the run finishes.")
        self.refresh()

    def confirm_recull(self, name: str) -> bool:
        """Ask before discarding a selection that already exists."""
        box = QMessageBox(self)
        box.setWindowTitle("Cull again?")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"{name} has already been culled.")
        box.setInformativeText(RECULL_WARNING.format(name=name))
        again = box.addButton("Cull again", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep the current cull", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is again

    def develop(self, project: dict) -> None:
        """Open a folder's frames for development, in this window.

        Development works from a selection. A folder that has been culled
        already has one; a folder that has not gets a deterministic
        everything-included selection, so a treatment does not require paying
        for a cull first.
        """
        report_path = Path(str(project.get("report", "")))
        try:
            if not report_path.is_file():
                report_path = self.services.projects.manual_selection_report(
                    str(project.get("id", "")))
        except (ProjectCatalogError, ValueError, OSError) as exc:
            self.report(str(exc), "alarm")
            return
        self._open_workspace(project, report_path, intent="develop")

    def open_review(self, project: dict) -> None:
        """Show the folder's culling decisions, in this window."""
        report_path = Path(str(project.get("report", "")))
        if not report_path.is_file():
            self.report("That report is no longer on disk.", "alarm")
            return
        self._open_workspace(project, report_path, intent="review")

    def _open_workspace(
        self, project: dict, report_path: Path, intent: str,
    ) -> None:
        """Both Review and Develop land on the same page of the same window."""
        photos_path = Path(str(project.get("photos", "")))
        try:
            report = load_report(report_path)
            photos = PhotoStore(
                photos_path, self.services.paths.cache / "previews")
            reviews = ReviewStore(
                default_review_path(report_path), report, photos.root)
        except Exception as exc:
            name = project.get("name", "That folder")
            self.report(f"{name} could not be opened: {exc}", "alarm")
            return
        self.show_review(report, photos, reviews, intent=intent)

    def show_review(
        self, report, photos, reviews, intent: str = "review",
    ) -> None:
        self._close_review()
        self._loader = PreviewLoader(photos, self)
        page = ReviewPage(report, reviews, self._loader, intent=intent)
        page.closed.connect(self.show_projects)
        self.review_page = page
        self.pages.addWidget(page)
        self.pages.setCurrentWidget(page)
        page.setFocus()

    def show_projects(self) -> None:
        self.pages.setCurrentWidget(self.projects_page)
        self._close_review()
        self.refresh()

    def _close_review(self) -> None:
        loader = getattr(self, "_loader", None)
        if loader is not None:
            loader.shutdown()
            self._loader = None
        if self.review_page is not None:
            self.pages.removeWidget(self.review_page)
            self.review_page.deleteLater()
            self.review_page = None

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
        self._close_review()
        self._library_previews.shutdown()
        self.services.close()
        super().closeEvent(event)


def show_message(parent: QWidget | None, title: str, message: str) -> None:
    QMessageBox.information(parent, title, message)
