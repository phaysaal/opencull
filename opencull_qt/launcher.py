"""The Darkimiya launcher: library, queue and providers, as a native window.

There is no HTTP server, port, session token or web view between this window
and the application. The widgets call ProjectCatalog and JobManager directly,
and the folder chooser is Qt's own.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QDialog,
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

from opencull_gui import phases
from opencull_gui.debrief import aggregate as debrief_aggregate
from opencull_gui.project import (
    load_or_create_folder_project,
    load_project,
    update_project,
)
from opencull_gui.project_catalog import ProjectCatalogError
from opencull_gui.provenance import frame_story
from opencull_gui.reviews import (
    ReviewError,
    default_review_path,
    narrow_selection,
)
from opencull_gui.shortlist import (
    tier_stars,
    unfinished_assessments,
    write_manual_shortlist,
)
from opencull_gui.style import StyleProfileStore
from scan import classify_folder

from . import theme
from .bench import Bench
from .criteria import CriteriaDialog
from .debrief import DebriefPage
from .develop import DevelopPage
from .export import ExportPage
from .finetune import FineTunePage
from .previews import LibraryPreviewLoader, PreviewLoader
from .provenance import ProvenanceDialog
from .providers import ProvidersDialog
from .review import ReviewPage
from .sheet import ContactSheet
from .shell import Invitation, ProjectShell
from .shortlist import ShortlistPage
from .studio import StudioPage
from .style import StyleDialog, StylePanel
from .suggestions import SuggestionsPage
from .widgets import ProjectCard, Row, band, replace_rows, short_path

ACTIVE = {"running", "queued", "stopping", "detached"}


def job_progress(job: dict) -> int | None:
    """How far along a run is, from its own checkpoint when it has one.

    The checkpoint's cluster counts advance with every decision for the
    whole run; the log's percentage lines belong to the scan and stop
    moving the moment the paid work starts, which read as a stall.
    """
    import re

    progress = job.get("progress") or {}
    total = int(progress.get("total_clusters") or 0)
    if total:
        completed = int(progress.get("completed_clusters") or 0)
        return max(0, min(100, round(100 * completed / total)))
    matches = re.findall(r"(\d{1,3})\s*%", str(job.get("log_tail") or ""))
    if matches:
        return max(0, min(100, int(matches[-1])))
    return 0 if job.get("status") == "running" else None


JOB_TITLES = {
    "culling": "Culling",
    "professional_shortlist": "Assessing",
    "edit_suggestions": "Suggesting edits",
    "style_profile": "Reading your style",
    "semantic_verification": "Verifying",
}


def job_title(job: dict) -> str:
    """Name a queued job the way the person who started it would."""
    kind = str(job.get("kind") or "culling")
    return JOB_TITLES.get(kind, kind.replace("_", " ").capitalize())


def active_job(project: dict) -> dict:
    """The job a folder is currently waiting on, if any.

    A folder can be assessed as well as culled, so the meter has to follow
    whichever run is actually in flight rather than assuming the cull.
    """
    for key in ("assessment", "culling"):
        job = project.get(key) or {}
        if str(job.get("status")) in ACTIVE:
            return job
    return {}


def project_state(project: dict) -> tuple[str, str]:
    """Return the label and tone for one folder.

    The stage furthest along that is still in flight wins, because that is
    the thing the person is waiting for.
    """
    status = str((project.get("culling") or {}).get("status", ""))
    assessing = str((project.get("assessment") or {}).get("status", ""))
    if assessing in {"running", "queued"}:
        return "Assessing", "running"
    if status in {"running", "stopping", "detached"}:
        return "Culling", "running"
    if status == "queued":
        return "Queued", "running"
    if "paused" in (assessing, status):
        return "Paused mid-run", "failed"
    if assessing == "failed":
        return "Assessment failed", "failed"
    if status == "failed":
        return "Failed", "failed"
    if project.get("shortlist_available"):
        return "Assessed", "ready"
    if project.get("report_available") and not project.get("report_is_manual"):
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


class CullProgress(QWidget):
    """The cull phase while its run is actually running.

    The invitation's buy button has done its job; what the page owes the
    photographer now is what the run is doing and how far along it is --
    the kept frames gathering above, the undecided ones thinning below,
    both straight from the run's own checkpoint.
    """

    def __init__(self, name: str, on_pause=None,
                 names: list[str] | None = None, loader=None,
                 checkpoint: Path | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.names = list(names or [])
        self.loader = loader
        self.checkpoint = checkpoint
        self._shown: tuple[int, int] = (-1, -1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 26)
        outer.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel(f"Culling {name}")
        title.setObjectName("clusterTitle")
        title.setFont(theme.display(20))
        head.addWidget(title, 1)
        self.pause_button = None
        if on_pause is not None:
            self.pause_button = QPushButton("Pause")
            self.pause_button.setObjectName("ghost")
            self.pause_button.setFont(theme.body(9))
            self.pause_button.setToolTip(
                "Stop after the current group. Every decision made so far "
                "is checkpointed; resuming buys only the rest.")
            self.pause_button.clicked.connect(lambda _=False: on_pause())
            head.addWidget(self.pause_button)
        outer.addLayout(head)
        self.message = QLabel("The culling worker is starting.")
        self.message.setObjectName("hint")
        self.message.setWordWrap(True)
        self.message.setFont(theme.body(10))
        outer.addWidget(self.message)
        from PySide6.QtWidgets import QProgressBar

        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(4)
        outer.addWidget(self.meter)
        self.detail = QLabel("")
        self.detail.setObjectName("hint")
        self.detail.setFont(theme.body(9))
        outer.addWidget(self.detail)
        self.kept_title = QLabel("")
        self.kept_title.setObjectName("clusterTitle")
        self.kept_title.setFont(theme.display(12))
        outer.addWidget(self.kept_title)
        self._kept_slot = QVBoxLayout()
        outer.addLayout(self._kept_slot, 3)
        self.waiting_title = QLabel("")
        self.waiting_title.setObjectName("clusterTitle")
        self.waiting_title.setFont(theme.display(12))
        outer.addWidget(self.waiting_title)
        self._waiting_slot = QVBoxLayout()
        outer.addLayout(self._waiting_slot, 2)
        self.kept_sheet: QWidget | None = None
        self.waiting_sheet: QWidget | None = None

    def _decisions(self) -> list[dict]:
        if self.checkpoint is None:
            return []
        try:
            state = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        decisions = []
        for item in state.get("decisions", []):
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError:
                    continue
            if isinstance(item, dict):
                decisions.append(item)
        return decisions

    def _refill(self, decisions: list[dict]) -> None:
        keepers: list[str] = []
        decided: set[str] = set()
        for decision in decisions:
            keepers.extend(
                name for name in decision.get("keepers", [])
                if isinstance(name, str))
            # Older checkpoints recorded only the keepers; the members
            # list makes the waiting area exact rather than approximate.
            members = decision.get("photos") or decision.get("keepers", [])
            decided.update(
                name for name in members if isinstance(name, str))
        waiting = [name for name in self.names if name not in decided]
        state = (len(keepers), len(waiting))
        if state == self._shown or self.loader is None:
            return
        self._shown = state
        for slot, old in ((self._kept_slot, self.kept_sheet),
                          (self._waiting_slot, self.waiting_sheet)):
            if old is not None:
                slot.removeWidget(old)
                old.deleteLater()
        self.kept_title.setText(
            f"Kept so far · {len(keepers)}" if keepers
            else "Kept so far · none yet")
        show_waiting = bool(waiting)
        self.waiting_title.setVisible(show_waiting)
        self.waiting_title.setText(
            f"Awaiting a decision · {len(waiting)}")
        self.kept_sheet = ContactSheet(keepers, self.loader)
        self._kept_slot.addWidget(self.kept_sheet)
        self.waiting_sheet = None
        if show_waiting:
            self.waiting_sheet = ContactSheet(waiting, self.loader)
            self._waiting_slot.addWidget(self.waiting_sheet)

    def set_job(self, job: dict) -> None:
        self.message.setText(str(job.get("message") or ""))
        if self.pause_button is not None:
            self.pause_button.setEnabled(
                job.get("status") in {"running", "detached"})
        progress = job.get("progress") or {}
        total = int(progress.get("total_clusters") or 0)
        completed = int(progress.get("completed_clusters") or 0)
        if total:
            self.meter.setValue(round(100 * completed / total))
            self.detail.setText(
                f"{completed} of {total} groups decided. Decisions are "
                "checkpointed as they land; an interrupted run resumes "
                "without buying them again.")
        else:
            self.meter.setValue(job_progress(job) or 0)
            self.detail.setText(
                "Reading the folder and preparing previews.")
        self._refill(self._decisions())


def show_assessment(record: dict, parent: QWidget | None = None) -> None:
    """One frame's assessment, exactly as the model gave it."""
    from opencull_gui.shortlist import ASSESSMENT_FIELDS

    photo = str(record.get("photo") or "")
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Why {photo} was rated")
    dialog.setStyleSheet(theme.STYLESHEET)
    dialog.setMinimumWidth(560)
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(22, 20, 22, 18)
    outer.setSpacing(10)
    tier = str(record.get("tier") or "")
    try:
        score = f"score {float(record.get('score', 0)):.0f}"
    except (TypeError, ValueError):
        score = ""
    head = QLabel(
        f"{tier_stars(tier)}   {tier.title()}"
        + (f"   ·   {score}" if score else ""))
    head.setObjectName("clusterTitle")
    head.setFont(theme.display(13))
    outer.addWidget(head)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    holder = QWidget()
    holder.setObjectName("page")
    body = QVBoxLayout(holder)
    body.setContentsMargins(0, 0, 8, 0)
    body.setSpacing(8)
    for label, text in (
        [("OVERALL", str(record.get("rationale") or ""))]
        + [(field.replace("_", " ").upper(), str(record.get(field) or ""))
           for field in ASSESSMENT_FIELDS]
    ):
        if not text.strip():
            continue
        name = QLabel(label)
        name.setObjectName("bandTitle")
        name.setFont(theme.display(7))
        body.addWidget(name)
        reading = QLabel(text.strip())
        reading.setObjectName("hint")
        reading.setWordWrap(True)
        reading.setFont(theme.body(9))
        body.addWidget(reading)
    body.addStretch(1)
    scroll.setWidget(holder)
    outer.addWidget(scroll, 1)
    close = QPushButton("Close")
    close.setObjectName("ghost")
    close.setFont(theme.body(10))
    close.setCursor(Qt.CursorShape.PointingHandCursor)
    close.clicked.connect(lambda _checked=False: dialog.accept())
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(close)
    outer.addLayout(row)
    dialog.resize(600, 560)
    dialog.exec()


