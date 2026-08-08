"""Disagreeing with a treatment, in the treatment's own terms.

A model wrote prose, the compiler turned it into bounded operations, and
those operations are the whole of what the renderer executes. So this page
does not offer a second set of sliders beside them: it shows the operations
themselves, each beside the sentence that produced it, and lets the
photographer move a value inside the range the compiler would have accepted
or switch the operation off.

That keeps two things true at once. The rendering always corresponds to
something readable -- "model asked +0.45 EV, you set +0.30 EV" -- and an
adjusted recipe stays exactly as executable as the one it came from,
because the bounds are the compiler's own.

Guardrails appear here but cannot be moved. They are what the treatment
promised not to do and what the verification pass checks; a photographer
who quietly switched one off would be holding a certificate that judged the
rendering against a claim it no longer makes.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from opencull_gui import adjustments
from opencull_gui.development import DevelopmentWorkspace

from . import theme
from .develop import PROOF_EDGE, PhotoLabel, Renderer
from .previews import PreviewLoader

PHOTO_ROW = 30
TREATMENT_ROW = 34

# Sliders are integers. Every control is carried at this resolution and
# divided back down, which is finer than any of the units are read at.
TICKS = 1000


class Control(QWidget):
    """One compiled operation, and the sentence it came from."""

    changed = Signal(str, dict)

    def __init__(self, control: dict[str, Any]):
        super().__init__()
        self.control = control
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.enabled = QCheckBox(control["label"])
        self.enabled.setChecked(control["enabled"])
        self.enabled.setFont(theme.body(10))
        self.enabled.setToolTip(
            "Switch this operation off entirely. What it was asked to do "
            "stays readable.")
        self.enabled.toggled.connect(self._switched)
        head.addWidget(self.enabled)
        head.addStretch(1)

        self.reading = QLabel(
            adjustments.written(control["value"], control["unit"]))
        self.reading.setObjectName("reading")
        self.reading.setFont(theme.mono(9))
        head.addWidget(self.reading)
        layout.addLayout(head)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, TICKS)
        self.slider.setValue(self._tick(control["value"]))
        self.slider.setEnabled(control["enabled"])
        self.slider.valueChanged.connect(self._moved)
        layout.addWidget(self.slider)

        if control["source"]:
            source = QLabel(f"from: “{control['source']}”")
            source.setObjectName("rowPath")
            source.setWordWrap(True)
            source.setFont(theme.body(8))
            layout.addWidget(source)

        self.provenance = QLabel(adjustments.describe(control))
        self.provenance.setObjectName("provenance")
        self.provenance.setWordWrap(True)
        self.provenance.setFont(theme.body(8))
        layout.addWidget(self.provenance)

    # --- the slider is integers, the control is not ----------------------

    def _tick(self, value: float) -> int:
        low, high = self.control["low"], self.control["high"]
        if high <= low:
            return 0
        return int(round((value - low) / (high - low) * TICKS))

    def _value(self, tick: int) -> float:
        low, high = self.control["low"], self.control["high"]
        return low + (high - low) * tick / TICKS

    def value(self) -> float:
        return self._value(self.slider.value())

    def _moved(self, tick: int) -> None:
        value = self._value(tick)
        self.reading.setText(adjustments.written(value, self.control["unit"]))
        self._retell(value)
        self.changed.emit(self.control["id"], {"value": value})

    def _switched(self, on: bool) -> None:
        self.slider.setEnabled(on)
        self._retell(self.value())
        self.changed.emit(self.control["id"], {"enabled": on})

    def _retell(self, value: float) -> None:
        shown = dict(self.control)
        shown["value"] = value
        shown["enabled"] = self.enabled.isChecked()
        self.provenance.setText(adjustments.describe(shown))
        moved = (not shown["enabled"]
                 or abs(value - self.control["asked"]) > 1e-9)
        self.provenance.setProperty("moved", moved)
        self.provenance.style().unpolish(self.provenance)
        self.provenance.style().polish(self.provenance)


class FineTunePage(QWidget):
    """The operations behind one treatment, and the proof of moving them."""

    closed = Signal()

    def __init__(self, report, workspace: DevelopmentWorkspace,
                 loader: PreviewLoader, pool: QThreadPool | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.report = report
        self.workspace = workspace
        self.loader = loader
        self.photos: list[str] = []
        self.treatments: list[dict] = []
        self.current = ""
        self.treatment = ""
        self.recipe: dict[str, Any] = {}
        self.changes: dict[str, dict] = {}
        self.controls: list[Control] = []

        self.renderer = Renderer(workspace, PROOF_EDGE, pool, self)
        self.renderer.done.connect(self._rendered)
        self.renderer.failed.connect(self._render_failed)

        self._build()
        self.refresh()

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

        self.list = QListWidget()
        self.list.setObjectName("clusterList")
        self.list.setFixedWidth(200)
        self.list.currentRowChanged.connect(self._chose_photo)
        split.addWidget(self.list)

        stage = QFrame()
        stage.setObjectName("pane")
        column = QVBoxLayout(stage)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(8)
        self.caption = QLabel("AS ADJUSTED")
        self.caption.setObjectName("paneCaption")
        self.caption.setFont(theme.display(8))
        column.addWidget(self.caption)
        self.frame = PhotoLabel()
        self.frame.setObjectName("paneImage")
        column.addWidget(self.frame, 1)
        split.addWidget(stage, 1)

        split.addWidget(self._panel())
        outer.addLayout(split, 1)

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

        self.title = QLabel("FINE TUNING")
        self.title.setObjectName("chromeTitle")
        self.title.setFont(theme.display(11))
        layout.addWidget(self.title)
        layout.addStretch(1)

        self.progress = QLabel("")
        self.progress.setObjectName("hint")
        self.progress.setFont(theme.body(9))
        layout.addWidget(self.progress)
        self.indicator = self.progress
        return bar

    def _panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setFixedWidth(360)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        heading = QLabel("TREATMENT")
        heading.setObjectName("eyebrow")
        heading.setFont(theme.display(8))
        layout.addWidget(heading)

        self.treatment_list = QListWidget()
        self.treatment_list.setObjectName("treatmentList")
        self.treatment_list.currentRowChanged.connect(self._chose_treatment)
        layout.addWidget(self.treatment_list)

        self.prompt = QLineEdit()
        self.prompt.setFont(theme.body(10))
        self.prompt.setPlaceholderText(
            "Say it: shadows +12, vignette -8, temperature 5400 kelvin")
        self.prompt.setToolTip(
            "Typed words compile on this machine, through the same grammar "
            "the suggestions use, and move the controls below. No model is "
            "asked and nothing is spent.")
        self.prompt.returnPressed.connect(self.speak)
        layout.addWidget(self.prompt)

        scroll = QScrollArea()
        scroll.setObjectName("controlScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("controls")
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setSpacing(6)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.reset_button = QPushButton("As suggested")
        self.reset_button.setObjectName("ghost")
        self.reset_button.setFont(theme.body(10))
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.setToolTip(
            "Put every control back to what the model asked for.")
        self.reset_button.clicked.connect(self.reset)
        actions.addWidget(self.reset_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.keep_button = QPushButton("Keep this version")
        self.keep_button.setObjectName("primary")
        self.keep_button.setFont(theme.body(10))
        self.keep_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.keep_button.setToolTip(
            "Render at full size and record it as its own version, beside "
            "the treatment it came from.")
        self.keep_button.clicked.connect(self.keep)
        layout.addWidget(self.keep_button)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        layout.addWidget(self.status)
        return panel

    # --- what there is to tune -------------------------------------------

    def refresh(self) -> None:
        payload = self.workspace.payload()
        self.photos = sorted(
            str(item.get("photo")) for item in payload.get("candidates", [])
            if isinstance(item, dict) and item.get("photo"))
        self.list.blockSignals(True)
        self.list.clear()
        for photo in self.photos:
            item = QListWidgetItem(photo)
            item.setSizeHint(QSize(0, PHOTO_ROW))
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.photos:
            self.list.setCurrentRow(0)
            self.show_photo(self.photos[0])
        else:
            self._nothing()

    def _nothing(self) -> None:
        self.progress.setText("")
        self.keep_button.setEnabled(False)
        self.reset_button.setEnabled(False)
        self.frame.set_message(
            "Fine tuning moves the numbers a treatment compiled into, so "
            "there has to be a treatment first. Ask for editing suggestions, "
            "and the frames you asked about appear here.")

    def _chose_photo(self, row: int) -> None:
        if 0 <= row < len(self.photos):
            self.show_photo(self.photos[row])

    def show_photo(self, photo: str) -> None:
        self.current = photo
        self.changes = {}
        self.progress.setText(photo)
        # The baseline compiles to no operations at all, so there is nothing
        # in it to move: it is left out rather than offered and found empty.
        self.treatments = [
            item for item in self.workspace.treatments(photo)
            if item.get("id") != "calibrated"]
        self.treatment_list.blockSignals(True)
        self.treatment_list.clear()
        for item in self.treatments:
            entry = QListWidgetItem(str(item.get("name") or item.get("id")))
            entry.setSizeHint(QSize(0, TREATMENT_ROW))
            self.treatment_list.addItem(entry)
        self.treatment_list.blockSignals(False)
        self.treatment_list.setFixedHeight(
            max(len(self.treatments), 1) * TREATMENT_ROW + 10)
        if self.treatments:
            self.treatment_list.setCurrentRow(0)
            self.show_treatment(str(self.treatments[0].get("id")))
        else:
            self._clear()
            self.keep_button.setEnabled(False)
            self.reset_button.setEnabled(False)
            self.frame.set_message(
                f"{photo} has no treatment with executable operations, so "
                "there is nothing here to move.")

    def _chose_treatment(self, row: int) -> None:
        if 0 <= row < len(self.treatments):
            self.show_treatment(str(self.treatments[row].get("id")))

    def engine(self) -> str:
        return str(self.workspace.decoder_for(self.current).get("engine")
                   or "default")

    def show_treatment(self, treatment: str) -> None:
        self.treatment = treatment
        self.changes = {}
        try:
            self.recipe = self.workspace.compiled_recipe(
                self.current, treatment, self.engine())
        except Exception as exc:                     # noqa: BLE001 - reported
            self.recipe = {}
            self._clear()
            self._report(f"That treatment could not be read: {exc}", "alarm")
            return
        self._show_controls()
        self.render()

    def _clear(self) -> None:
        self.controls = []
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _show_controls(self) -> None:
        self._clear()
        found = adjustments.controls(self.recipe)
        self.keep_button.setEnabled(bool(found))
        self.reset_button.setEnabled(bool(found))
        if not found:
            empty = QLabel(
                "This treatment compiled to no bounded operations, so there "
                "is nothing here to move. What it asked for is on the "
                "suggestions page.")
            empty.setObjectName("hint")
            empty.setWordWrap(True)
            empty.setFont(theme.body(9))
            self.body.addWidget(empty)
            self.body.addStretch(1)
            return
        section = ""
        for control in found:
            if control["section"] != section:
                section = control["section"]
                heading = QLabel(section.upper())
                heading.setObjectName("axisName")
                heading.setFont(theme.display(7))
                self.body.addWidget(heading)
            widget = Control(control)
            widget.changed.connect(self._control_changed)
            self.controls.append(widget)
            self.body.addWidget(widget)
        for text in adjustments.guardrails(self.recipe):
            rail = QLabel(f"◆ {text}")
            rail.setObjectName("guardrail")
            rail.setWordWrap(True)
            rail.setFont(theme.body(8))
            rail.setToolTip(
                "A promise this treatment made, which verification checks. "
                "It is shown rather than offered: switching it off would "
                "leave a certificate judging a claim the rendering no longer "
                "makes.")
            self.body.addWidget(rail)
        self.body.addStretch(1)

    # --- moving them ------------------------------------------------------

    def _control_changed(self, key: str, change: dict) -> None:
        self.changes.setdefault(key, {}).update(change)
        self.render()

    def speak(self) -> None:
        """Move the controls by saying so.

        The words compile locally through the recipe grammar; what was
        heard moves, what was not is said back. Free words a model would
        have to interpret are named as exactly that, not swallowed.
        """
        text = self.prompt.text().strip()
        if not text:
            return
        heard, unheard = adjustments.compile_words(text)
        by_op = {widget.control["op"]: widget for widget in self.controls}
        moved: list[str] = []
        absent: list[str] = []
        for operation in heard:
            widget = by_op.get(str(operation["op"]))
            label = adjustments.LABELS.get(
                str(operation["op"]), str(operation["op"]))
            if widget is None:
                absent.append(label)
                continue
            if str(operation.get("mode")) == "absolute":
                value = float(operation["value"])
            else:
                value = widget.value() + float(operation["value"])
            widget.slider.setValue(
                widget._tick(adjustments.clamp(widget.control, value)))
            moved.append(
                f"{label} {adjustments.written(widget.value(), widget.control['unit'])}")
        parts = []
        if moved:
            parts.append("Moved " + " · ".join(moved) + ".")
        if absent:
            parts.append(
                "This treatment has no "
                + ", ".join(dict.fromkeys(absent))
                + " to move.")
        if unheard:
            parts.append(
                "Not understood: "
                + "; ".join(f"“{phrase}”" for phrase in unheard)
                + " — free words need a model to read them, and that is "
                "not built yet.")
        self._report(" ".join(parts) or "Nothing to do.",
                     "alarm" if (unheard or absent) and not moved else "ok")
        if moved and not unheard:
            self.prompt.clear()

    def reset(self) -> None:
        """Put every control back to what the model asked for."""
        self.changes = {}
        self._show_controls()
        self.render()
        self._report("Back to the treatment as it was suggested.")

    def moved(self) -> int:
        return len(adjustments.moved(
            adjustments.apply(self.recipe, self.changes)))

    def render(self) -> None:
        if not self.current or not self.treatment:
            return
        self.caption.setText(
            "AS ADJUSTED" if self.changes else "AS SUGGESTED")
        self.renderer.render(
            self.current, self.treatment, self.engine(), self._demosaic(),
            adjustments=self.changes or None)

    def _demosaic(self) -> str:
        return str(self.workspace.payload().get("rendering", {}).get(
            "demosaic") or "markesteijn-3-pass")

    def _rendered(self, photo: str, treatment: str, pixmap) -> None:
        if photo != self.current or treatment != self.treatment:
            return
        self.frame.set_source(pixmap)

    def _render_failed(self, photo: str, reason: str) -> None:
        self._report(f"{photo} could not be rendered: {reason}", "alarm")

    def keep(self) -> None:
        """Record this version at full size, beside the one it came from."""
        if not self.current or not self.treatment:
            return
        if not self.changes:
            self._report(
                "Nothing has been moved, so this is the treatment as "
                "suggested. Develop it from the development page.", "alarm")
            return
        self.keep_button.setEnabled(False)
        self._report(
            f"Rendering {self.current} at full size with your adjustments. "
            "It is recorded as its own version, not as the treatment.")
        try:
            record = self.workspace.render_full(
                self.current, self.treatment, self.engine(),
                self._demosaic(), adjustments=self.changes)
        except Exception as exc:                     # noqa: BLE001 - reported
            self.keep_button.setEnabled(True)
            self._report(f"It could not be rendered: {exc}", "alarm")
            return
        self.keep_button.setEnabled(True)
        variant = str((record.get("render") or {}).get("variant") or "")
        self._report(
            f"Kept as {variant}. It is on the export page beside the "
            "treatment it came from.", "ok")

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def step(self, delta: int) -> None:
        if not self.photos:
            return
        row = (self.list.currentRow() + delta) % len(self.photos)
        self.list.setCurrentRow(row)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.closed.emit()
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_J):
            self.step(1)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_K):
            self.step(-1)
        else:
            super().keyPressEvent(event)

    def shutdown(self) -> None:
        self.renderer.shutdown()
