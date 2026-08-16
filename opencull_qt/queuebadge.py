"""The queue badge: one ring in the chrome that any job fills.

Jobs run on one sequential queue, and their progress used to be scattered
across per-phase pages and one-off dialogs. This is the single always-visible
indicator instead: a ring showing the running job's own progress with a count
of what is queued behind it, and -- on click -- the whole queue with each
job's stage and its completion action.

The badge is deliberately dumb. It is handed a queue snapshot on every poll
and draws it; when the photographer acts on a row it emits a signal and lets
its owner (which holds the job manager) do the work. So the same widget serves
both the projects window and a project's chrome without either knowing how the
other is wired.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

# A job's kind as a person would name it. The timelapse programs are a
# kimiya_program, so their name is read from the program instead.
KIND_LABEL = {
    "culling": "Cull",
    "professional_shortlist": "Assessment",
    "edit_suggestions": "AI editing",
    "treatment": "Treatment",
    "control_zones": "Zone advice",
    "semantic_verification": "Verification",
    "style_profile": "Personal style",
    "development_render": "Render",
    "development_pipeline": "Development",
    "renderer_comparison": "Renderer check",
    "renderer_export": "Render export",
    "delivery_export": "Export",
}
_TIMELAPSE = {
    "eclipse_timelapse.kim", "subject_timelapse.kim",
    "named_subject_timelapse.kim",
}
_ACTIVE = {"running", "stopping", "detached"}
_WAITING = {"queued", "paused"}
_TERMINAL = {"completed", "failed", "cancelled"}


def job_label(job: dict[str, Any]) -> str:
    kind = str(job.get("kind") or "")
    if kind == "kimiya_program":
        program = str(job.get("program") or "")
        if program in _TIMELAPSE:
            return "Timelapse"
        stem = Path(program).stem.replace("_", " ").strip()
        return stem[:1].upper() + stem[1:] if stem else "Program"
    return KIND_LABEL.get(kind, "Job")


def job_detail(job: dict[str, Any]) -> str:
    """A frame name where a job is about one, for the row's subtitle."""
    photo = str(job.get("photo") or "")
    return Path(photo).stem if photo else ""


def completion(job: dict[str, Any]) -> tuple[str, str, str] | None:
    """What a finished job offers: (label, kind, path).

    ``kind`` is "reveal" (show the file/folder) or "log" (open the run's
    log). None means the row shows no action.
    """
    status = str(job.get("status") or "")
    if status == "failed":
        log = str(job.get("log") or "")
        return ("Log", "log", log) if log else None
    if status != "completed":
        return None
    output = str(job.get("output") or "")
    jkind = str(job.get("kind") or "")
    program = str(job.get("program") or "")
    if jkind == "kimiya_program" and program in _TIMELAPSE:
        try:
            report = json.loads(Path(output).read_text(encoding="utf-8"))
            video = str(report.get("video") or "")
        except (OSError, ValueError):
            video = ""
        return ("Show the video", "reveal", video) if video else None
    if jkind in {"delivery_export", "renderer_export"}:
        return ("Open folder", "reveal", output)
    if jkind in {"development_render", "renderer_comparison",
                 "development_pipeline"}:
        return ("Open", "reveal", output) if output else None
    return None


