"""The review page: choose keepers from each cluster of near-duplicates.

This is the work the application exists for, so it is native rather than a
web view held in a window. The report is immutable evidence; every human
decision goes to the review sidecar through ReviewStore, which owns the
revision checking and the atomic write.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.actions import (
    ActionError,
    Operation,
    build_plan,
    default_journal_path,
)
from opencull_gui.report import ReportIndex
from opencull_gui.reviews import ReviewError, ReviewStore, approve_remaining

from . import theme
from .previews import PreviewLoader, plain_icon, scaled
from .widgets import Filmstrip, tooltip, workspace_title

THUMB = 200
# The stored thumb previews are 520px on the long edge, so tiles can grow
# to the width of a large window without ever being enlarged past their
# source and going soft.
THUMB_MAX = 500


class Frame(QFrame):
    """One photograph in a cluster, kept or not."""

    toggled = Signal(str)
    inspected = Signal(str)

    def __init__(self, name: str, index: int, kept: bool, recommended: bool):
        super().__init__()
        self.name = name
        self.kept = kept
        self._pixmap = None
        self._thumb = THUMB
        self.setObjectName("frame")
        self.setProperty("kept", "true" if kept else "false")
        self.setFixedSize(THUMB + 16, THUMB + 58)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setFixedSize(THUMB, THUMB)
        self.image.setObjectName("frameImage")
        self.image.setText("…")
        layout.addWidget(self.image)

        caption = QHBoxLayout()
        caption.setSpacing(6)
        number = QLabel(str(index + 1) if index < 9 else "")
        number.setObjectName("frameNumber")
        number.setFont(theme.mono(8))
        caption.addWidget(number)

        label = QLabel(name)
        label.setObjectName("frameName")
        label.setFont(theme.mono(8))
        label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        caption.addWidget(label, 1)

        if recommended:
            star = QLabel("AI")
            star.setObjectName("frameAi")
            star.setFont(theme.display(7))
            star.setToolTip(tooltip("Recommended by the curator"))
            caption.addWidget(star)
        layout.addLayout(caption)

    def set_pixmap(self, pixmap) -> None:
        self._pixmap = pixmap
        self._render()
        self.image.setText("")

    def set_scale(self, thumb: int) -> None:
        """Give the photograph the size its row was dealt."""
        if thumb == self._thumb:
            return
        self._thumb = thumb
        self.setFixedSize(thumb + 16, thumb + 58)
        self.image.setFixedSize(thumb, thumb)
        self._render()

    def rerender(self) -> None:
        """Redraw for the screen the window is on now."""
        self._render(force=True)

    def _render(self, force: bool = False) -> None:
        if self._pixmap is None:
            return
        state = (self._thumb, self.devicePixelRatioF())
        if not force and state == getattr(self, "_rendered", None):
            return
        self._rendered = state
        self.image.setPixmap(scaled(
            self._pixmap, self._thumb, self._thumb,
            self.devicePixelRatioF()))

    def set_focused(self, focused: bool) -> None:
        self.setProperty("focused", "true" if focused else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_kept(self, kept: bool) -> None:
        self.kept = kept
        self.setProperty("kept", "true" if kept else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggled.emit(self.name)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.inspected.emit(self.name)
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if not self.kept:
            return
        # A kept frame is marked, not merely tinted: the grease-pencil tick a
        # contact sheet would carry.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(theme.SAFELIGHT)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
        painter.end()


class GroupCard(QFrame):
    """One near-duplicate group, shown by its own photographs.

    The card says what the group is -- its frames, how many there are,
    how many are kept, whether it has been reviewed -- and opens into
    the frames themselves for overriding.
    """

    WIDTH = 252
    HEIGHT = 178
    chosen = Signal(str)

    def __init__(self, cluster_id: str, photos: list[str],
                 kept: int, reviewed: bool):
        super().__init__()
        self.cluster_id = cluster_id
        self.photos = list(photos)
        self.setObjectName("card")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip("Open this group to review or override."))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.strip = Filmstrip(min(3, max(1, len(self.photos))))
        self.strip.setFixedHeight(126)
        self.strip.clicked.connect(
            lambda: self.chosen.emit(self.cluster_id))
        self.strip.set_opens(True, "Open this group.")
        layout.addWidget(self.strip)
        caption = QHBoxLayout()
        caption.setContentsMargins(10, 6, 10, 6)
        caption.setSpacing(6)
        mark = QLabel("✓" if reviewed else "·")
        mark.setObjectName("frameAi" if reviewed else "frameNumber")
        mark.setFont(theme.mono(8))
        mark.setToolTip(tooltip(
            "You have reviewed this group." if reviewed
            else "Not reviewed yet; the AI proposal stands."))
        caption.addWidget(mark)
        title = QLabel(cluster_id)
        title.setObjectName("frameName")
        title.setFont(theme.mono(8))
        caption.addWidget(title, 1)
        count = QLabel(f"{kept}/{len(self.photos)} kept")
        count.setObjectName("frameNumber")
        count.setFont(theme.mono(8))
        caption.addWidget(count)
        layout.addLayout(caption)

    def sample_names(self) -> list[str]:
        return self.photos[:self.strip.count]

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.chosen.emit(self.cluster_id)
        super().mousePressEvent(event)


class ZoomView(QWidget):
    """One photograph as large as the tab allows, pannable at 1:1.

    Inspection only: nothing here changes a decision. The caption says
    which frame this is and whether it is currently kept; the keyboard
    keeps working exactly as it does on the frames themselves.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.name = ""
        self._pixmap = None
        self._one_to_one = False
        self._drag_start = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 12, 22, 12)
        layout.setSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image = QLabel("…")
        self.image.setObjectName("frameImage")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.image)
        layout.addWidget(self.scroll, 1)
        self.caption = QLabel("")
        self.caption.setObjectName("hint")
        self.caption.setFont(theme.mono(9))
        layout.addWidget(self.caption)
        hint = QLabel(
            "←/→ next frame · Space toggle keep · Enter keep · "
            "click for 1:1 and drag to pan · Esc back")
        hint.setObjectName("hint")
        hint.setFont(theme.body(9))
        layout.addWidget(hint)

    def show_photo(self, name: str, pixmap, kept: bool,
                   reviewed: bool = False,
                   recommended: bool = False) -> None:
        self.name = name
        self._pixmap = pixmap
        self._one_to_one = False
        self.set_state(kept, reviewed, recommended)
        self._render()

    def set_state(self, kept: bool, reviewed: bool,
                  recommended: bool) -> None:
        state = "kept ✓" if kept else "not kept"
        whose = "your decision" if reviewed else "AI proposal"
        parts = [self.name, f"{state} · {whose}"]
        if recommended:
            parts.append("AI recommended this frame")
        self.caption.setText("   ·   ".join(parts))

    def set_kept(self, kept: bool) -> None:
        self.set_state(kept, False, False)

    def _render(self) -> None:
        if self._pixmap is None:
            self.image.setText("…")
            return
        self.image.setText("")
        if self._one_to_one:
            shown = self._pixmap
        else:
            viewport = self.scroll.viewport().size()
            shown = scaled(
                self._pixmap, max(50, viewport.width() - 2),
                max(50, viewport.height() - 2),
                self.devicePixelRatioF())
        self.image.setPixmap(shown)
        self.image.resize(shown.deviceIndependentSize().toSize())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if not self._one_to_one:
            self._render()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = (
                event.position().toPoint(),
                self.scroll.horizontalScrollBar().value(),
                self.scroll.verticalScrollBar().value())
            self._dragged = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_start is not None and self._one_to_one:
            start, h, v = self._drag_start
            delta = event.position().toPoint() - start
            if delta.manhattanLength() > 4:
                self._dragged = True
            self.scroll.horizontalScrollBar().setValue(h - delta.x())
            self.scroll.verticalScrollBar().setValue(v - delta.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if (event.button() == Qt.MouseButton.LeftButton
                and self._drag_start is not None):
            if not getattr(self, "_dragged", False):
                self._one_to_one = not self._one_to_one
                self._render()
            self._drag_start = None
        super().mouseReleaseEvent(event)


class ReviewPage(QWidget):
    """Clusters on the left, their frames on the right."""

    closed = Signal()
    # The trash worker runs in its own thread; Qt marshals the result back.
    _trash_done = Signal(dict)

    def __init__(self, report: ReportIndex, reviews: ReviewStore,
                 loader: PreviewLoader, parent: QWidget | None = None):
        super().__init__(parent)
        self.report = report
        self.reviews = reviews
        self.loader = loader
        self.frames: dict[str, Frame] = {}
        self._order: list[str] = []
        self._layout_state = (0, 0)
        self._screen_hooked = False
        self.cluster_ids = list(report.cluster_by_id)
        self.current = self.cluster_ids[0] if self.cluster_ids else ""

        self.loader.ready.connect(self._painted)
        self._trash_operation = None
        self._trash_done.connect(self._trash_finished)
        self._cards: dict[str, GroupCard] = {}
        self._card_wants: dict[str, list[tuple[GroupCard, int]]] = {}
        self._index_wants: dict[str, set] = {}
        self._build()
        self._fill_clusters()
        self.show_overview()

    @staticmethod
    def frame_geometry(available: int, count: int, spacing: int) -> tuple[int, int]:
        """How many columns, and how large a photograph, for one width."""
        columns = max(1, count)
        while columns > 1 and (
            (available - spacing * (columns - 1)) / columns < THUMB + 16
        ):
            columns -= 1
        tile = (available - spacing * (columns - 1)) / columns
        thumb = int(max(THUMB, min(THUMB_MAX, tile - 16)))
        return columns, thumb

    def _relayout(self) -> None:
        """Deal the cluster's frames the width the window actually has.

        Few frames on a wide window grow toward the preview's own
        resolution; many frames wrap into as many columns as fit at the
        base size. Recomputed on every resize, so no screen is left with
        a strip of photographs and a plain of empty page.
        """
        names = getattr(self, "_order", [])
        if not names:
            return
        available = self.scroll.viewport().width()
        if available <= 0:
            return
        columns, thumb = self.frame_geometry(
            available, len(names), self.grid.spacing())
        if (columns, thumb) == self._layout_state:
            return
        self._layout_state = (columns, thumb)
        while self.grid.count():
            self.grid.takeAt(0)
        for column_index in range(self.grid.columnCount() + 1):
            self.grid.setColumnStretch(column_index, 0)
        for row_index in range(self.grid.rowCount() + 1):
            self.grid.setRowStretch(row_index, 0)
        rows = (len(names) + columns - 1) // columns
        for position, name in enumerate(names):
            frame = self.frames[name]
            frame.set_scale(thumb)
            self.grid.addWidget(frame, position // columns, position % columns)
        # Leftover width and height pool past the photographs instead of
        # being dealt out between them.
        self.grid.setColumnStretch(columns, 1)
        self.grid.setRowStretch(rows, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._relayout()
        self._relayout_overview()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
        if event.type() == QEvent.Type.Resize:
            if watched is self.overview_scroll.viewport():
                self._relayout_overview()
            elif watched is self.scroll.viewport():
                self._relayout()
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Moving the window to another monitor does not always change its
        # logical size, so a resize hook alone would keep showing the old
        # screen's rendering on the new screen's pixels.
        handle = self.window().windowHandle() if self.window() else None
        if handle is not None and not self._screen_hooked:
            self._screen_hooked = True
            handle.screenChanged.connect(self._screen_changed)
        self._relayout()

    def _screen_changed(self, _screen) -> None:
        for frame in self.frames.values():
            frame.rerender()
        self._layout_state = (0, 0)
        self._relayout()

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.bar = self._bar()
        outer.addWidget(self.bar)

        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)

        self.clusters = QListWidget()
        self.clusters.setObjectName("clusterList")
        self.clusters.setFixedWidth(238)
        # The keyboard belongs to the page: clicking the index must not
        # move the arrows and decision keys onto the list widget.
        self.clusters.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clusters.setIconSize(QSize(206, 60))
        self.clusters.currentRowChanged.connect(self._chose_row)
        split.addWidget(self.clusters)

        right = QWidget()
        right.setObjectName("page")
        column = QVBoxLayout(right)
        column.setContentsMargins(22, 18, 22, 18)
        column.setSpacing(12)

        heading_row = QHBoxLayout()
        heading_row.setSpacing(10)
        self.back_button = QPushButton("\u2190 All groups")
        self.back_button.setObjectName("ghost")
        self.back_button.setFont(theme.body(9))
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.setToolTip(tooltip(
            "Back to every group at once. Your decisions are saved as "
            "you make them."))
        self.back_button.clicked.connect(self.show_overview)
        heading_row.addWidget(self.back_button)
        self.heading = QLabel("")
        self.heading.setObjectName("clusterTitle")
        self.heading.setFont(theme.display(20))
        heading_row.addWidget(self.heading, 1)
        column.addLayout(heading_row)

        self.rationale = QLabel("")
        self.rationale.setObjectName("hint")
        self.rationale.setWordWrap(True)
        self.rationale.setFont(theme.body(10))
        column.addWidget(self.rationale)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.viewport().installEventFilter(self)
        holder = QWidget()
        holder.setObjectName("page")
        self.grid = QGridLayout(holder)
        self.grid.setContentsMargins(0, 6, 0, 6)
        self.grid.setSpacing(14)
        self.scroll.setWidget(holder)
        column.addWidget(self.scroll, 1)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        for label, slot, tip in (
            ("Accept AI  (A)", self.accept_ai, "Take the curator's selection"),
            ("Accept scene  (S)", self.accept_current_scene,
             "Take the proposal for every unreviewed group of this scene. "
             "Groups you decided stay as you left them."),
            ("Keep none  (N)", self.keep_none, "Reject every frame in this cluster"),
            ("Unreviewed  (U)", self.mark_unreviewed, "Undo your decision here"),
        ):
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setFont(theme.body(9))
            button.setToolTip(tooltip(tip))
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setFont(theme.body(9))
        actions.addWidget(self.status)
        column.addLayout(actions)

        overview = QWidget()
        overview.setObjectName("page")
        overview_column = QVBoxLayout(overview)
        overview_column.setContentsMargins(22, 18, 22, 18)
        overview_column.setSpacing(12)
        self.overview_heading = QLabel("")
        self.overview_heading.setObjectName("clusterTitle")
        self.overview_heading.setFont(theme.display(20))
        overview_column.addWidget(self.overview_heading)
        overview_hint = QLabel(
            "Each card is one group of near-duplicates: its frames, and "
            "how many are kept. Open a group to override the choice.")
        overview_hint.setObjectName("hint")
        overview_hint.setWordWrap(True)
        overview_hint.setFont(theme.body(10))
        overview_column.addWidget(overview_hint)
        self.overview_scroll = QScrollArea()
        self.overview_scroll.setWidgetResizable(True)
        self.overview_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The page's own resizeEvent fires before its children re-lay,
        # so column maths read a stale viewport width there. The
        # viewport's own resize is the honest signal.
        self.overview_scroll.viewport().installEventFilter(self)
        overview_holder = QWidget()
        overview_holder.setObjectName("page")
        self.overview_grid = QGridLayout(overview_holder)
        self.overview_grid.setContentsMargins(0, 6, 0, 6)
        self.overview_grid.setSpacing(14)
        self.overview_scroll.setWidget(overview_holder)
        overview_column.addWidget(self.overview_scroll, 1)

        self.views = QStackedWidget()
        self.views.addWidget(overview)
        self.views.addWidget(right)
        self.zoom = ZoomView()
        self.views.addWidget(self.zoom)
        split.addWidget(self.views, 1)
        outer.addLayout(split, 1)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(self.closed)
        layout.addWidget(back)

        self.title = QLabel("")
        self.title.setObjectName("chromeTitle")
        self.title.setFont(theme.display(11))
        layout.addWidget(self.title)
        layout.addStretch(1)

        self.approve_button = QPushButton("Approve the lot")
        self.approve_button.setObjectName("ghost")
        self.approve_button.setFont(theme.body(10))
        self.approve_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.approve_button.setToolTip(tooltip(
            "Accept the proposal for every group you have not reviewed. "
            "Groups you already decided stay exactly as you left them."))
        self.approve_button.clicked.connect(self.approve_the_lot)
        layout.addWidget(self.approve_button)

        self.trash_button = QPushButton("Trash the rejects…")
        self.trash_button.setObjectName("ghost")
        self.trash_button.setFont(theme.body(10))
        self.trash_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.trash_button.clicked.connect(self.trash_rejects)
        layout.addWidget(self.trash_button)

        self.progress = QLabel("")
        self.progress.setObjectName("hint")
        self.progress.setFont(theme.body(9))
        layout.addWidget(self.progress)
        # What this page counts, kept when a shell takes over the chrome.
        self.indicator = self.progress
        return bar

    # --- clusters, in scenes --------------------------------------------

    def scenes(self) -> list[dict]:
        """The review's groups, gathered into scenes.

        Same place, same light: the coarser order above the near-duplicate
        groups, computed by the same rules the suggestion pass shares
        treatments across. One scene means the grouping adds nothing, and
        the list stays flat.
        """
        from opencull_gui.scenes import scene_groups

        entries = [
            {"photo": photo, "cluster_id": cluster_id}
            for cluster_id, cluster in self.report.cluster_by_id.items()
            for photo in cluster.get("photos", [])
        ]
        found = []
        for group in scene_groups(entries, self.reviews.photos_root):
            ordered: list[str] = []
            for photo in group["photos"]:
                for cluster_id, cluster in self.report.cluster_by_id.items():
                    if photo in cluster.get("photos", []):
                        if cluster_id not in ordered:
                            ordered.append(cluster_id)
                        break
            found.append({"id": group["id"], "clusters": ordered})
        return found

    def scene_of(self, cluster_id: str) -> dict | None:
        """The scene the given group belongs to, if scenes apply."""
        for scene in self.scenes():
            if cluster_id in scene["clusters"]:
                return scene
        return None

    def accept_current_scene(self) -> None:
        """Agree with the proposal for the current frame's whole scene."""
        scene = self.scene_of(self.current)
        if scene is None:
            return
        self.accept_scene(list(scene["clusters"]))

    def accept_scene(self, clusters: list[str]) -> None:
        """Agree with the proposal for one scene's unreviewed groups."""
        try:
            written = approve_remaining(
                self.reviews, only_clusters=clusters)
        except ReviewError as exc:
            self._report(str(exc), "alarm")
            return
        self._fill_clusters()
        self.show_cluster(self.current)
        self._report(
            f"Accepted the proposal for {written} group"
            f"{'' if written == 1 else 's'} of that scene.", "ok")

    def _fill_clusters(self) -> None:
        state = self.reviews.public_state()
        self.title.setText(workspace_title(
            self.report, self.reviews.photos_root).upper())
        self.clusters.blockSignals(True)
        self.clusters.clear()
        scenes = self.scenes()
        show_headers = len(scenes) > 1

        def add_cluster(cluster_id: str) -> None:
            reviewed = bool(
                state["clusters"].get(cluster_id, {}).get("reviewed"))
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, cluster_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, reviewed)
            item.setIcon(plain_icon(self._index_pixmap(cluster_id, reviewed)))
            item.setSizeHint(QSize(220, 70))
            item.setToolTip(tooltip(
                "Reviewed." if reviewed
                else "Not reviewed yet; the AI proposal stands."))
            self.clusters.addItem(item)

        if show_headers:
            for scene in scenes:
                number = scene["id"].replace("scene-", "").lstrip("0") or "1"
                count = len(scene["clusters"])
                header = QListWidgetItem(
                    f"SCENE {number} · {count} "
                    f"group{'' if count == 1 else 's'}")
                # A header is a place, not a choice: enabled so it is not
                # dimmed, but never selectable. Accepting a scene lives in
                # the action row below, beside Accept AI, where every other
                # whole-cluster act already lives.
                header.setFlags(Qt.ItemFlag.ItemIsEnabled)
                header.setFont(theme.display(7))
                self.clusters.addItem(header)
                for cluster_id in scene["clusters"]:
                    add_cluster(cluster_id)
        else:
            for cluster_id in self.cluster_ids:
                add_cluster(cluster_id)
        self.clusters.blockSignals(False)
        status = state["status"]
        self.progress.setText(
            f"{status['reviewed_clusters']} of {status['total_clusters']} reviewed")

    def _index_pixmap(self, cluster_id: str, reviewed: bool) -> QPixmap:
        """A small collage that says which group this is by its pictures.

        Reviewed groups carry the safelight border: the same mark the
        kept frames themselves wear, meaning a person has been here.
        """
        width, height = 206, 60
        canvas = QPixmap(width, height)
        canvas.fill(QColor(theme.RAISED))
        photos = self.report.cluster_by_id[cluster_id]["photos"][:3]
        painter = QPainter(canvas)
        slot_width = (width - 2 * (len(photos) - 1)) // max(1, len(photos))
        x = 0
        for name in photos:
            pixmap = self.loader.cached(name, "thumb")
            if pixmap is None:
                self.loader.request(name, "thumb")
                self._index_wants.setdefault(name, set()).add(cluster_id)
            else:
                fitted = scaled(pixmap, slot_width, height)
                painter.drawPixmap(
                    x + (slot_width - fitted.width()) // 2,
                    (height - fitted.height()) // 2, fitted)
            x += slot_width + 2
        if reviewed:
            pen = QPen(QColor(theme.SAFELIGHT))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawRect(1, 1, width - 3, height - 3)
        painter.end()
        return canvas

    def _repaint_index(self, cluster_id: str) -> None:
        row = self._row_of(cluster_id)
        if row < 0:
            return
        item = self.clusters.item(row)
        reviewed = bool(item.data(Qt.ItemDataRole.UserRole + 1))
        item.setIcon(plain_icon(self._index_pixmap(cluster_id, reviewed)))

    def _row_of(self, cluster_id: str) -> int:
        for row in range(self.clusters.count()):
            item = self.clusters.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == cluster_id:
                return row
        return -1

    def _chose_row(self, row: int) -> None:
        item = self.clusters.item(row)
        cluster_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if cluster_id:
            self.show_cluster(str(cluster_id))

    def show_overview(self) -> None:
        """Every group at once, each shown by its own photographs."""
        state = self.reviews.public_state()
        status = state["status"]
        total = int(status["total_clusters"])
        reviewed = int(status["reviewed_clusters"])
        # Before any of it is reviewed, the count of reviews is not the
        # news; the shoot is.
        self.overview_heading.setText(
            f"{total} groups to review" if not reviewed
            else f"{total} groups · {reviewed} reviewed"
            if reviewed < total else f"All {total} groups reviewed")
        while self.overview_grid.count():
            item = self.overview_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()
        self._card_wants.clear()
        for cluster_id in self.cluster_ids:
            cluster = self.report.cluster_by_id[cluster_id]
            decided = state["clusters"].get(cluster_id, {})
            decision = self.report.decision_by_id.get(cluster_id, {})
            keepers = decided.get("keepers")
            if keepers is None:
                keepers = decision.get("photos") or []
            card = GroupCard(
                cluster_id, cluster["photos"], len(keepers),
                bool(decided.get("reviewed")))
            card.chosen.connect(self.show_cluster)
            self._cards[cluster_id] = card
            for slot, name in enumerate(card.sample_names()):
                pixmap = self.loader.request(name, "thumb")
                if pixmap is not None:
                    card.strip.set_frame(slot, pixmap)
                else:
                    self._card_wants.setdefault(name, []).append(
                        (card, slot))
        self._overview_state = (0,)
        self._relayout_overview()
        self.views.setCurrentIndex(0)
        # The overview already shows every group by its pictures; a
        # second list of the same groups says nothing more.
        self.clusters.hide()
        self._fill_clusters()

    def _relayout_overview(self) -> None:
        cards = [self._cards[key] for key in self.cluster_ids
                 if key in self._cards]
        if not cards:
            return
        available = self.overview_scroll.viewport().width()
        if available <= 0:
            return
        spacing = self.overview_grid.spacing()
        columns = max(
            1, (available + spacing) // (GroupCard.WIDTH + spacing))
        columns = int(min(columns, len(cards)))
        if (columns,) == getattr(self, "_overview_state", (0,)):
            return
        self._overview_state = (columns,)
        while self.overview_grid.count():
            self.overview_grid.takeAt(0)
        for column_index in range(self.overview_grid.columnCount() + 1):
            self.overview_grid.setColumnStretch(column_index, 0)
        for row_index in range(self.overview_grid.rowCount() + 1):
            self.overview_grid.setRowStretch(row_index, 0)
        rows = (len(cards) + columns - 1) // columns
        for position, card in enumerate(cards):
            self.overview_grid.addWidget(
                card, position // columns, position % columns)
        self.overview_grid.setColumnStretch(columns, 1)
        self.overview_grid.setRowStretch(rows, 1)

    def show_cluster(self, cluster_id: str) -> None:
        self.views.setCurrentIndex(1)
        self.clusters.show()
        self.current = cluster_id
        self.loader.abandon()
        cluster = self.report.cluster_by_id[cluster_id]
        decision = self.report.decision_by_id.get(cluster_id, {})
        state = self.reviews.public_state()["clusters"].get(cluster_id, {})
        reviewed = bool(state.get("reviewed"))
        # "Reviewed to none" is a decision, and an empty list is its
        # honest record. Only an absent record falls back to the AI --
        # `or` would conflate the two and quietly resurrect the proposal.
        keepers = state.get("keepers")
        if keepers is None:
            keepers = decision.get("photos") or []
        keepers = set(keepers)
        recommended = set(decision.get("photos") or [])

        self.heading.setText(cluster_id)
        confidence = decision.get("confidence")
        rationale = str(decision.get("rationale") or "No rationale recorded.")
        if isinstance(confidence, (int, float)):
            rationale += f"   ·   curator confidence {float(confidence):.0%}"
        self.rationale.setText(rationale)

        while self.grid.count():
            item = self.grid.takeAt(0)
            # Held once: reparenting can release the layout item's own
            # reference, so asking it a second time can answer None.
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.frames.clear()

        self._order = list(cluster["photos"])
        for index, name in enumerate(self._order):
            frame = Frame(name, index, name in keepers, name in recommended)
            frame.toggled.connect(self.toggle)
            frame.inspected.connect(self._inspect)
            self.frames[name] = frame
            pixmap = self.loader.request(name, "thumb")
            if pixmap is not None:
                frame.set_pixmap(pixmap)
        self._layout_state = (0, 0)
        self._relayout()
        self._focus = 0
        self._apply_focus()

        # Rows are found by their data, not their index: scene headers sit
        # between clusters, so index arithmetic would land on the wrong row.
        row = self._row_of(cluster_id)
        if row >= 0 and self.clusters.currentRow() != row:
            self.clusters.blockSignals(True)
            self.clusters.setCurrentRow(row)
            self.clusters.blockSignals(False)
        self._report("" if reviewed else "Not reviewed yet.")
        self.setFocus()

    def _painted(self, name: str, size: str, pixmap) -> None:
        if size == "detail":
            if (self.views.currentIndex() == 2
                    and self.zoom.name == name):
                self.zoom.show_photo(name, pixmap, *self._zoom_state(name))
            return
        if size != "thumb":
            return
        frame = self.frames.get(name)
        if frame is not None:
            frame.set_pixmap(pixmap)
        for card, slot in self._card_wants.pop(name, []):
            card.strip.set_frame(slot, pixmap)
        for cluster_id in self._index_wants.pop(name, set()):
            self._repaint_index(cluster_id)

    # --- decisions ------------------------------------------------------

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _save(self, keepers: list[str], reviewed: bool = True) -> None:
        state = self.reviews.public_state()
        cluster = state["clusters"].get(self.current, {})
        try:
            self.reviews.update_cluster(
                self.current, keepers, cluster.get("note", ""), reviewed,
                state.get("revision"))
        except ReviewError as exc:
            self._report(str(exc), "alarm")
            return
        for name, frame in self.frames.items():
            frame.set_kept(name in set(keepers))
        self._fill_clusters()
        self._report("Saved." if reviewed else "Returned to unreviewed.", "ok")

    def current_keepers(self) -> list[str]:
        return [name for name, frame in self.frames.items() if frame.kept]

    def toggle(self, name: str) -> None:
        frame = self.frames.get(name)
        if frame is None:
            return
        if name in self._order:
            self._focus = self._order.index(name)
            self._apply_focus()
        keepers = set(self.current_keepers())
        keepers.symmetric_difference_update({name})
        order = self.report.cluster_by_id[self.current]["photos"]
        self._save([item for item in order if item in keepers])
        if self.views.currentIndex() == 2 and self.zoom.name == name:
            self._show_zoom(name)
        # A click must not carry the keyboard away with it.
        self.setFocus()

    def toggle_index(self, index: int) -> None:
        order = self.report.cluster_by_id[self.current]["photos"]
        if 0 <= index < len(order):
            self.toggle(order[index])

    # --- the whole cull at once -----------------------------------------

    def approve_the_lot(self) -> None:
        """Accept every proposal nobody has reviewed, in one press."""
        state = self.reviews.public_state()
        status = state.get("status", {})
        total = int(status.get("total_clusters", 0))
        reviewed = int(status.get("reviewed_clusters", 0))
        waiting = total - reviewed
        if waiting <= 0:
            self._report("Every group is already reviewed.", "ok")
            return
        if not self.confirm_approve(waiting, reviewed):
            return
        try:
            written = approve_remaining(self.reviews)
        except ReviewError as exc:
            self._report(str(exc), "alarm")
            return
        self._fill_clusters()
        self.show_cluster(self.current)
        self._report(
            f"Approved the proposal for {written} group"
            f"{'' if written == 1 else 's'}. Your own decisions were not "
            "touched.", "ok")

    def confirm_approve(self, waiting: int, reviewed: int) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Approve every proposal?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"{waiting} group{'' if waiting == 1 else 's'} "
            f"{'is' if waiting == 1 else 'are'} still unreviewed.")
        box.setInformativeText(
            "Approving takes the curator's proposal for each of them."
            + (f" The {reviewed} group{'' if reviewed == 1 else 's'} you "
               "already decided stay exactly as you left them."
               if reviewed else "")
            + " Any group can still be reopened afterwards.")
        approve = box.addButton(
            f"Approve {waiting}", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(approve)
        box.exec()
        return box.clickedButton() is approve

    # --- the rejects ----------------------------------------------------

    def trash_rejects(self) -> None:
        """Move every unselected frame to the system trash, reversibly.

        The first control in this application that moves a photographer's
        files. It refuses to run over an unfinished review, moves only
        the unselected, uses the platform's own trash, and records a
        journal the move can be rolled back from.
        """
        state = self.reviews.public_state()
        status = state.get("status", {})
        total = int(status.get("total_clusters", 0))
        reviewed = int(status.get("reviewed_clusters", 0))
        if reviewed < total:
            self._report(
                f"{total - reviewed} group{'' if total - reviewed == 1 else 's'} "
                "are not reviewed yet. The rejects are only rejects once "
                "every group has been decided.", "alarm")
            return
        try:
            plan = build_plan(
                self.report, self.reviews, self.loader.store, None,
                "effective", "trash", "opensull",
                selection_scope="unselected")
        except ActionError as exc:
            self._report(str(exc), "alarm")
            return
        summary = plan.get("summary", {})
        errors = list(summary.get("errors") or [])
        if errors:
            self._report("; ".join(errors), "alarm")
            return
        count = int(summary.get("files", 0))
        if not self.confirm_trash(count, int(summary.get("bytes", 0))):
            return
        self.trash_button.setEnabled(False)
        self._report(
            f"Moving {count} frame{'' if count == 1 else 's'} to the trash.")
        operation = Operation(
            plan, default_journal_path(self.report, plan),
            on_complete=lambda journal: self._trash_done.emit(dict(journal)))
        self._trash_operation = operation
        operation.start()

    def confirm_trash(self, count: int, size: int) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Move the rejects to the trash?")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"{count} unselected frame{'' if count == 1 else 's'} "
            f"({size / 1_000_000:.0f} MB) would move to the system trash.")
        box.setInformativeText(
            "Only frames no review kept are moved -- never a keeper. They "
            "go in one named batch, to the same trash your file manager "
            "uses, so they can be brought back from there; Darkimiya also "
            "records the batch and can roll the move back itself.\n\n"
            "Nothing is deleted.")
        move = box.addButton(
            f"Move {count} to the trash",
            QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep them where they are",
                             QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is move

    def _trash_finished(self, journal: dict) -> None:
        self.trash_button.setEnabled(True)
        moved = int(journal.get("completed_files", 0))
        # The completion hook fires before the journal's status flips, by
        # design: per-item statuses are already durable. Judge by those.
        done = moved == len(journal.get("items") or []) and not journal.get(
            "error")
        if done:
            self._report(
                f"{moved} frame{'' if moved == 1 else 's'} moved to the "
                "trash, in one batch. They can be brought back from the "
                "trash, or rolled back from the operation journal.", "ok")
        else:
            self._report(
                f"The move stopped: {journal.get('error') or 'unknown'}. "
                f"{moved} moved so far; the journal can resume or roll "
                "back.", "alarm")

    def accept_ai(self) -> None:
        decision = self.report.decision_by_id.get(self.current, {})
        self._save(list(decision.get("photos") or []))

    def keep_none(self) -> None:
        self._save([])

    def mark_unreviewed(self) -> None:
        decision = self.report.decision_by_id.get(self.current, {})
        self._save(list(decision.get("photos") or []), reviewed=False)

    def focus_name(self) -> str:
        if 0 <= self._focus < len(self._order):
            return self._order[self._focus]
        return ""

    def _apply_focus(self) -> None:
        for index, name in enumerate(self._order):
            frame = self.frames.get(name)
            if frame is not None:
                frame.set_focused(index == self._focus)
        name = self.focus_name()
        frame = self.frames.get(name)
        if frame is not None:
            self.scroll.ensureWidgetVisible(frame)
        if self.views.currentIndex() == 2 and name:
            self._show_zoom(name)

    def move_focus(self, delta: int) -> None:
        if not self._order:
            return
        self._focus = max(0, min(len(self._order) - 1, self._focus + delta))
        self._apply_focus()

    def _inspect(self, name: str) -> None:
        if name in self._order:
            self._focus = self._order.index(name)
            self._apply_focus()
            self.zoom_focus()

    def zoom_focus(self) -> None:
        name = self.focus_name()
        if name:
            self._show_zoom(name)
            self.views.setCurrentIndex(2)

    def _zoom_state(self, name: str) -> tuple[bool, bool, bool]:
        state = self.reviews.public_state()["clusters"].get(
            self.current, {})
        decision = self.report.decision_by_id.get(self.current, {})
        return (
            name in set(self.current_keepers()),
            bool(state.get("reviewed")),
            name in set(decision.get("photos") or []),
        )

    def _show_zoom(self, name: str) -> None:
        # The bounded thumb appears at once; the 2400px detail replaces
        # it the moment the loader has decoded it.
        pixmap = (self.loader.cached(name, "detail")
                  or self.loader.request(name, "detail")
                  or self.loader.cached(name, "thumb"))
        self.zoom.show_photo(name, pixmap, *self._zoom_state(name))

    def approve_and_advance(self) -> None:
        """Confirm the group's current selection as reviewed, and move on."""
        self._save(self.current_keepers())
        self.step(1)

    def step(self, delta: int) -> None:
        if not self.cluster_ids:
            return
        row = self.cluster_ids.index(self.current) + delta
        if 0 <= row < len(self.cluster_ids):
            self.show_cluster(self.cluster_ids[row])

    # --- keyboard -------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.views.currentIndex() == 0:
            super().keyPressEvent(event)
            return
        key = event.key()
        zoomed = self.views.currentIndex() == 2
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            index = key - Qt.Key.Key_1
            if 0 <= index < len(self._order):
                if zoomed and self._focus == index:
                    # The key that opened it closes it.
                    self.views.setCurrentIndex(1)
                    self._apply_focus()
                else:
                    self._focus = index
                    self._apply_focus()
                    self.zoom_focus()
        elif key == Qt.Key.Key_A:
            self.accept_ai()
        elif key == Qt.Key.Key_S:
            self.accept_current_scene()
        elif key == Qt.Key.Key_N:
            self.keep_none()
        elif key == Qt.Key.Key_U:
            self.mark_unreviewed()
        elif key == Qt.Key.Key_Z:
            if zoomed:
                self.views.setCurrentIndex(1)
                self._apply_focus()
            else:
                self.zoom_focus()
        elif key == Qt.Key.Key_Right:
            self.move_focus(1)
        elif key == Qt.Key.Key_Left:
            self.move_focus(-1)
        elif key == Qt.Key.Key_Down:
            self.step(1)
            if zoomed:
                # The lightbox persists: the next group opens on its
                # first frame, still zoomed.
                self.zoom_focus()
        elif key == Qt.Key.Key_Up:
            self.step(-1)
            if zoomed:
                self.zoom_focus()
        elif key == Qt.Key.Key_Space:
            name = self.focus_name()
            if name:
                self.toggle(name)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.approve_and_advance()
            if zoomed:
                self.zoom_focus()
        elif key == Qt.Key.Key_Escape:
            if zoomed:
                # Out of the zoom, back to the frames, focus preserved.
                self.views.setCurrentIndex(1)
                self._apply_focus()
            else:
                self.closed.emit()
        else:
            super().keyPressEvent(event)


def photo_root(report_path: Path, photos: Path) -> Path:
    return photos if photos.is_dir() else report_path.parent
