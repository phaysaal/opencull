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

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from opencull_gui import adjustments, presets, zones
from opencull_gui.development import DevelopmentWorkspace

from . import theme
from .develop import PROOF_EDGE, PhotoLabel, Renderer
from .previews import PreviewLoader
from .widgets import tooltip
from .zoneslider import ZoneSlider

PHOTO_ROW = 30
TREATMENT_ROW = 34
# Treatments and presets share this list, so it can be thirteen rows long.
# Past six it scrolls rather than growing, because the controls below it
# are what the page is for.
TREATMENT_ROWS_SHOWN = 6

# Sliders are integers. Every control is carried at this resolution and
# divided back down, which is finer than any of the units are read at.
TICKS = 1000


class Control(QWidget):
    """One compiled operation, and the sentence it came from.

    An absent control -- an operation the recipe never used -- sits at
    neutral with its checkbox off; ticking it is the ask that inserts
    the operation. The slider's groove is painted with this frame's
    advice bands: teal safe, amber artistic, red damage.
    """

    changed = Signal(str, dict)
    wanted = Signal(str, float)   # an absent op, asked into existence

    def __init__(self, control: dict[str, Any],
                 bands: dict[str, Any] | None = None):
        super().__init__()
        self.control = control
        self.bands = bands or {}
        self.absent = bool(control.get("absent"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.enabled = QCheckBox(control["label"])
        self.enabled.setChecked(control["enabled"] and not self.absent)
        self.enabled.setFont(theme.body(10))
        self.enabled.setToolTip(tooltip(
            "Not part of this treatment. Tick it, or move the slider, "
            "and it becomes a real operation in the recipe."
            if self.absent else
            "Switch this operation off entirely. What it was asked to do "
            "stays readable."))
        self.enabled.toggled.connect(self._switched)
        head.addWidget(self.enabled)
        head.addStretch(1)

        self.reading = QLabel(
            adjustments.written(control["value"], control["unit"]))
        self.reading.setObjectName("reading")
        self.reading.setFont(theme.mono(9))
        head.addWidget(self.reading)
        layout.addLayout(head)

        self.slider = ZoneSlider()
        self.slider.setRange(0, TICKS)
        self.slider.setValue(self._tick(control["value"]))
        self.slider.setEnabled(control["enabled"] and not self.absent)
        safe = self.bands.get("safe")
        artistic = self.bands.get("artistic")
        if safe and artistic:
            self.slider.set_zones(control["low"], control["high"],
                                  tuple(safe), tuple(artistic))
        self.slider.set_marks(
            control["low"], control["high"],
            None if self.absent else float(control["asked"]),
            float(control.get("neutral", 0.0)))
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
        if self.absent:
            # Moving the slider is as clear an ask as ticking the box.
            self.absent = False
            self.enabled.blockSignals(True)
            self.enabled.setChecked(True)
            self.enabled.blockSignals(False)
            self.slider.setEnabled(True)
            self.wanted.emit(self.control["op"], value)
            return
        self.changed.emit(self.control["id"], {"value": value})

    def _switched(self, on: bool) -> None:
        self.slider.setEnabled(on)
        self._retell(self.value())
        if self.absent:
            if on:
                # Asked into existence at its current position.
                self.absent = False
                self.wanted.emit(self.control["op"], self.value())
            return
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


class GeometrySlider(QWidget):
    """One geometric fact about a mask: a name, a ZoneSlider, a reading."""

    changed = Signal(str, float)

    def __init__(self, key: str, label: str, low: float, high: float,
                 value: float, unit: str = "%"):
        super().__init__()
        self.key = key
        self.low, self.high, self.unit = low, high, unit
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        name = QLabel(label)
        name.setFont(theme.body(9))
        name.setFixedWidth(64)
        row.addWidget(name)
        self.slider = ZoneSlider()
        self.slider.setRange(0, TICKS)
        self.slider.setValue(
            int((value - low) / (high - low) * TICKS) if high > low else 0)
        self.slider.valueChanged.connect(self._moved)
        row.addWidget(self.slider, 1)
        self.reading = QLabel(f"{value:.0f}{unit}")
        self.reading.setObjectName("reading")
        self.reading.setFont(theme.mono(9))
        self.reading.setFixedWidth(48)
        row.addWidget(self.reading)

    def _moved(self, tick: int) -> None:
        value = self.low + (self.high - self.low) * tick / TICKS
        self.reading.setText(f"{value:.0f}{self.unit}")
        self.changed.emit(self.key, value)


class MaskCard(QFrame):
    """One mask: where it lands, and what happens inside it.

    Geometry rides sliders, the shape's discrete facts ride combos, and
    the effects reuse the same Control the whole-frame operations use,
    advice bands included. Every change is emitted as the changes-dict
    keys apply() reads, so the card cannot say anything the renderer
    will not do.
    """

    changed = Signal(str, dict)        # "mask:N" -> {"geometry"/"enabled"}
    effect_changed = Signal(str, dict)  # "mask:N/op" -> {"value"/"enabled"}
    show_me = Signal(str, bool)        # mask id, overlay on or off

    def __init__(self, mask: dict[str, Any],
                 advice: dict[str, dict] | None = None):
        super().__init__()
        self.setObjectName("panelCard")
        self.mask = mask
        geometry = dict(mask["geometry"])
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.enabled = QCheckBox(
            f"Mask · {mask['shape']}")
        self.enabled.setChecked(mask["enabled"])
        self.enabled.setFont(theme.body(10))
        self.enabled.setToolTip(tooltip(
            "Switch this mask off entirely; everything inside it stops."))
        self.enabled.toggled.connect(
            lambda on: self.changed.emit(self.mask["id"], {"enabled": on}))
        head.addWidget(self.enabled)
        head.addStretch(1)
        self.show_button = QPushButton("Show")
        self.show_button.setObjectName("ghost")
        self.show_button.setFont(theme.body(8))
        self.show_button.setCheckable(True)
        self.show_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_button.setToolTip(tooltip(
            "Tint the picture where this mask lands, from the renderer's "
            "own weights. What you see is what will be blended."))
        self.show_button.toggled.connect(
            lambda on: self.show_me.emit(self.mask["id"], on))
        head.addWidget(self.show_button)
        column.addLayout(head)

        where = QLabel(mask["label"])
        where.setObjectName("rowPath")
        where.setWordWrap(True)
        where.setFont(theme.body(8))
        column.addWidget(where)

        shape = mask["shape"]
        if shape == "radial":
            for key, label, low, high in (
                    ("centre_x", "Centre X", 0.0, 100.0),
                    ("centre_y", "Centre Y", 0.0, 100.0),
                    ("radius", "Radius", 1.0, 100.0),
                    ("feather", "Feather", 5.0, 100.0)):
                slider = GeometrySlider(
                    key, label, low, high, float(geometry.get(key, 50)))
                slider.changed.connect(self._geometry_moved)
                column.addWidget(slider)
            self.inverted = QCheckBox("Inverted — everything except it")
            self.inverted.setChecked(bool(geometry.get("inverted")))
            self.inverted.setFont(theme.body(9))
            self.inverted.toggled.connect(
                lambda on: self._geometry_moved("inverted", bool(on)))
            column.addWidget(self.inverted)
        elif shape == "linear":
            self.edge = QComboBox()
            self.edge.addItems(list(adjustments.EDGES))
            self.edge.setCurrentText(str(geometry.get("edge", "bottom")))
            self.edge.setFont(theme.body(9))
            self.edge.currentTextChanged.connect(
                lambda text: self._geometry_moved("edge", text))
            column.addWidget(self.edge)
            reach = GeometrySlider(
                "reach", "Reach", 5.0, 100.0,
                float(geometry.get("reach", 100)))
            reach.changed.connect(self._geometry_moved)
            column.addWidget(reach)
        elif shape == "luma":
            self.band = QComboBox()
            self.band.addItems(list(adjustments.BANDS))
            self.band.setCurrentText(str(geometry.get("band", "shadows")))
            self.band.setFont(theme.body(9))
            self.band.currentTextChanged.connect(
                lambda text: self._geometry_moved("band", text))
            column.addWidget(self.band)

        for effect in mask["effects"]:
            widget = Control(effect,
                             (advice or {}).get(effect["op"]))
            widget.changed.connect(self.effect_changed.emit)
            column.addWidget(widget)
        if not mask["effects"]:
            idle = QLabel("This mask carries no effects; it does nothing.")
            idle.setObjectName("hint")
            idle.setFont(theme.body(8))
            column.addWidget(idle)

    def _geometry_moved(self, key: str, value) -> None:
        self.changed.emit(self.mask["id"], {"geometry": {key: value}})


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
        # Which sections have their absent controls unfolded.
        self._open_sections: dict[str, bool] = {}
        self._mask_cards: list[MaskCard] = []
        # Which mask is being shown as a tint, if any, and the plain
        # pixmap underneath it.
        self._overlay_for = ""
        self._plain_pixmap = None

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
        self.prompt.setToolTip(tooltip(
            "Typed words compile on this machine, through the same grammar "
            "the suggestions use, and move the controls below. No model is "
            "asked and nothing is spent."))
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
        self.reset_button.setToolTip(tooltip(
            "Put every control back to what the model asked for."))
        self.reset_button.clicked.connect(self.reset)
        actions.addWidget(self.reset_button)
        actions.addStretch(1)
        self.preset_button = QPushButton("Save as preset…")
        self.preset_button.setObjectName("ghost")
        self.preset_button.setFont(theme.body(10))
        self.preset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preset_button.setToolTip(tooltip(
            "Keep these adjustments as a look you can apply to any "
            "photograph. The numbers travel; the crop and the "
            "straightening stay with this frame, because they are about "
            "where its subject is."))
        self.preset_button.clicked.connect(self.save_preset)
        actions.addWidget(self.preset_button)
        layout.addLayout(actions)

        self.keep_button = QPushButton("Keep this version")
        self.keep_button.setObjectName("primary")
        self.keep_button.setFont(theme.body(10))
        self.keep_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.keep_button.setToolTip(tooltip(
            "Render at full size and record it as its own version, beside "
            "the treatment it came from."))
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
            min(max(len(self.treatments), 1), TREATMENT_ROWS_SHOWN)
            * TREATMENT_ROW + 10)
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

    def _mask_changed(self, key: str, change: dict) -> None:
        held = self.changes.setdefault(key, {})
        if "geometry" in change:
            held.setdefault("geometry", {}).update(change["geometry"])
        if "enabled" in change:
            held["enabled"] = change["enabled"]
        # The recipe the overlay reads must be the recipe being rendered.
        self.recipe = adjustments.apply(self.recipe, {key: change})
        self.changes[key] = held
        if self._overlay_for == key:
            self._paint_overlay()
        self.render()

    def _add_mask(self) -> None:
        shapes = ["radial — around a point",
                  "linear — from an edge",
                  "luma — a band of brightness"]
        chosen, agreed = QInputDialog.getItem(
            self, "Add a mask", "What is this mask the answer to?",
            shapes, 0, False)
        if not agreed:
            return
        shape = chosen.split(" ", 1)[0]
        made = {"shape": shape,
                "geometry": {"centre_x": 50.0, "centre_y": 50.0,
                             "radius": 30.0, "feather": 100,
                             "reach": 40.0},
                "effects": [{"op": "tone.exposure", "value": 0.0}]}
        self.changes.setdefault("+mask", []).append(made)
        self.recipe = adjustments.apply(self.recipe, {"+mask": [made]})
        self._show_controls()
        self.render()

    def _show_mask(self, key: str, on: bool) -> None:
        """Tint the proof where one mask lands, from the engine's weights."""
        for card in self._mask_cards:
            if card.mask["id"] != key and card.show_button.isChecked():
                card.show_button.blockSignals(True)
                card.show_button.setChecked(False)
                card.show_button.blockSignals(False)
        self._overlay_for = key if on else ""
        if on:
            self._paint_overlay()
        elif self._plain_pixmap is not None:
            self.frame.set_source(self._plain_pixmap)

    def _paint_overlay(self) -> None:
        from PySide6.QtGui import QPainter, QPixmap

        from opencull_gui import maskpaint

        if self._plain_pixmap is None or not self._overlay_for:
            return
        placed = {item["id"]: item for item in adjustments.masks(self.recipe)}
        mask = placed.get(self._overlay_for)
        if mask is None:
            return
        # The value dict, straight off the recipe by ordinal.
        ordinal = int(self._overlay_for.split(":", 1)[1])
        values = [item["value"] for item in
                  self.recipe.get("operations", [])
                  if isinstance(item, dict)
                  and str(item.get("op", "")).startswith("mask.")
                  and str(item.get("op", "")) != "mask.vignette"
                  and isinstance(item.get("value"), dict)]
        if not 1 <= ordinal <= len(values):
            return
        try:
            reference = self.workspace._as_shot_preview(
                self.current, PROOF_EDGE)
            png = maskpaint.overlay_png(
                reference, mask["shape"], values[ordinal - 1])
        except Exception as exc:                     # noqa: BLE001 - shown
            self._report(f"The mask could not be shown: {exc}", "alarm")
            return
        wash = QPixmap()
        wash.loadFromData(png)
        composed = QPixmap(self._plain_pixmap)
        painter = QPainter(composed)
        painter.drawPixmap(composed.rect(), wash, wash.rect())
        painter.end()
        self.frame.set_source(composed)

    def _clear(self) -> None:
        self.controls = []
        self._mask_cards = []
        self._overlay_for = ""
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _show_controls(self) -> None:
        self._clear()
        surface = adjustments.full_surface(self.recipe)
        compiled = [item for item in surface if not item.get("absent")]
        self.keep_button.setEnabled(bool(compiled))
        self.reset_button.setEnabled(bool(compiled))
        advice = zones.load(
            Path(str(self.workspace.project.get("source_folder") or ".")),
            self.current) if self.current else {}
        # A section holds its compiled controls first, then -- folded
        # behind its own count -- everything the renderer could also do.
        # The fold keeps the panel the height of the treatment while the
        # whole instrument stays one click away.
        by_section: dict[str, list[dict]] = {}
        for control in surface:
            by_section.setdefault(control["section"], []).append(control)
        for section, members in by_section.items():
            present = [item for item in members if not item.get("absent")]
            absent = [item for item in members if item.get("absent")]
            heading = QLabel(
                section.upper()
                + (f"  ·  {len(present)}" if present else ""))
            heading.setObjectName("axisName")
            heading.setFont(theme.display(7))
            self.body.addWidget(heading)
            for control in present:
                widget = Control(control, advice.get(control["op"]))
                widget.changed.connect(self._control_changed)
                widget.wanted.connect(self._control_wanted)
                self.controls.append(widget)
                self.body.addWidget(widget)
            if absent:
                if self._open_sections.get(section):
                    for control in absent:
                        widget = Control(control, advice.get(control["op"]))
                        widget.changed.connect(self._control_changed)
                        widget.wanted.connect(self._control_wanted)
                        self.controls.append(widget)
                        self.body.addWidget(widget)
                more = QPushButton(
                    f"{'Fewer' if self._open_sections.get(section) else 'More'}"
                    f" {section.lower()} controls · {len(absent)}")
                more.setObjectName("ghost")
                more.setFont(theme.body(8))
                more.setCursor(Qt.CursorShape.PointingHandCursor)
                more.clicked.connect(
                    lambda _=False, s=section: self._toggle_section(s))
                self.body.addWidget(more)
        placed = adjustments.masks(self.recipe)
        if placed or True:
            heading = QLabel(
                f"MASKS  ·  {len(placed)}" if placed else "MASKS")
            heading.setObjectName("axisName")
            heading.setFont(theme.display(7))
            self.body.addWidget(heading)
        for mask in placed:
            card = MaskCard(mask, advice)
            card.changed.connect(self._mask_changed)
            card.effect_changed.connect(self._control_changed)
            card.show_me.connect(self._show_mask)
            self._mask_cards.append(card)
            self.body.addWidget(card)
        add_mask = QPushButton("Add a mask…")
        add_mask.setObjectName("ghost")
        add_mask.setFont(theme.body(8))
        add_mask.setCursor(Qt.CursorShape.PointingHandCursor)
        add_mask.setToolTip(tooltip(
            "A mask is the answer to two parts of the frame needing "
            "opposite things: radial around a point, linear from an "
            "edge, luma across a band of brightness."))
        add_mask.clicked.connect(self._add_mask)
        self.body.addWidget(add_mask)
        for text in adjustments.guardrails(self.recipe):
            rail = QLabel(f"◆ {text}")
            rail.setObjectName("guardrail")
            rail.setWordWrap(True)
            rail.setFont(theme.body(8))
            rail.setToolTip(tooltip(
                "A promise this treatment made, which verification checks. "
                "It is shown rather than offered: switching it off would "
                "leave a certificate judging a claim the rendering no longer "
                "makes."))
            self.body.addWidget(rail)
        self.body.addStretch(1)

    # --- moving them ------------------------------------------------------

    def _control_changed(self, key: str, change: dict) -> None:
        inserted = next(
            (item for item in self.changes.get("+insert", [])
             if item.get("op") == key), None)
        if inserted is not None and "value" in change:
            # The control was asked in by hand; its value lives on the
            # insert entry, not in a second change that apply() would
            # race it with.
            inserted["value"] = change["value"]
            if change.get("enabled") is False:
                self.changes["+insert"].remove(inserted)
            self.render()
            return
        self.changes.setdefault(key, {}).update(change)
        self.render()

    def _toggle_section(self, section: str) -> None:
        self._open_sections[section] = not self._open_sections.get(section)
        self._show_controls()

    def _control_wanted(self, op: str, value: float) -> None:
        """An absent operation, asked into the recipe by hand.

        The ask rides the changes dict -- the only payload the render
        path carries -- so the renderer, the cache identity and the
        full-size keep all see the same insertion. An earlier shape
        rewrote only this page's copy of the recipe, and the render
        quietly ignored every added control.
        """
        inserts = self.changes.setdefault("+insert", [])
        if not any(item.get("op") == op for item in inserts):
            inserts.append({"op": op, "value": value})
        else:
            for item in inserts:
                if item.get("op") == op:
                    item["value"] = value
        self.recipe = adjustments.apply(
            self.recipe, {"+insert": [{"op": op, "value": value}]})
        self._open_sections[adjustments._section_of(op)] = True
        self._show_controls()
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
        self._plain_pixmap = pixmap
        self.frame.set_source(pixmap)
        if self._overlay_for:
            self._paint_overlay()

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

    # --- keeping a look ---------------------------------------------------

    def preset_operations(self) -> list[dict[str, Any]]:
        """This version's adjustments, as a look rather than as a render.

        The compiled recipe with the photographer's moves folded in is
        exactly what the renderer would execute, which is what makes it
        worth keeping: what gets saved is what they were looking at, not
        the prose that started it.
        """
        if not self.recipe:
            return []
        applied = adjustments.apply(self.recipe, self.changes)
        return presets.portable_operations(applied.get("operations", []))

    def save_preset(self) -> None:
        name, said = self.ask_preset_name()
        if not said:
            return
        try:
            kept = presets.save(
                name, self.preset_operations(),
                intent=f"Kept from the {self._treatment_name()} treatment "
                       f"of {self.current}.",
                origin_note={"photo": self.current,
                             "treatment": self.treatment},
                root=self.workspace.presets_root)
        except presets.PresetError as exc:
            self._report(str(exc), "alarm")
            return
        self._report(
            f"Kept as the preset “{kept['name']}”. It is on every "
            "photograph's treatment list, under Presets.", "ok")

    def ask_preset_name(self) -> tuple[str, bool]:
        suggested = f"{self._treatment_name()} — {Path(self.current).stem}"
        return QInputDialog.getText(
            self, "Save as preset", "Call this look:", text=suggested)

    def _treatment_name(self) -> str:
        return next(
            (str(item["name"]) for item in self.treatments
             if item["id"] == self.treatment), self.treatment)

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
