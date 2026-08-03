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
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.report import ReportIndex
from opencull_gui.reviews import ReviewError, ReviewStore

from . import theme
from .previews import PreviewLoader, scaled

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

    def __init__(self, report: ReportIndex, reviews: ReviewStore,
                 loader: PreviewLoader, parent: QWidget | None = None,
                 intent: str = "review"):
        super().__init__(parent)
        self.report = report
        self.reviews = reviews
        self.loader = loader
        self.intent = intent
        self.frames: dict[str, Frame] = {}
        self.cluster_ids = list(report.cluster_by_id)
        self.current = self.cluster_ids[0] if self.cluster_ids else ""

        self.loader.ready.connect(self._painted)
        self._build()
        self._fill_clusters()
        if self.current:
            self.show_cluster(self.current)

    # --- construction ---------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._bar())

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

        self.pending = QLabel(
            "Development controls are not in this window yet. This is the "
            "selection they will work from.")
        self.pending.setObjectName("hint")
        self.pending.setWordWrap(True)
        self.pending.setFont(theme.body(9))
        self.pending.setVisible(self.intent == "develop")
        column.addWidget(self.pending)

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

        self.progress = QLabel("")
        self.progress.setObjectName("hint")
        self.progress.setFont(theme.body(9))
        layout.addWidget(self.progress)
        return bar

    # --- clusters -------------------------------------------------------

    def _fill_clusters(self) -> None:
        state = self.reviews.public_state()
        self.title.setText(self.report.path.stem.removesuffix("-results").upper())
        self.clusters.blockSignals(True)
        self.clusters.clear()
        for cluster_id in self.cluster_ids:
            cluster = self.report.cluster_by_id[cluster_id]
            decided = state["clusters"].get(cluster_id, {})
            kept = len(decided.get("keepers", []) or [])
            mark = "✓" if decided.get("reviewed") else "·"
            item = QListWidgetItem(
                f" {mark}  {cluster_id}   {kept}/{len(cluster['photos'])}")
            item.setData(Qt.ItemDataRole.UserRole, cluster_id)
            self.clusters.addItem(item)
        self.clusters.blockSignals(False)
        status = state["status"]
        self.progress.setText(
            f"{status['reviewed_clusters']} of {status['total_clusters']} reviewed")

    def _chose_row(self, row: int) -> None:
        if 0 <= row < len(self.cluster_ids):
            self.show_cluster(self.cluster_ids[row])

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
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self.frames.clear()

        for index, name in enumerate(cluster["photos"]):
            frame = Frame(name, index, name in keepers, name in recommended)
            frame.toggled.connect(self.toggle)
            self.grid.addWidget(frame)
            self.frames[name] = frame
            pixmap = self.loader.request(name, "thumb")
            if pixmap is not None:
                frame.set_pixmap(pixmap)

        row = self.cluster_ids.index(cluster_id)
        if self.clusters.currentRow() != row:
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