class AssessProgress(QWidget):
    """The assessment phase while its run is actually running.

    The rated frames gather above with their tiers tallied, the
    unrated thin below, both straight from the run's own checkpoint.
    """

    def __init__(self, name: str, on_pause=None,
                 names: list[str] | None = None, loader=None,
                 checkpoint: Path | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.names = list(names or [])
        self.loader = loader
        self.checkpoint = checkpoint
        self._shown: tuple[int, int] = (-1, -1)
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 26)
        outer.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel(f"Assessing {name}")
        title.setObjectName("clusterTitle")
        title.setFont(theme.display(20))
        head.addWidget(title, 1)
        self.pause_button = None
        if on_pause is not None:
            self.pause_button = QPushButton("Pause")
            self.pause_button.setObjectName("ghost")
            self.pause_button.setFont(theme.body(9))
            self.pause_button.setToolTip(
                "Stop after the current frame. Every rating so far is "
                "checkpointed; resuming buys only the rest.")
            self.pause_button.clicked.connect(lambda _=False: on_pause())
            head.addWidget(self.pause_button)
        outer.addLayout(head)
        self.message = QLabel("The assessment worker is starting.")
        self.message.setObjectName("hint")
        self.message.setWordWrap(True)
        self.message.setFont(theme.body(10))
        outer.addWidget(self.message)
        from PySide6.QtWidgets import QProgressBar

        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(4)
        outer.addWidget(self.meter)
        self.detail = QLabel("")
        self.detail.setObjectName("hint")
        self.detail.setFont(theme.body(9))
        outer.addWidget(self.detail)
        self.rated_title = QLabel("")
        self.rated_title.setObjectName("clusterTitle")
        self.rated_title.setFont(theme.display(12))
        outer.addWidget(self.rated_title)
        self._rated_slot = QVBoxLayout()
        outer.addLayout(self._rated_slot, 3)
        self.waiting_title = QLabel("")
        self.waiting_title.setObjectName("clusterTitle")
        self.waiting_title.setFont(theme.display(12))
        outer.addWidget(self.waiting_title)
        self._waiting_slot = QVBoxLayout()
        outer.addLayout(self._waiting_slot, 2)
        self.rated_sheet: QWidget | None = None
        self.waiting_sheet: QWidget | None = None

    def _assessed(self) -> list[dict]:
        if self.checkpoint is None:
            return []
        try:
            state = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        records = []
        for item in state.get("assessments", []):
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError:
                    continue
            if isinstance(item, dict):
                records.append(item)
        return records

    def _refill(self, records: list[dict]) -> None:
        rated = [str(item.get("photo") or "") for item in records]
        rated = [name for name in rated if name]
        waiting = [name for name in self.names if name not in set(rated)]
        state = (len(rated), len(waiting))
        if state == self._shown or self.loader is None:
            return
        self._shown = state
        for slot, old in ((self._rated_slot, self.rated_sheet),
                          (self._waiting_slot, self.waiting_sheet)):
            if old is not None:
                slot.removeWidget(old)
                old.deleteLater()
        tally: dict[str, int] = {}
        for item in records:
            tier = str(item.get("tier") or "").strip().lower()
            if tier:
                tally[tier] = tally.get(tier, 0) + 1
        tiers = ", ".join(
            f"{count} {tier}" for tier, count in sorted(
                tally.items(), key=lambda pair: -pair[1]))
        self.rated_title.setText(
            f"Assessed so far · {len(rated)}"
            + (f" — {tiers}" if tiers else "")
            if rated else "Assessed so far · none yet")
        show_waiting = bool(waiting)
        self.waiting_title.setVisible(show_waiting)
        self.waiting_title.setText(
            f"Awaiting assessment · {len(waiting)}")
        self.rated_sheet = ContactSheet(rated, self.loader)
        self._records = {str(item.get("photo") or ""): item
                         for item in records}
        self.rated_sheet.inspect_wanted.connect(self._explain)
        for item in records:
            tile = self.rated_sheet.tiles.get(str(item.get("photo") or ""))
            if tile is None:
                continue
            stars = tier_stars(str(item.get("tier") or ""))
            if stars:
                tile.set_badge(stars, str(item.get("tier") or "").title())
            tile.set_inspectable(True)
        self._rated_slot.addWidget(self.rated_sheet)

        self.waiting_sheet = None
        if show_waiting:
            self.waiting_sheet = ContactSheet(waiting, self.loader)
            self._waiting_slot.addWidget(self.waiting_sheet)

    def _explain(self, photo: str) -> None:
        """The model's own words about one frame, while the run continues."""
        record = getattr(self, "_records", {}).get(photo)
        if not record:
            return
        show_assessment(record, self)

    def set_job(self, job: dict) -> None:
        self.message.setText(str(job.get("message") or ""))
        if self.pause_button is not None:
            self.pause_button.setEnabled(
                job.get("status") in {"running", "detached"})
        progress = job.get("progress") or {}
        total = int(progress.get("total_items") or 0)
        completed = int(progress.get("completed_items") or 0)
        if total:
            self.meter.setValue(round(100 * completed / total))
            self.detail.setText(
                f"{completed} of {total} frames assessed. Ratings are "
                "checkpointed as they land; an interrupted run resumes "
                "without buying them again.")
        else:
            self.meter.setValue(job_progress(job) or 0)
            self.detail.setText("Preparing the selection.")
        self._refill(self._assessed())


