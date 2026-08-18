"""One button's worth of night sky: trails, or a stack.

The kernel takes ten parameters and eight of them are things the
process already knows -- the folder, its dominant file type, where
outputs conventionally land, the sensor's width. This dialog asks only
what is genuinely the photographer's to say: which of the two pictures
they are after, what lens it was shot with (so the sky's own turning
rate becomes a bound in pixels), and whether there are dark frames.
The answers become an ordinary program run on the ordinary queue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from timelapse_kernel import default_run

from . import theme
from .widgets import tooltip


class SuperimposeDialog(QDialog):
    """Which picture, which lens, and where it lands."""

    def __init__(self, photos: str, parent: QWidget | None = None,
                 selection: list[str] | None = None):
        super().__init__(parent)
        self.photos = str(photos)
        self.selection = [str(name) for name in (selection or [])]
        self.setWindowTitle("Superimpose")
        self.setMinimumWidth(560)
        defaults = default_run(photos)
        self.pattern = str(defaults["pattern"])

        column = QVBoxLayout(self)
        lead = QLabel(
            "Many frames of one sky, laid on each other. Either the "
            "sky's motion draws the picture, or it is taken back out "
            "and the frames are averaged -- which buys the cleanliness "
            "of one long exposure without its blown stars.")
        lead.setWordWrap(True)
        lead.setFont(theme.body(9))
        column.addWidget(lead)

        title = QLabel("THE PICTURE")
        title.setObjectName("eyebrow")
        title.setFont(theme.display(8))
        column.addWidget(title)

        self.trails = QRadioButton(
            "Star trails — brightest wins, frames left where they were")
        self.trails.setChecked(True)
        self.trails.setFont(theme.body(10))
        self.trails.setToolTip(tooltip(
            "The sky's own turning is the subject. Nothing is "
            "registered; each pixel keeps the brightest thing that "
            "ever crossed it."))
        column.addWidget(self.trails)
        self.clipped = QRadioButton(
            "Stack, rejecting strays — registered, averaged, satellites "
            "and aeroplanes left out")
        self.clipped.setFont(theme.body(10))
        self.clipped.setToolTip(tooltip(
            "The frames are registered on the stars and averaged, and "
            "a pixel far from what the stack agrees on is left out of "
            "it -- which is what a satellite, an aeroplane and a "
            "cosmic ray all are."))
        column.addWidget(self.clipped)
        self.drizzle = QRadioButton(
            "Drizzle — a finer grid, for frames that dithered")
        self.drizzle.setFont(theme.body(10))
        self.drizzle.setToolTip(tooltip(
            "Each input pixel is shrunk and dropped on to a finer "
            "grid, sharing its light with whatever output pixels it "
            "actually lands on -- nothing is interpolated, so nothing "
            "is blurred. It repays two things and nothing else: "
            "frames that shifted by FRACTIONS of a pixel between "
            "exposures, and stars so small the sensor could not "
            "sample them properly. On a wide lens with well-sampled "
            "stars it costs four times the memory and buys nothing."))
        column.addWidget(self.drizzle)
        self.average = QRadioButton(
            "Stack, plain average — registered, everything kept")
        self.average.setFont(theme.body(10))
        self.average.setToolTip(tooltip(
            "The same registration, with nothing rejected. Honest for "
            "a clean sequence, and it keeps a meteor."))
        column.addWidget(self.average)

        # Two questions, two groups. Radio buttons sharing a parent are
        # one exclusive group as far as Qt is concerned, so choosing
        # "handheld" silently un-chose "clipped" -- two answers fighting
        # over one slot.
        self._picture = QButtonGroup(self)
        for choice in (self.trails, self.clipped, self.drizzle,
                       self.average):
            self._picture.addButton(choice)

        held_title = QLabel("HOW IT WAS HELD")
        held_title.setObjectName("eyebrow")
        held_title.setFont(theme.display(8))
        column.addWidget(held_title)

        self.tripod = QRadioButton(
            "A tripod — the sky's own turning rate does the work, free")
        self.tripod.setChecked(True)
        self.tripod.setFont(theme.body(10))
        self.tripod.setToolTip(tooltip(
            "On a tripod the only thing moving is the sky, and it "
            "turns at a rate the clock knows. The angle is read off "
            "the timestamps and the shift is searched inside a disc "
            "of known size. No model is asked for anything."))
        column.addWidget(self.tripod)
        self.handheld = QRadioButton(
            "Handheld — a model names a star group to narrow the "
            "search (paid, a handful of calls)")
        self.handheld.setFont(theme.body(10))
        self.handheld.setToolTip(tooltip(
            "A hand moves further than the sky does, so nothing is "
            "bounded and the clock says nothing. Star pairs still "
            "give the roll -- shape survives what a hand does -- and "
            "a model naming a pattern it recognises on a few "
            "keyframes narrows where to look. The stars still settle "
            "it to a fraction of a pixel."))
        column.addWidget(self.handheld)

        self._holding = QButtonGroup(self)
        for choice in (self.tripod, self.handheld):
            self._holding.addButton(choice)

        asks = QFormLayout()
        self.focal = QDoubleSpinBox()
        self.focal.setRange(0.0, 2000.0)
        self.focal.setDecimals(1)
        self.focal.setSuffix(" mm")
        self.focal.setSpecialValueText("unknown")
        self.focal.setValue(0.0)
        self.focal.setToolTip(tooltip(
            "The lens the sky was shot with. With it and the frames' "
            "own timestamps there is an upper bound on how far a star "
            "can have moved between two frames -- so the registration "
            "searches a disc of known size rather than guessing. "
            "Leave it unknown and a generous default is used."))
        asks.addRow("Focal length", self.focal)
        self.sensor = QDoubleSpinBox()
        self.sensor.setRange(1.0, 200.0)
        self.sensor.setDecimals(1)
        self.sensor.setSuffix(" mm")
        self.sensor.setValue(23.5)
        self.sensor.setToolTip(tooltip(
            "The sensor's width. 23.5 is APS-C; full frame is 36; a "
            "medium-format back is larger. Only used with the focal "
            "length, to turn the sky's turning rate into pixels."))
        asks.addRow("Sensor width", self.sensor)

        self.scale = QDoubleSpinBox()
        self.scale.setRange(1.0, 4.0)
        self.scale.setDecimals(1)
        self.scale.setSingleStep(0.5)
        self.scale.setValue(2.0)
        self.scale.setSuffix(" ×")
        self.scale.setToolTip(tooltip(
            "How much finer the output grid is. Two is the usual "
            "answer and costs four times the memory; more than that "
            "wants hundreds of frames to fill honestly."))
        asks.addRow("Drizzle grid", self.scale)
        self.pixfrac = QDoubleSpinBox()
        self.pixfrac.setRange(0.05, 1.0)
        self.pixfrac.setDecimals(2)
        self.pixfrac.setSingleStep(0.05)
        self.pixfrac.setValue(0.8)
        self.pixfrac.setToolTip(tooltip(
            "How far each input pixel is shrunk before it is dropped. "
            "Smaller drops recover more detail in theory and need far "
            "more frames in practice: measured on twenty, 0.5 came "
            "back both noisier and blunter than 0.8, because each "
            "output pixel heard from too few drops."))
        asks.addRow("Drop size", self.pixfrac)

        darks_row = QHBoxLayout()
        darks_row.setSpacing(8)
        self.darks = QLineEdit()
        self.darks.setPlaceholderText("optional — a folder of dark frames")
        self.darks.setFont(theme.body(9))
        self.darks.setToolTip(tooltip(
            "Frames shot with the lens cap on, at the same exposure "
            "and temperature. Their median is what the sensor does "
            "with no light at all, and subtracting it takes the "
            "camera's own fixed pattern out of every frame."))
        darks_row.addWidget(self.darks, 1)
        pick_darks = QPushButton("Choose…")
        pick_darks.setObjectName("ghost")
        pick_darks.setFont(theme.body(9))
        pick_darks.clicked.connect(self._choose_darks)
        darks_row.addWidget(pick_darks)
        asks.addRow("Dark frames", darks_row)

        flats_row = QHBoxLayout()
        flats_row.setSpacing(8)
        self.flats = QLineEdit()
        self.flats.setPlaceholderText("optional — a folder of flat frames")
        self.flats.setFont(theme.body(9))
        self.flats.setToolTip(tooltip(
            "Frames of an evenly lit field -- a dawn sky, a white "
            "screen -- shot at the same focus and aperture. A flat "
            "carries the lens's vignetting, the shadow of every speck "
            "on the sensor, and each pixel's own sensitivity, and "
            "dividing by it takes all three out at once."))
        flats_row.addWidget(self.flats, 1)
        pick_flats = QPushButton("Choose…")
        pick_flats.setObjectName("ghost")
        pick_flats.setFont(theme.body(9))
        pick_flats.clicked.connect(
            lambda: self._choose_into(self.flats, "The flat frames"))
        flats_row.addWidget(pick_flats)
        asks.addRow("Flat frames", flats_row)

        bias_row = QHBoxLayout()
        bias_row.setSpacing(8)
        self.bias = QLineEdit()
        self.bias.setPlaceholderText("optional — a folder of bias frames")
        self.bias.setFont(theme.body(9))
        self.bias.setToolTip(tooltip(
            "The shortest exposure the camera can make, with the cap "
            "on: what the sensor reads before any light or any time. "
            "It comes off the flats and the lights alike."))
        bias_row.addWidget(self.bias, 1)
        pick_bias = QPushButton("Choose…")
        pick_bias.setObjectName("ghost")
        pick_bias.setFont(theme.body(9))
        pick_bias.clicked.connect(
            lambda: self._choose_into(self.bias, "The bias frames"))
        bias_row.addWidget(pick_bias)
        asks.addRow("Bias frames", bias_row)

        where_row = QHBoxLayout()
        where_row.setSpacing(8)
        self.where = QLineEdit(str(
            Path(str(defaults["output"])).parent.parent / "Superimpose"))
        self.where.setFont(theme.body(9))
        where_row.addWidget(self.where, 1)
        pick_where = QPushButton("Choose…")
        pick_where.setObjectName("ghost")
        pick_where.setFont(theme.body(9))
        pick_where.clicked.connect(self._choose_where)
        where_row.addWidget(pick_where)
        asks.addRow("Lands in", where_row)
        column.addLayout(asks)

        self.only_selected = QCheckBox(
            f"Only the {len(self.selection)} frames marked in this project")
        self.only_selected.setFont(theme.body(9))
        self.only_selected.setChecked(bool(self.selection))
        self.only_selected.setEnabled(bool(self.selection))
        column.addWidget(self.only_selected)

        self.judge = QCheckBox(
            "Ask about the frames the star count cannot call (paid, a "
            "few calls)")
        self.judge.setFont(theme.body(9))
        self.judge.setToolTip(tooltip(
            "Cloud hides stars and brightens the sky, and both are "
            "already measured -- so most frames are judged for "
            "nothing. What is left is the band where a threshold is a "
            "coin toss: thin cloud, a brightening sky, haze. Those "
            "frames, and only those, are shown to a model."))
        column.addWidget(self.judge)

        self.demosaic = QCheckBox(
            "Demosaic every frame — slower, and the only honest way to "
            "stack a raw")
        self.demosaic.setFont(theme.body(9))
        self.demosaic.setChecked(True)
        self.demosaic.setToolTip(tooltip(
            "Averaging buys cleanliness only if what is averaged is "
            "the sensor's own reading. Off, the camera's embedded "
            "rendering is used: fast, and already denoised by the "
            "camera in ways that stacking cannot undo."))
        column.addWidget(self.demosaic)

        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        column.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ghost")
        cancel.setFont(theme.body(10))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.go = QPushButton("Superimpose")
        self.go.setFont(theme.body(10))
        self.go.setDefault(True)
        self.go.clicked.connect(self.accept)
        buttons.addWidget(self.go)
        column.addLayout(buttons)

        for choice in (self.trails, self.clipped, self.average,
                       self.tripod, self.handheld):
            choice.toggled.connect(self._retell)
        self._retell()

    # --- small mechanics ---------------------------------------------------

    def _retell(self) -> None:
        """Trails register nothing, so how it was held cannot matter."""
        registering = not self.trails.isChecked()
        self.tripod.setEnabled(registering)
        self.handheld.setEnabled(registering)
        held = registering and self.tripod.isChecked()
        self.focal.setEnabled(held)
        self.sensor.setEnabled(held)
        # Trails average nothing, so a clouded frame cannot poison
        # them; handheld already spends its calls on recognition.
        self.judge.setEnabled(held)
        drizzling = self.drizzle.isChecked()
        self.scale.setEnabled(drizzling)
        self.pixfrac.setEnabled(drizzling)

    def _choose_darks(self) -> None:
        self._choose_into(self.darks, "The dark frames")

    def _choose_into(self, field, title: str) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, title, field.text() or self.photos)
        if chosen:
            field.setText(chosen)

    def _choose_where(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Where the stack lands", self.where.text())
        if chosen:
            self.where.setText(chosen)

    def mode(self) -> str:
        for choice, name in ((self.trails, "trails"),
                             (self.clipped, "clipped"),
                             (self.drizzle, "drizzle")):
            if choice.isChecked():
                return name
        return "average"

    # --- the run -----------------------------------------------------------

    def run_request(self) -> dict[str, Any] | None:
        """The program and its parameters, or None with the reason shown."""
        home = Path(self.where.text().strip()).expanduser()
        parameters = {
            "photos": self.photos,
            "pattern": self.pattern,
            "mode": self.mode(),
            "only": "",
            "darks": self.darks.text().strip(),
            "flats": self.flats.text().strip(),
            "bias": self.bias.text().strip(),
            "sigma": "2.5",
            "scale": str(self.scale.value()),
            "pixfrac": str(self.pixfrac.value()),
            "output": str(home),
            "demosaic": "true" if self.demosaic.isChecked() else "false",
        }
        if self.handheld.isChecked():
            # A different program: the deterministic one has nothing to
            # bound its search with, and asking a model is a decision
            # the photographer makes rather than a fallback taken
            # quietly on their behalf.
            parameters["every"] = "20"
            parameters["proofs_dir"] = str(home / "keyframes")
        else:
            parameters["focal_mm"] = str(self.focal.value())
            parameters["sensor_mm"] = str(self.sensor.value())
            if self.judge.isEnabled() and self.judge.isChecked():
                parameters["proofs_dir"] = str(home / "keyframes")
        if self.only_selected.isChecked() and self.selection:
            # Three hundred names do not fit in a parameter; they ride
            # in a small file beside the run's own outputs.
            chosen = home / "selection.json"
            try:
                home.mkdir(parents=True, exist_ok=True)
                chosen.write_text(json.dumps(
                    {"names": self.selection}), encoding="utf-8")
            except OSError as exc:
                self.status.setText(
                    f"The selection could not be written: {exc}")
                return None
            parameters["only"] = str(chosen)
        if self.handheld.isChecked():
            program = "handheld_stack.kim"
        elif self.judge.isEnabled() and self.judge.isChecked():
            program = "clear_stack.kim"
        else:
            program = "superimpose.kim"
        return {"program": program, "parameters": parameters}