def _draw_ring(painter: QPainter, box: QRectF, *, fraction: float | None,
               angle: int, count: int, done: bool, width: float) -> None:
    """One ring: a faint track, then either an arc for a known fraction, a
    sweep that spins for indeterminate work, or a full ring with a tick."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    inset = width / 2 + 1
    ring = box.adjusted(inset, inset, -inset, -inset)

    track = QColor(theme.EDGE)
    if done:
        track = QColor("#2E5C45")
    painter.setPen(QPen(track, width))
    painter.drawArc(ring, 0, 360 * 16)

    if done:
        pen = QPen(QColor(theme.FIXED), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(ring, 0, 360 * 16)
        _tick(painter, ring)
        return

    accent = QColor(theme.SAFELIGHT)
    if fraction is None:
        accent = QColor(theme.FIXED)
        pen = QPen(accent, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        # A quarter-circle sweep, rotated by the animation angle.
        painter.drawArc(ring, -angle * 16, 90 * 16)
    elif fraction > 0:
        pen = QPen(accent, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        span = int(max(0.0, min(1.0, fraction)) * 360)
        painter.drawArc(ring, 90 * 16, -span * 16)

    if count > 0:
        painter.setPen(QColor(theme.PAPER))
        font = painter.font()
        font.setPointSizeF(max(7.0, box.height() * 0.32))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, str(count))


def _tick(painter: QPainter, ring: QRectF) -> None:
    pen = QPen(QColor(theme.FIXED), ring.width() * 0.09)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    r = ring.width() / 2
    painter.drawPolyline([
        ring.center() + _pt(-r * 0.42, r * 0.02),
        ring.center() + _pt(-r * 0.12, r * 0.34),
        ring.center() + _pt(r * 0.44, -r * 0.32),
    ])


def _pt(dx: float, dy: float):
    from PySide6.QtCore import QPointF

    return QPointF(dx, dy)


class QueueRing(QWidget):
    """A small painted ring, animated while a job is indeterminate."""

    def __init__(self, diameter: int = 30, width: float = 3.0,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._d = diameter
        self._w = width
        self.setFixedSize(diameter, diameter)
        self._fraction: float | None = 0.0
        self._count = 0
        self._done = False
        self._busy = False
        self._angle = 0
        self._spin = QTimer(self)
        self._spin.setInterval(60)
        self._spin.timeout.connect(self._advance)

    def set_state(self, *, fraction: float | None, count: int = 0,
                  done: bool = False) -> None:
        self._fraction = fraction
        self._count = count
        self._done = done
        busy = (fraction is None) and not done
        if busy and not self._spin.isActive():
            self._spin.start()
        elif not busy and self._spin.isActive():
            self._spin.stop()
        self.update()

    def _advance(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        _draw_ring(painter, QRectF(0, 0, self._d, self._d),
                   fraction=self._fraction, angle=self._angle,
                   count=self._count, done=self._done, width=self._w)
        painter.end()


class QueuePopover(QFrame):
    """The queue itself, as a panel under the badge."""

    cancel_wanted = Signal(str)
    resume_wanted = Signal(str)
    reveal_wanted = Signal(str)
    log_wanted = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("queuePopover")
        self.setWindowFlags(Qt.WindowType.Popup)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)

    def rebuild(self, snapshot: dict[str, Any]) -> None:
        while self._outer.count():
            item = self._outer.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        jobs = list(snapshot.get("jobs") or [])
        active = [j for j in jobs if str(j.get("status")) in _ACTIVE]
        waiting = [j for j in jobs if str(j.get("status")) in _WAITING]
        recent = [j for j in jobs if str(j.get("status")) in _TERMINAL][-6:]
        recent.reverse()

        head = QHBoxLayout()
        head.setContentsMargins(14, 12, 14, 8)
        eyebrow = QLabel("QUEUE")
        eyebrow.setObjectName("eyebrow")
        eyebrow.setFont(theme.display(8))
        head.addWidget(eyebrow)
        head.addStretch(1)
        count = QLabel(self._summary(active, waiting))
        count.setObjectName("hint")
        count.setFont(theme.body(9))
        head.addWidget(count)
        holder = QWidget()
        holder.setLayout(head)
        self._outer.addWidget(holder)

        if not active and not waiting and not recent:
            empty = QLabel("Nothing running. Everything is up to date.")
            empty.setObjectName("hint")
            empty.setContentsMargins(14, 4, 14, 14)
            empty.setFont(theme.body(9))
            self._outer.addWidget(empty)

        for job in active + waiting:
            self._outer.addWidget(self._row(job, running=True))
        if recent:
            label = QLabel("RECENT")
            label.setObjectName("bandTitle")
            label.setContentsMargins(14, 8, 14, 4)
            label.setFont(theme.display(7))
            self._outer.addWidget(label)
            for job in recent:
                self._outer.addWidget(self._row(job, running=False))

    @staticmethod
    def _summary(active: list, waiting: list) -> str:
        parts = []
        if active:
            parts.append(f"{len(active)} running")
        if waiting:
            parts.append(f"{len(waiting)} queued")
        return " · ".join(parts) if parts else "idle"

    def _row(self, job: dict[str, Any], *, running: bool) -> QWidget:
        row = QFrame()
        row.setObjectName("queueRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(14, 9, 14, 9)
        line.setSpacing(11)

        status = str(job.get("status") or "")
        progress = job.get("progress") or {}
        fraction = progress.get("fraction")
        line.addWidget(self._indicator(status, fraction))

        body = QVBoxLayout()
        body.setSpacing(1)
        name = QLabel(self._name(job))
        name.setObjectName("rowName")
        name.setFont(theme.body(10))
        body.addWidget(name)
        stage = QLabel(str(progress.get("stage") or job.get("message") or ""))
        stage.setObjectName("hint")
        stage.setFont(theme.body(9))
        body.addWidget(stage)
        holder = QWidget()
        holder.setLayout(body)
        line.addWidget(holder, 1)

        action = self._action(job, status)
        if action is not None:
            line.addWidget(action)
        return row

    @staticmethod
    def _name(job: dict[str, Any]) -> str:
        detail = job_detail(job)
        return f"{job_label(job)} · {detail}" if detail else job_label(job)

    def _indicator(self, status: str, fraction: Any) -> QWidget:
        if status in _ACTIVE:
            ring = QueueRing(20, 2.4)
            try:
                value = float(fraction)
            except (TypeError, ValueError):
                value = 0.0
            ring.set_state(fraction=value if value > 0 else None)
            return ring
        if status == "completed":
            mark = QLabel("✓")
            mark.setObjectName("queueCheck")
            mark.setFixedWidth(20)
            return mark
        dot = QLabel("●")
        dot.setObjectName(
            "queueDotErr" if status in {"failed", "cancelled"}
            else "queueDot")
        dot.setFixedWidth(20)
        return dot

    def _action(self, job: dict[str, Any], status: str) -> QWidget | None:
        job_id = str(job.get("id") or "")
        if status == "paused":
            return self._button("Resume", lambda: self.resume_wanted.emit(job_id))
        if status in _ACTIVE:
            return self._button("Cancel", lambda: self.cancel_wanted.emit(job_id))
        if status == "queued":
            word = QLabel("queued")
            word.setObjectName("hint")
            word.setFont(theme.body(9))
            return word
        done = completion(job)
        if done is None:
            return None
        text, kind, path = done
        if not path:
            return None
        signal = self.log_wanted if kind == "log" else self.reveal_wanted
        primary = kind == "reveal" and status == "completed"
        return self._button(text, lambda: signal.emit(path), primary=primary)

    def _button(self, text: str, on_click, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("queuePrimary" if primary else "queueMini")
        button.setFont(theme.body(9))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: on_click())
        return button


class QueueBadge(QWidget):
    """The clickable ring in the chrome; owns the popover."""

    cancel_wanted = Signal(str)
    resume_wanted = Signal(str)
    reveal_wanted = Signal(str)
    log_wanted = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(38, 38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ring = QueueRing(30, 3.0, self)
        self._ring.move(4, 4)
        self._ring.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._snapshot: dict[str, Any] = {"jobs": []}
        self._was_active = False
        self._done_for = QTimer(self)
        self._done_for.setSingleShot(True)
        self._done_for.setInterval(5000)
        self._done_for.timeout.connect(self._clear_done)
        self._show_done = False

        self._popover = QueuePopover(self)
        for name in ("cancel_wanted", "resume_wanted",
                     "reveal_wanted", "log_wanted"):
            getattr(self._popover, name).connect(getattr(self, name))
        # A row's action has done its job; close the panel behind it.
        for name in ("cancel_wanted", "resume_wanted",
                     "reveal_wanted", "log_wanted"):
            getattr(self._popover, name).connect(
                lambda *_: self._popover.hide())

    def set_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot or {"jobs": []}
        jobs = list(self._snapshot.get("jobs") or [])
        active = [j for j in jobs if str(j.get("status")) in _ACTIVE]
        waiting = [j for j in jobs if str(j.get("status")) in _WAITING]

        if self._was_active and not active and not waiting:
            # The queue just went quiet: a brief tick, then back to idle.
            self._show_done = True
            self._done_for.start()
        if active or waiting:
            self._show_done = False
            self._done_for.stop()
        self._was_active = bool(active or waiting)

        if active:
            fraction = active[0].get("progress", {}).get("fraction")
            try:
                value = float(fraction)
            except (TypeError, ValueError):
                value = 0.0
            self._ring.set_state(
                fraction=value if value > 0 else None, count=len(waiting))
        elif waiting:
            self._ring.set_state(fraction=None, count=len(waiting))
        elif self._show_done:
            self._ring.set_state(fraction=1.0, done=True)
        else:
            self._ring.set_state(fraction=0.0)

        self.setToolTip(self._tooltip(active, waiting))
        if self._popover.isVisible():
            self._popover.rebuild(self._snapshot)
            self._popover.adjustSize()

    @staticmethod
    def _tooltip(active: list, waiting: list) -> str:
        if not active and not waiting:
            return "Queue — nothing running"
        bits = []
        if active:
            bits.append(f"{len(active)} running")
        if waiting:
            bits.append(f"{len(waiting)} queued")
        return "Queue — " + ", ".join(bits)

    def _clear_done(self) -> None:
        self._show_done = False
        self.set_snapshot(self._snapshot)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._popover.isVisible():
            self._popover.hide()
            return
        self._popover.rebuild(self._snapshot)
        self._popover.adjustSize()
        self._popover.setFixedWidth(370)
        corner = self.mapToGlobal(self.rect().bottomRight())
        self._popover.move(corner.x() - self._popover.width(), corner.y() + 6)
        self._popover.show()