RECULL_WARNING = (
    "Culling {name} again discards the current selection and asks the models "
    "to choose from scratch. It costs another full run.\n\n"
    "Your own keeper and reject marks are kept, but the AI recommendations "
    "they sit beside will change.\n\n"
    "Only do this if the previous cull was wrong in a large way. To disagree "
    "with a few frames, open the review and change them there.")


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
        self.style_profiles = StyleProfileStore(
            services.paths.support / "style-profile.json", services.paths.results)
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
        self.review_page: ProjectShell | None = None
        self.bench: Bench | None = None

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

        self.studio_button = QPushButton("The Studio")
        self.studio_button.setObjectName("ghost")
        self.studio_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.studio_button.setFont(theme.body(10))
        self.studio_button.setToolTip(
            "Your own things: profiles, the taste ledger, providers. They "
            "serve every folder alike.")
        self.studio_button.clicked.connect(self.show_studio)
        layout.addWidget(self.studio_button)
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
            self._refresh_phases()
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
            Row(str(job.get("name") or Path(str(job.get("photos", ""))).name
                    or "Job"),
                short_path(str(job.get("photos", ""))),
                job_title(job) if job.get("status") == "running" else "Queued",
                "running",
                [("Cancel", lambda job=job: self.cancel(job))],
                progress=job_progress(job),
                last=index == len(active) - 1)
            for index, job in enumerate(active)
        ])

    def _refresh_phases(self) -> None:
        """Keep the phase bar honest while a run is in flight.

        A cull started from inside the shell finishes somewhere else. The
        bar has to notice, or the phase it filled stays an invitation to do
        what has already been done.
        """
        shell, bench = self.review_page, self.bench
        if not isinstance(shell, ProjectShell) or bench is None:
            return
        try:
            queue = self.services.jobs.public()
            projects = self.services.projects.public(queue)["projects"]
        except Exception:                            # noqa: BLE001 - transient
            return
        current = next(
            (item for item in projects
             if item.get("id") == bench.project.get("id")), None)
        if current is None:
            return
        bench.project = current
        shell.show_plan(self.phase_plan(current, bench))
        # The cull page follows its run: an invitation becomes a progress
        # page when the run starts, ticks while it runs, and becomes the
        # review when the report lands -- without anyone reopening it.
        page = shell.page_for(phases.CULL)
        running = next(
            (job for job in self._culling_jobs(
                str(current.get("photos", "")))
             if job.get("status") in
             {"running", "queued", "stopping", "detached"}), None)
        if isinstance(page, CullProgress):
            if running is not None:
                page.set_job(running)
            else:
                shell.rebuild(phases.CULL)
        elif page is not None and running is not None:
            shell.rebuild(phases.CULL)
        assess_page = shell.page_for(phases.ASSESSMENT)
        assessing = next(
            (job for job in self._professional_jobs(
                str(current.get("photos", "")))
             if job.get("status") in
             {"running", "queued", "stopping", "detached"}), None)
        if isinstance(assess_page, AssessProgress):
            if assessing is not None:
                assess_page.set_job(assessing)
            else:
                shell.rebuild(phases.ASSESSMENT)
        elif assess_page is not None and assessing is not None:
            shell.rebuild(phases.ASSESSMENT)

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
            # Held once: reparenting can release the layout item's own
            # reference, so asking it a second time can answer None.
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
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
        # Review and assessment work over any selection, including the
        # manual everything-included one; only the cull button's label and
        # its costs-another-run warning care whether a model actually ran.
        culled = bool(project.get("report_available"))
        culled_by_model = culled and not project.get("report_is_manual")
        running = tone == "running"
        actions: list[tuple[str, object]] = []
        in_flight = active_job(project)
        if in_flight.get("status") in {"running", "detached"}:
            actions.append((
                "Pause",
                lambda j=str(in_flight.get("id", "")): self.pause_job(j)))
        paused_run = next(
            (project.get(key) for key in ("assessment", "culling")
             if (project.get(key) or {}).get("status") == "paused"), None)
        if paused_run:
            actions.append((
                "Resume",
                lambda j=str(paused_run.get("id", "")): self.resume_job(j)))
        if culled:
            actions.append(("Review", lambda p=project: self.open_review(p)))
        # Assessment reads the cull and its review, so it is only offered
        # once there is a cull to read.
        if culled and not running:
            # "Marks" opens the ratings that already exist; "Assess" starts
            # one. The longer word "Assessment" cannot fit four-abreast on
            # a fixed-width card without being clipped mid-word.
            actions.append((
                "Marks" if project.get("shortlist_available") else "Assess",
                lambda p=project: self.assess_project(p)))
        contents = self.folder_contents(project)
        treatment = treatment_label(contents["kind"])
        name = str(project.get("name", ""))
        opens = bool(treatment and not running and project.get("available"))
        if opens:
            actions.append((treatment, lambda p=project: self.develop(p)))
        if not running and project.get("available"):
            actions.append((
                "Re-cull" if culled_by_model else "Cull",
                lambda p=project, again=culled_by_model:
                    self.cull_project(p, again)))

        total = int(contents.get("total") or 0)
        subtitle = (
            f"{total:,} photograph{'' if total == 1 else 's'}" if total
            else "no readable photographs")
        card = ProjectCard(
            name,
            short_path(str(project.get("photos", ""))),
            subtitle, label, tone, actions,
            progress=job_progress(active_job(project)) if running else None,
            on_remove=lambda p=project: self.remove_project(p),
            # The strip opens the shoot where the photographer left off,
            # or on the cull the first time. A folder mid-run opens too:
            # its cull page is the live progress of the run. Only a folder
            # with nothing readable, or one off its disk, stays inert.
            on_open=(
                (lambda p=project: self.open_project(p))
                if project.get("available") and total
                and contents.get("kind") != "empty"
                else None),
            open_hint=(
                f"Open {name}".strip()
                if project.get("available") and total
                and contents.get("kind") != "empty"
                else ""))

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
        culled_by_model = culled and not project.get("report_is_manual")
        running = tone == "running"
        actions: list[tuple[str, object]] = []
        in_flight = active_job(project)
        if in_flight.get("status") in {"running", "detached"}:
            actions.append((
                "Pause",
                lambda j=str(in_flight.get("id", "")): self.pause_job(j)))

        paused_run = next(
            (project.get(key) for key in ("assessment", "culling")
             if (project.get(key) or {}).get("status") == "paused"), None)
        if paused_run:
            actions.append((
                "Resume",
                lambda j=str(paused_run.get("id", "")): self.resume_job(j)))
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
                "Re-cull" if culled_by_model else "Cull",
                lambda p=project, again=culled_by_model:
                    self.cull_project(p, again)))

        return Row(
            str(project.get("name", "")),
            short_path(str(project.get("photos", ""))),
            label, tone, actions,
            progress=job_progress(active_job(project)) if running else None,
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

    def resume_job(self, job_id: str) -> None:
        """Wake a paused run; the checkpoint pays for what was done."""
        try:
            self.services.jobs.action(job_id, "resume")
        except Exception as exc:
            self._say(str(exc), "alarm")
            return
        self._say(
            "Resuming from the checkpoint. Everything already done is "
            "kept.", "ok")
        self.refresh()

    def pause_job(self, job_id: str) -> None:
        """Stop a running job after its current step, keeping the checkpoint."""
        try:
            self.services.jobs.action(job_id, "pause")
        except Exception as exc:
            self._say(str(exc), "alarm")
            return
        self._say(
            "Pausing. Every decision made so far is kept; the same button "
            "resumes the run.", "ok")
        self.refresh()

    def _professional_jobs(self, photos: str) -> list[dict]:
        try:
            queue = self.services.jobs.public()["jobs"]
        except Exception:                            # noqa: BLE001 - transient
            return []
        return [
            job for job in queue
            if job.get("kind") == "professional_shortlist"
            and str(job.get("photos") or "")
            and Path(str(job["photos"])) == Path(photos)]

    def _culling_jobs(self, photos: str) -> list[dict]:
        try:
            queue = self.services.jobs.public()["jobs"]
        except Exception:                            # noqa: BLE001 - transient
            return []
        return [
            job for job in queue
            if job.get("kind", "culling") == "culling"
            and str(job.get("photos") or "")
            and Path(str(job["photos"])) == Path(photos)]

    def cull_project(self, project: dict, again: bool = False,
                     only_photos: list[str] | None = None) -> None:
        name = str(project.get("name") or Path(str(project.get("photos", ""))).name)
        photos = str(project.get("photos", ""))
        mine = self._culling_jobs(photos)
        if any(job.get("status") in
               {"running", "queued", "stopping", "detached"}
               for job in mine):
            self._say(
                f"{name} is already being culled. Progress shows right "
                "here as it runs.", "ok")
            return
        paused = next(
            (job for job in reversed(mine)
             if job.get("status") == "paused"), None)
        if paused is not None:
            # The interrupted run already paid for part of its work. The
            # same button resumes it rather than buying a second run.
            try:
                self.services.jobs.action(str(paused["id"]), "resume")
            except Exception as exc:
                self._say(str(exc), "alarm")
                return
            self._say(
                f"Resuming the cull of {name} from its checkpoint. "
                "Everything already decided is kept.", "ok")
            self.refresh()
            return
        if again and not self.confirm_recull(name):
            return
        try:
            # The stored provider drives the cull like every other stage.
            # With no profile configured this stays empty and the run falls
            # back to the legacy local program, which still works offline.
            self.services.jobs.add(
                photos, "", 2, True, "family", self.provider_id(),
                None, 5, 4, only_photos=only_photos)
        except Exception as exc:
            # A missing provider credential surfaces here, and is the most
            # common reason a cull cannot start, so say so plainly.
            self._say(str(exc), "alarm")
            self.refresh()
            return
        self._say(
            f"Culling {name}. It will be marked Culled when the run finishes.",
            "ok")
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

    def open_project(self, project: dict, phase: str = "") -> None:
        """Open one folder in this window, on one of its phases.

        Every phase of a shoot lives behind the same chrome and the same
        phase bar, so opening a folder is one act rather than four
        differently-shaped ones.
        """
        report_path = Path(str(project.get("report", "")))
        try:
            if not report_path.is_file():
                # Development and the phase bar both need a selection. A
                # folder that was never culled gets the deterministic
                # everything-included one, so nothing here demands a cull.
                report_path = self.services.projects.manual_selection_report(
                    str(project.get("id", "")))
        except (ProjectCatalogError, ValueError, OSError) as exc:
            self.report(str(exc), "alarm")
            return
        self._close_review()
        try:
            bench = Bench(project, self.services.paths.cache, report_path)
            self._loader = PreviewLoader(bench.photos, self)
        except Exception as exc:
            name = project.get("name", "That folder")
            self.report(f"{name} could not be opened: {exc}", "alarm")
            return
        shell = ProjectShell(project, lambda key: self._phase_page(bench, key))
        shell.closed.connect(self.show_projects)
        self.bench = bench
        self.review_page = shell
        self.pages.addWidget(shell)
        self.pages.setCurrentWidget(shell)
        shell.show_plan(self.phase_plan(project, bench))
        # The folder remembers which phase the photographer was working
        # in, so coming back -- from the library or from a fresh launch --
        # lands where they left off. A folder never opened lands on the
        # cull. Connected before the first opening so the landing itself
        # is remembered too.
        manifest_path = str(project.get("project", ""))
        shell.opened.connect(
            lambda key, path=manifest_path: self._remember_phase(path, key))
        if not shell.open_phase(phase or self._remembered_phase(manifest_path)):
            # A folder whose photographs are not on disk has every phase of
            # its own shut. Landing on the first one that is open beats
            # landing on nothing at all.
            for item in shell.plan:
                if item["state"] != "blocked" and shell.open_phase(item["id"]):
                    break
        self.refresh()

    @staticmethod
    def _remembered_phase(manifest_path: str) -> str:
        try:
            remembered = str(load_project(
                Path(manifest_path)).get("last_phase") or "")
        except (OSError, ValueError):
            remembered = ""
        return remembered if remembered in phases.ORDER else phases.CULL

    @staticmethod
    def _remember_phase(manifest_path: str, key: str) -> None:
        path = Path(manifest_path)
        if key in phases.ORDER and path.is_file():
            try:
                update_project(path, last_phase=key)
            except (OSError, ValueError):
                pass

    def phase_plan(self, project: dict, bench: Bench) -> list[dict]:
        return phases.plan(
            project,
            profile_selected=bool(self.style_profiles.selected()),
            marked=bench.marked(), culled=bench.culled(),
            suggested=bench.suggested())

    @staticmethod
    def _wire_selection(invitation, sheet, label) -> None:
        """Keep the paid button's count equal to the sheet's selection.

        The price on the button is the price paid, so unticking a frame
        re-counts it immediately. Nothing chosen disables the buttons with
        the reason where the press would have been.
        """
        def recount() -> None:
            count = len(sheet.chosen())
            invitation.button.setText(label(count))
            nothing = count == 0
            reason = ("Every frame is unticked, so there is nothing for "
                      "the run to read. Tick at least one frame.")
            for button in (invitation.button, invitation.other):
                if button is None:
                    continue
                button.setEnabled(not nothing)
                button.setToolTip(reason if nothing else "")
        sheet.changed.connect(recount)
        recount()

    # --- the pages behind the phases -------------------------------------

    def _phase_page(self, bench: Bench, key: str):
        builders = {
            phases.CULL: self._cull_page,
            phases.ASSESSMENT: self._assessment_page,
            phases.PROFILE: self._profile_page,
            phases.SUGGESTIONS: self._suggestions_page,
            phases.DEVELOPMENT: self._development_page,
            phases.FINE_TUNING: self._finetune_page,
            phases.EXPORT: self._export_page,
            phases.DEBRIEF: self._debrief_page,
        }
        build = builders.get(key)
        if build is None:
            return None
        try:
            return build(bench)
        except Exception as exc:                     # noqa: BLE001 - reported
            shell = self.review_page
            if isinstance(shell, ProjectShell):
                shell.report(f"That phase could not be opened: {exc}", "alarm")
            return None

    def _cull_page(self, bench: Bench):
        # Not "has a report": a folder developed without culling has one,
        # written locally to include everything. There is nothing to review
        # in a selection that kept every frame.
        if not bench.culled():
            mine = self._culling_jobs(str(bench.project.get("photos", "")))
            running = next(
                (job for job in mine
                 if job.get("status") in
                 {"running", "queued", "stopping", "detached"}), None)
            if running is not None:
                page = CullProgress(
                    str(bench.project.get("name") or ""),
                    on_pause=lambda job=str(running.get("id", "")):
                        self.pause_job(job),
                    names=list(bench.report.photo_names),
                    loader=self._loader,
                    checkpoint=Path(str(running.get("checkpoint") or "")))
                page.set_job(running)
                return page
            paused = next(
                (job for job in reversed(mine)
                 if job.get("status") == "paused"), None)
            if paused is not None:
                frames = list(bench.report.photo_names)
                return Invitation(
                    "This cull is paused mid-run",
                    "The run stopped partway and left its checkpoint. "
                    "Resuming keeps every group already decided and buys "
                    "only the rest.",
                    "Resume culling",
                    lambda: self.cull_project(bench.project),
                    f"The run covers {len(frames)} photographs.",
                    shows=ContactSheet(frames, self._loader))
            frames = list(bench.report.photo_names)
            count = len(frames)
            sheet = ContactSheet(frames, self._loader, selectable=True)
            invitation = Invitation(
                "This folder has not been culled",
                "A cull reads every frame, groups the near-duplicates, and "
                "proposes which one of each group to keep. You review what it "
                "proposes; nothing is deleted, ever. Click a frame to leave "
                "it out of the run.",
                f"Cull these {count} photographs",
                lambda: self.cull_project(
                    bench.project,
                    # Full selection stays byte-identical to an unfiltered
                    # cull: same command, same manifest, same checkpoints.
                    only_photos=(
                        sheet.chosen()
                        if len(sheet.chosen()) < count else None)),
                f"These are the {count} photographs in the folder. Every one "
                "ticked is read by a model, so this costs.",
                shows=sheet)
            self._wire_selection(
                invitation, sheet,
                lambda chosen: (
                    f"Cull these {count} photographs" if chosen == count
                    else f"Cull {chosen} of these {count} photographs"))
            return invitation
        return ReviewPage(bench.report, bench.reviews, self._loader)

    def _assessment_page(self, bench: Bench):
        if not bench.shortlist_path.is_file():
            mine = self._professional_jobs(
                str(bench.project.get("photos", "")))
            running = next(
                (job for job in mine
                 if job.get("status") in
                 {"running", "queued", "stopping", "detached"}), None)
            if running is not None:
                page = AssessProgress(
                    str(bench.project.get("name") or ""),
                    on_pause=lambda job=str(running.get("id", "")):
                        self.pause_job(job),
                    names=bench.selection(),
                    loader=self._loader,
                    checkpoint=Path(str(running.get("checkpoint") or "")))
                page.set_job(running)
                return page
            paused = next(
                (job for job in reversed(mine)
                 if job.get("status") == "paused"), None)
            if paused is not None:
                return Invitation(
                    "This assessment is paused mid-run",
                    str(paused.get("message") or
                        "The run stopped partway and left its checkpoint. "
                        "Resuming keeps every rating already bought."),
                    "Resume assessment",
                    lambda job=str(paused.get("id", "")):
                        self.resume_job(job),
                    "")
            culled = bench.culled()
            frames = bench.selection()
            count = len(frames)
            # A run can rate every frame and still fail to certify. Say
            # what is waiting rather than inviting a second purchase of
            # work already paid for.
            waiting = unfinished_assessments(bench.shortlist_path)
            sheet = ContactSheet(frames, self._loader, selectable=True)
            invitation = Invitation(
                "This folder has not been assessed",
                "An assessment judges each frame in the selection for what it "
                "could become. You then mark the ones worth developing, and "
                "those are the ones that get editing suggestions. Click a "
                "frame to leave it out.",
                f"Assess these {count}" if culled else
                f"Assess all {count} frames",
                lambda: self.assess_project(bench.project, sheet.chosen()),
                ((" ".join(
                    f"A previous run under the {item['bar'].split('(')[0].strip()}"
                    f" rated {item['rated']} of these frames; that work is "
                    "kept, and choosing the same bar again continues from "
                    "it rather than paying twice."
                    for item in waiting[:1]) + " ") if waiting else "")
                + (f"These are the {count} frames the cull kept. Each ticked "
                 "frame is read by a model, so this costs." if culled else
                 "This folder has not been culled, so the selection is every "
                 f"frame in it -- about {count} model calls rather than one "
                 "per keeper. Culling first is usually cheaper.")
                + " Frames left out are recorded in the cull review, where "
                "the choice can be reversed.",
                shows=sheet,
                instead=("Rate them myself",
                         lambda: self.rate_by_hand(bench, sheet.chosen())))
            self._wire_selection(
                invitation, sheet,
                lambda chosen: (
                    (f"Assess these {count}" if culled else
                     f"Assess all {count} frames") if chosen == count
                    else f"Assess {chosen} of these {count}"))
            return invitation
        page = ShortlistPage(
            bench.shortlist, bench.shortlist_reviews, self._loader,
            directions=bench.directions)
        page.why_wanted.connect(self.explain_frame)
        page.reassess_wanted.connect(
            lambda pr=bench.project: self.reassess_project(pr))
        return page

    def _profile_page(self, bench: Bench):
        panel = StylePanel(
            self.style_profiles, self.services.jobs, self.services.providers,
            closable=False)
        return panel

    def _suggestions_page(self, bench: Bench):
        page = SuggestionsPage(
            bench.shortlist, bench.shortlist_reviews, bench.directions)
        page.why_wanted.connect(self.explain_frame)
        page.suggested.connect(
            lambda output, wanted: self.suggest_edits(
                bench.shortlist, bench.shortlist_reviews, bench.photos,
                output, wanted, bench.project))
        return page

    def _development_page(self, bench: Bench):
        page = DevelopPage(bench.report, bench.workspace, self._loader)
        page.verification_wanted.connect(
            lambda request: self.verify_render(bench.workspace, request))
        page.why_wanted.connect(self.explain_frame)
        return page

    def _finetune_page(self, bench: Bench):
        return FineTunePage(bench.report, bench.workspace, self._loader)

    def _export_page(self, bench: Bench):
        return ExportPage(bench.workspace)

    def _debrief_page(self, bench: Bench):
        return DebriefPage(debrief_aggregate(bench))

    # --- starting the runs behind the phases ------------------------------

    def develop(self, project: dict) -> None:
        self.open_project(project, phases.DEVELOPMENT)

    def open_review(self, project: dict) -> None:
        self.open_project(project, phases.CULL)

    def open_shortlist(self, project: dict) -> None:
        self.open_project(project, phases.ASSESSMENT)

    def reassess_project(self, project: dict) -> None:
        """Ask the models to assess this folder's selection again."""
        name = str(project.get("name")
                   or Path(str(project.get("photos", ""))).name)
        report_path = Path(str(project.get("report", "")))
        if not report_path.is_file():
            self._say("That report is no longer on disk.", "alarm")
            return
        review = default_review_path(report_path)
        stance = self.ask_criteria(
            name, len(self.bench.selection()) if self.bench else 0,
            self.bench.culled() if self.bench else True)
        if not stance:
            return
        try:
            self.services.jobs.add_professional(
                str(report_path), str(project.get("photos", "")),
                review=str(review) if review.is_file() else "",
                profile=stance, provider_profile_id=self.provider_id(),
                reassess=True)
        except Exception as exc:
            self._say(str(exc), "alarm")
            return
        # Drop the built shortlist page so the phase reopens on the live
        # progress of the new run.
        shell = self.review_page
        if isinstance(shell, ProjectShell):
            shell.rebuild(phases.ASSESSMENT)
        self._say(f"Reassessing {name}. Your own ratings are kept.", "ok")
        self.refresh()

    def assess_project(self, project: dict,
                       chosen: list[str] | None = None) -> None:
        """Queue the assessment of what stayed ticked.

        The prefilter is written only after the criteria dialog accepts, so
        cancelling leaves no trace anywhere -- and it is written as an
        ordinary human review, through the same store the review page uses,
        so the run, the counts, and any open review agree.
        """
        mine = self._professional_jobs(str(project.get("photos", "")))
        if any(job.get("status") in
               {"running", "queued", "stopping", "detached"}
               for job in mine):
            self._say(
                "This folder is already being assessed. Progress shows "
                "on the assessment page as it runs.", "ok")
            return
        paused = next(
            (job for job in reversed(mine)
             if job.get("status") == "paused"), None)
        if paused is not None:
            self.resume_job(str(paused["id"]))
            return
        report_path = Path(str(project.get("report", "")))
        if not report_path.is_file():
            self.report("That report is no longer on disk.", "alarm")
            return
        bench = self.bench
        culled = bench.culled() if bench is not None else True
        selection = bench.selection() if bench is not None else []
        # The number quoted is the number the run will read.
        frames = len(chosen) if chosen is not None else len(selection)
        if chosen is not None and not chosen:
            self._say("Every frame is unticked; there is nothing to assess.",
                      "alarm")
            return
        name = str(project.get("name") or Path(
            str(project.get("photos", ""))).name)
        stance = self.ask_criteria(name, frames, culled)
        if not stance:
            return
        if (
            bench is not None and chosen is not None
            and set(chosen) != set(selection)
        ):
            try:
                narrow_selection(bench.reviews, chosen)
            except ReviewError as exc:
                # Whatever was written before the failure is a valid
                # recorded decision, but the price shown is no longer the
                # price that would be paid -- so nothing is queued.
                self._say(f"The prefilter could not be recorded: {exc}",
                          "alarm")
                return
            shell = self.review_page
            if isinstance(shell, ProjectShell):
                # A built review page shares the memoized store and would
                # show keepers the prefilter just changed.
                shell.drop(phases.CULL)
            if set(bench.selection()) != set(chosen):
                self._say(
                    "The recorded selection does not match what was ticked, "
                    "so nothing was queued.", "alarm")
                return
        photos = str(project.get("photos", ""))
        review = default_review_path(report_path)
        try:
            self.services.jobs.add_professional(
                str(report_path), photos,
                review=str(review) if review.is_file() else "",
                profile=stance,
                provider_profile_id=self.provider_id())
        except Exception as exc:
            self._say(str(exc), "alarm")
            self.refresh()
            return
        self.stance = stance
        self._say(
            f"Assessing {name} against the {stance} bar. "
            + (f"{frames} frame{'' if frames == 1 else 's'} are read"
               if chosen is not None and len(chosen) != len(selection)
               else "Every frame the cull kept is read" if culled
               else f"All {frames} frames are read")
            + "; the frames you then mark are the ones that get editing "
            "suggestions.", "ok")
        self.refresh()

    def ask_criteria(self, name: str, frames: int, culled: bool) -> str:
        """Ask what the shoot is judged by, and confirm the spend with it.

        Returns the chosen stance, or an empty string if the run was called
        off. One dialog rather than two: choosing the bar and agreeing to
        pay for it are the same decision.
        """
        dialog = CriteriaDialog(
            name, frames, culled,
            getattr(self, "criteria_choice", None)
            or getattr(self, "stance", ""), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ""
        # The parts are remembered for the next dialog; the composed
        # prose is what the run itself is judged by.
        self.criteria_choice = dialog.choice()
        return dialog.stance()

    def rate_by_hand(self, bench: Bench,
                     chosen: list[str] | None = None) -> None:
        """Open the assessment with nobody's opinion in it but your own.

        Rating your own work should not require buying a model's judgement
        first in order to disagree with it. This lays out the same shortlist
        the assessment would produce, unrated, and costs nothing. Frames
        unticked on the sheet are recorded in the cull review first, so the
        by-hand path and any later paid run agree about the selection.
        """
        try:
            if chosen is not None and set(chosen) != set(bench.selection()):
                narrow_selection(bench.reviews, chosen)
            write_manual_shortlist(
                bench.report, bench.selection(), bench.shortlist_path)
        except Exception as exc:                     # noqa: BLE001 - reported
            self._say(f"That could not be laid out: {exc}", "alarm")
            return
        shell = self.review_page
        if isinstance(shell, ProjectShell):
            # The invitation is stale the moment the shortlist exists.
            shell.drop(phases.ASSESSMENT)
            shell.show_plan(self.phase_plan(bench.project, bench))
            shell.open_phase(phases.ASSESSMENT)
        self._say(
            "Rate them however you like. Nothing here has been assessed, and "
            "no model has been asked.", "ok")

    def suggest_edits(self, shortlist, reviews, photos, output: str,
                      wanted: list, project: dict) -> None:
        """Queue editing directions for the frames that were marked."""
        try:
            _project_path, manifest = load_or_create_folder_project(
                photos.root, shortlist.path.stem)
            self.services.jobs.add_edit_suggestions(
                shortlist=str(shortlist.path), review=str(reviews.path),
                photos=str(photos.root), output=output,
                profile="professional",
                # The project records which profile it last used; the
                # window's selection is what a new run should use.
                style_profile=(
                    self.style_profiles.selected()
                    or str(manifest.get("active_style_profile") or "")),
                only_photos=list(wanted),
                provider_profile_id=self.provider_id())
        except Exception as exc:
            self._say(str(exc), "alarm")
            return
        self._say(
            f"Queued. {len(wanted)} frame"
            f"{'' if len(wanted) == 1 else 's'} will get editing "
            "directions; they appear here when the run finishes.", "ok")

    def verify_render(self, workspace, request: dict) -> None:
        """Ask a model whether one rendering did what it promised."""
        try:
            self.services.jobs.add_semantic_verification(
                original=str(request["original"]),
                developed=str(request["developed"]),
                suggestion=str(request["suggestion"]),
                project=str(workspace.project_path),
                provider_profile_id=self.provider_id())
        except Exception as exc:
            self._say(str(exc), "alarm")
            return
        self._say(
            f"Verifying {request['photo']}. The certificate covers the "
            "full-size render and appears here when it finishes.", "ok")

    def provider_id(self) -> str:
        providers = getattr(self.services, "providers", None)
        profiles = (providers.public().get("profiles") or []
                    if providers is not None else [])
        return str(profiles[0]["id"]) if profiles else ""

    def _say(self, message: str, tone: str = "") -> None:
        """Report where the photographer is looking.

        Inside a folder that is its shell; on the library it is the hero.
        A message put on the page nobody is reading is a message nobody
        gets.
        """
        shell = self.review_page
        if isinstance(shell, ProjectShell):
            shell.report(message, tone)
            page = shell.page_for(shell.current)
            if page is not None and hasattr(page, "_report"):
                page._report(message, tone)
            return
        self.report(message, tone)

    def show_projects(self) -> None:
        self.pages.setCurrentWidget(self.projects_page)
        self._close_review()
        self.refresh()

    def _close_review(self) -> None:
        if self.review_page is not None:
            # A render in flight holds the page alive and would deliver into a
            # widget that is going away.
            shutdown = getattr(self.review_page, "shutdown", None)
            if shutdown is not None:
                shutdown()
        loader = getattr(self, "_loader", None)
        if loader is not None:
            loader.shutdown()
            self._loader = None
        if self.review_page is not None:
            self.pages.removeWidget(self.review_page)
            self.review_page.deleteLater()
            self.review_page = None
        self.bench = None

    def remove_project(self, project: dict) -> None:
        """Forget a folder. Nothing inside it is touched."""
        name = str(project.get("name") or Path(str(project.get("photos", ""))).name)
        if not self.confirm_remove(name):
            return
        try:
            self.services.projects.remove(str(project.get("id", "")))
        except (ProjectCatalogError, ValueError) as exc:
            self.report(str(exc), "alarm")
            self.refresh()
            return
        self._contents.pop(str(project.get("photos", "")), None)
        self.report(
            f"{name} was removed from the library. Its photographs, and any "
            "cull already made, are untouched on disk.")
        self.refresh()

    def confirm_remove(self, name: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Remove from library?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Remove {name} from the library?")
        box.setInformativeText(
            "Darkimiya forgets this folder. The photographs stay exactly where "
            "they are, along with any culling report and review already made, "
            "so adding the folder again brings the work back.\n\n"
            "Nothing in Darkimiya deletes a photograph.")
        remove = box.addButton("Remove from library", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("Keep", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is remove

    def cancel(self, job: dict) -> None:
        try:
            self.services.jobs.action(str(job.get("id", "")), "cancel")
            self.report("")
        except Exception as exc:
            self.report(str(exc), "alarm")
        self.refresh()

    def explain_frame(self, photo: str) -> None:
        """Answer "why is this frame here?" from the records alone."""
        bench = self.bench
        if bench is None or not photo:
            return
        dialog = ProvenanceDialog(photo, frame_story(bench, photo), self)
        dialog.exec()

    def show_studio(self) -> None:
        """One room for the photographer's own things."""
        if getattr(self, "studio_page", None) is None:
            self.studio_page = StudioPage(
                self.style_profiles, self.services.jobs,
                self.services.providers, self.edit_providers)
            self.studio_page.closed.connect(self.show_projects)
            self.pages.addWidget(self.studio_page)
        self.studio_page.panel.refresh()
        self.pages.setCurrentWidget(self.studio_page)

    def edit_providers(self) -> None:
        dialog = ProvidersDialog(self.services.providers, self)
        dialog.exec()
        self.refresh()

    def edit_style(self) -> None:
        dialog = StyleDialog(
            self.style_profiles, self.services.jobs, self.services.providers,
            self)
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
