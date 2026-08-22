"""One button's worth of night sky: the finished picture, either way.

The kernel takes many parameters and most of them are things the
process already knows -- the folder, its dominant file type, where
outputs conventionally land, the sensor's width. This dialog asks only
what is genuinely the photographer's to say: which of the two pictures
they are after, what lens it was shot with (so the sky's own turning
rate becomes a bound in pixels), and whether there are calibration
frames. The answer becomes an ordinary program run on the ordinary
queue, and both roads end at a DEVELOPED picture, verified before it
is handed over.

The bare stacking modes this dialog once offered -- the unclipped
average, the drizzle grid, the stack without the develop -- were not
wrong, they were contained: the verified pipeline writes the same
16-bit stack on its way to the show, and the specialist modes remain
as programs in the Studio for the hands that want them.
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
            "Many frames of one sky, made into one finished picture. "
            "Either the frames are registered on their own stars and "
            "stacked -- the cleanliness of one long exposure without "
            "its blown stars -- or the sky's motion is left in and "
            "draws the trails. Both are developed by measurement and "
            "verified before they are handed over.")
        lead.setWordWrap(True)
        lead.setFont(theme.body(9))
        column.addWidget(lead)

        title = QLabel("THE PICTURE")
        title.setObjectName("eyebrow")
        title.setFont(theme.display(8))
        column.addWidget(title)

        self.finished = QRadioButton(
            "The finished starfield — stacked, verified at every "
            "step, developed")
        self.finished.setChecked(True)
        self.finished.setFont(theme.body(10))
        self.finished.setToolTip(tooltip(
            "The whole night pipeline with nobody watching it: "
            "registered with the roll rescue and the field truing, "
            "clouded frames set aside, stacked sigma-clipped in light "
            "-- and REFUSED unless every star appears once. Only then "
            "is it developed, by measurement, with the recipe that won "
            "the blind panels. If any gate fails it abstains and says "
            "why, rather than hand over a ruined picture. The 16-bit "
            "stack lands beside the show for any hand that wants to "
            "develop it differently. No model is asked for anything."))
        column.addWidget(self.finished)
        self.trails = QRadioButton(
            "Star trails — the sky's own turning draws the picture, "
            "finished the same way")
        self.trails.setFont(theme.body(10))
        self.trails.setToolTip(tooltip(
            "The sky's turning is the subject. Nothing is registered; "
            "each pixel keeps the brightest thing that ever crossed "
            "it, which is why a satellite stays and a cloud cannot "
            "poison it. The drawn picture is then developed by the "
            "same measured recipe and held to the same delivery "
            "gates as the starfield."))
        column.addWidget(self.trails)

        self._picture = QButtonGroup(self)
        for choice in (self.finished, self.trails):
            self._picture.addButton(choice)

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
        self.go = QPushButton("Make the picture")
        self.go.setFont(theme.body(10))
        self.go.setDefault(True)
        self.go.clicked.connect(self.accept)
        buttons.addWidget(self.go)
        column.addLayout(buttons)

        for choice in (self.finished, self.trails):
            choice.toggled.connect(self._retell)
        self._retell()

    # --- small mechanics ---------------------------------------------------

    def _retell(self) -> None:
        """The button says which picture it will make."""
        self.go.setText("Draw the trails" if self.trails.isChecked()
                        else "Make the picture")

    def _choose_darks(self) -> None:
        self._choose_into(self.darks, "The dark frames")

    def _choose_into(self, field, title: str) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, title, field.text() or self.photos)
        if chosen:
            field.setText(chosen)

    def _choose_where(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Where the picture lands", self.where.text())
        if chosen:
            self.where.setText(chosen)

    # --- the run -----------------------------------------------------------

    def run_request(self) -> dict[str, Any] | None:
        """The program and its parameters, or None with the reason shown."""
        home = Path(self.where.text().strip()).expanduser()
        parameters = {
            "photos": self.photos,
            "pattern": self.pattern,
            "only": "",
            "darks": self.darks.text().strip(),
            "flats": self.flats.text().strip(),
            "bias": self.bias.text().strip(),
            "focal_mm": str(self.focal.value()),
            "sensor_mm": str(self.sensor.value()),
            "output": str(home),
            "demosaic": "true" if self.demosaic.isChecked() else "false",
        }
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
        program = ("night_trails.kim" if self.trails.isChecked()
                   else "night_show.kim")
        return {"program": program, "parameters": parameters}
