"""The review page: choose keepers from each cluster of near-duplicates.

This is the work the application exists for, so it is native rather than a
web view held in a window. The report is immutable evidence; every human
decision goes to the review sidecar through ReviewStore, which owns the
revision checking and the atomic write.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
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
from .previews import PreviewLoader, scaled
from .widgets import workspace_title

THUMB = 200


class Frame(QFrame):
    """One photograph in a cluster, kept or not."""

    toggled = Signal(str)

    def __init__(self, name: str, index: int, kept: bool, recommended: bool):
        super().__init__()
        self.name = name
        self.kept = kept
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
            star.setToolTip("Recommended by the curator")
            caption.addWidget(star)
        layout.addLayout(caption)

    def set_pixmap(self, pixmap) -> None:
        self.image.setPixmap(scaled(pixmap, THUMB, THUMB))
        self.image.setText("")

    def set_kept(self, kept: bool) -> None:
        self.kept = kept
        self.setProperty("kept", "true" if kept else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggled.emit(self.name)
        super().mousePressEvent(event)

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
        self.cluster_ids = list(report.cluster_by_id)
        self.current = self.cluster_ids[0] if self.cluster_ids else ""

        self.loader.ready.connect(self._painted)
        self._trash_operation = None
        self._trash_done.connect(self._trash_finished)
        self._build()
        self._fill_clusters()
        if self.current:
            self.show_cluster(self.current)

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
        self.clusters.currentRowChanged.connect(self._chose_row)
        split.addWidget(self.clusters)

        right = QWidget()
        right.setObjectName("page")
        column = QVBoxLayout(right)
        column.setContentsMargins(22, 18, 22, 18)
        column.setSpacing(12)

        self.heading = QLabel("")
        self.heading.setObjectName("clusterTitle")
        self.heading.setFont(theme.display(20))
        column.addWidget(self.heading)

        self.rationale = QLabel("")
        self.rationale.setObjectName("hint")
        self.rationale.setWordWrap(True)
        self.rationale.setFont(theme.body(10))
        column.addWidget(self.rationale)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        self.grid = QHBoxLayout(holder)
        self.grid.setContentsMargins(0, 6, 0, 6)
        self.grid.setSpacing(14)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(holder)
        column.addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        for label, slot, tip in (
            ("Accept AI  (A)", self.accept_ai, "Take the curator's selection"),
            ("Keep none  (N)", self.keep_none, "Reject every frame in this cluster"),
            ("Unreviewed  (U)", self.mark_unreviewed, "Undo your decision here"),
        ):
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setFont(theme.body(9))
            button.setToolTip(tip)
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setFont(theme.body(9))
        actions.addWidget(self.status)
        column.addLayout(actions)

        split.addWidget(right, 1)
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
        self.approve_button.setToolTip(
            "Accept the proposal for every group you have not reviewed. "
            "Groups you already decided stay exactly as you left them.")
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

    def _scene_header(self, scene: dict, state: dict) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(12, 4, 8, 4)
        row.setSpacing(8)
        count = len(scene["clusters"])
        label = QLabel(
            f"{scene['id'].replace('scene-', 'SCENE ').lstrip('0')}"
            f" · {count} group{'' if count == 1 else 's'}")
        label.setObjectName("axisName")
        label.setFont(theme.display(7))
        row.addWidget(label)
        row.addStretch(1)
        waiting = sum(
            1 for cluster_id in scene["clusters"]
            if not (state["clusters"].get(cluster_id) or {}).get("reviewed"))
        if waiting:
            accept = QPushButton("Accept scene")
            accept.setObjectName("ghost")
            accept.setFont(theme.body(8))
            accept.setCursor(Qt.CursorShape.PointingHandCursor)
            accept.setToolTip(
                f"Take the proposal for the {waiting} unreviewed group"
                f"{'' if waiting == 1 else 's'} of this scene. Groups you "
                "decided stay as you left them.")
            accept.clicked.connect(
                lambda _=False, clusters=list(scene["clusters"]):
                self.accept_scene(clusters))
            row.addWidget(accept)
        return holder

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
            cluster = self.report.cluster_by_id[cluster_id]
            decided = state["clusters"].get(cluster_id, {})
            kept = len(decided.get("keepers", []) or [])
            mark = "✓" if decided.get("reviewed") else "·"
            item = QListWidgetItem(
                f" {mark}  {cluster_id}   {kept}/{len(cluster['photos'])}")
            item.setData(Qt.ItemDataRole.UserRole, cluster_id)
            self.clusters.addItem(item)

        if show_headers:
            for scene in scenes:
                header = QListWidgetItem("")
                # A header is a place, not a choice.
                header.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.clusters.addItem(header)
                widget = self._scene_header(scene, state)
                header.setSizeHint(widget.sizeHint())
                self.clusters.setItemWidget(header, widget)
                for cluster_id in scene["clusters"]:
                    add_cluster(cluster_id)
        else:
            for cluster_id in self.cluster_ids:
                add_cluster(cluster_id)
        self.clusters.blockSignals(False)
        status = state["status"]
        self.progress.setText(
            f"{status['reviewed_clusters']} of {status['total_clusters']} reviewed")

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

    def show_cluster(self, cluster_id: str) -> None:
        self.current = cluster_id
        self.loader.abandon()
        cluster = self.report.cluster_by_id[cluster_id]
        decision = self.report.decision_by_id.get(cluster_id, {})
        state = self.reviews.public_state()["clusters"].get(cluster_id, {})
        reviewed = bool(state.get("reviewed"))
        keepers = set(
            state.get("keepers") or (decision.get("photos") or []))
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

        for index, name in enumerate(cluster["photos"]):
            frame = Frame(name, index, name in keepers, name in recommended)
            frame.toggled.connect(self.toggle)
            self.grid.addWidget(frame)
            self.frames[name] = frame
            pixmap = self.loader.request(name, "thumb")
            if pixmap is not None:
                frame.set_pixmap(pixmap)

        # Rows are found by their data, not their index: scene headers sit
        # between clusters, so index arithmetic would land on the wrong row.
        row = self._row_of(cluster_id)
        if row >= 0 and self.clusters.currentRow() != row:
            self.clusters.blockSignals(True)
            self.clusters.setCurrentRow(row)
            self.clusters.blockSignals(False)
        self._report("" if reviewed else "Not reviewed yet.")

    def _painted(self, name: str, size: str, pixmap) -> None:
        frame = self.frames.get(name)
        if frame is not None and size == "thumb":
            frame.set_pixmap(pixmap)

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
        keepers = set(self.current_keepers())
        keepers.symmetric_difference_update({name})
        order = self.report.cluster_by_id[self.current]["photos"]
        self._save([item for item in order if item in keepers])

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

    def step(self, delta: int) -> None:
        if not self.cluster_ids:
            return
        row = self.cluster_ids.index(self.current) + delta
        if 0 <= row < len(self.cluster_ids):
            self.show_cluster(self.cluster_ids[row])

    # --- keyboard -------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            self.toggle_index(key - Qt.Key.Key_1)
        elif key == Qt.Key.Key_A:
            self.accept_ai()
        elif key == Qt.Key.Key_N:
            self.keep_none()
        elif key == Qt.Key.Key_U:
            self.mark_unreviewed()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.step(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.step(-1)
        elif key == Qt.Key.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(event)


def photo_root(report_path: Path, photos: Path) -> Path:
    return photos if photos.is_dir() else report_path.parent
