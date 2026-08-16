"""One button's worth of timelapse: the process fills the geeky parts.

The stabilizer programs take eight parameters each, and six of them
are things the process already knows -- the folder, its dominant file
type, where outputs conventionally land. This dialog asks only what is
genuinely the photographer's to say: what the subject is, what colour
the frames should wear, and (should they care) where it all goes. The
answers become an ordinary program run on the ordinary queue; nothing
here is a second pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from opencull_gui import presets
from timelapse_kernel import default_run

from . import theme
from .widgets import tooltip


class TimelapseDialog(QDialog):
    """What the subject is, what it wears, where it lands."""

    def __init__(self, photos: str, parent: QWidget | None = None,
                 look: str = ""):
        super().__init__(parent)
        self.photos = str(photos)
        self.setWindowTitle("Timelapse")
        self.setMinimumWidth(560)
        defaults = default_run(photos)

        column = QVBoxLayout(self)
        lead = QLabel(
            "The frames were not shot from a tripod, so the subject "
            "drifts. This pins it: every frame cropped to the same size "
            "with the subject at the same place, numbered and ready for "
            "video. Frames that cannot be pinned are set aside by name.")
        lead.setWordWrap(True)
        lead.setFont(theme.body(9))
        column.addWidget(lead)

        subject_title = QLabel("THE SUBJECT")
        subject_title.setObjectName("eyebrow")
        subject_title.setFont(theme.display(8))
        column.addWidget(subject_title)

        self.sun = QRadioButton(
            "The sun (an eclipse) — found by its own shape, free")
        self.sun.setChecked(True)
        self.sun.setFont(theme.body(10))
        column.addWidget(self.sun)
        self.marked = QRadioButton(
            "Something I mark with a box on the first frame — free")
        self.marked.setFont(theme.body(10))
        column.addWidget(self.marked)
        self.named = QRadioButton(
            "Something I name in words — a model anchors it (paid, a "
            "handful of calls)")
        self.named.setFont(theme.body(10))
        column.addWidget(self.named)

        asks = QFormLayout()
        self.box_field = QLineEdit()
        self.box_field.setPlaceholderText("x0, y0, x1, y1 on the first frame")
        self.box_field.setFont(theme.mono(9))
        self.box_field.setToolTip(tooltip(
            "The subject's box in pixels of the first frame, origin "
            "top-left. Read the numbers off any image viewer."))
        asks.addRow("Subject box", self.box_field)
        self.name_field = QLineEdit()
        self.name_field.setPlaceholderText(
            "e.g. the eclipsed sun · the red kite · the lead cyclist")
        self.name_field.setFont(theme.body(10))
        asks.addRow("Subject name", self.name_field)
        self.every = QSpinBox()
        self.every.setRange(2, 500)
        self.every.setValue(30)
        self.every.setToolTip(tooltip(
            "A model looks at the first, the last, and every Nth frame; "
            "free tracking carries the subject between. Smaller N is "
            "more calls and tighter anchoring."))
        asks.addRow("Anchor every", self.every)
        column.addLayout(asks)

        look_title = QLabel("THE LOOK")
        look_title.setObjectName("eyebrow")
        look_title.setFont(theme.display(8))
        column.addWidget(look_title)
        self.look = QComboBox()
        self.look.setFont(theme.body(10))
        self.look.addItem("As shot — no colour change", "")
        for preset in presets.presets():
            short = str(preset["id"]).removeprefix("preset-")
            self.look.addItem(str(preset["name"]), short)
        self.look.addItem("A recipe file of my own…", "…browse…")
        # Open on the look already chosen in Development, so the timelapse
        # wears the treatment the photographer is looking at rather than
        # reverting to no colour change.
        if look:
            at = self.look.findData(look)
            if at >= 0:
                self.look.setCurrentIndex(at)
        self.look.currentIndexChanged.connect(self._chose_look)
        column.addWidget(self.look)
        self._custom_recipe = ""

        where_title = QLabel("WHERE IT LANDS")
        where_title.setObjectName("eyebrow")
        where_title.setFont(theme.display(8))
        column.addWidget(where_title)
        where_row = QHBoxLayout()
        self.where = QLineEdit(str(Path(defaults["frames_dir"]).parent))
        self.where.setFont(theme.mono(9))
        self.where.setToolTip(tooltip(
            "The timelapse folder. The numbered frames go into frames/ "
            "inside it, the report beside them, and the report ends "
            "with the one ffmpeg line that makes the video."))
        where_row.addWidget(self.where, 1)
        browse = QPushButton("Choose…")
        browse.setObjectName("ghost")
        browse.setFont(theme.body(9))
        browse.clicked.connect(self._choose_where)
        where_row.addWidget(browse)
        column.addLayout(where_row)

        self.pattern = defaults["pattern"]
        told = QLabel(
            f"Frames: every {self.pattern} in the folder, in capture order.")
        told.setObjectName("hint")
        told.setFont(theme.body(9))
        column.addWidget(told)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        go = QPushButton("Make the timelapse")
        go.setObjectName("primary")
        go.clicked.connect(self._go)
        buttons.addWidget(go)
        column.addLayout(buttons)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setFont(theme.body(9))
        column.addWidget(self.status)

        self.sun.toggled.connect(self._retell)
        self.marked.toggled.connect(self._retell)
        self.named.toggled.connect(self._retell)
        self._retell()

    # --- small mechanics ---------------------------------------------------

    def _retell(self) -> None:
        self.box_field.setEnabled(self.marked.isChecked())
        self.name_field.setEnabled(self.named.isChecked())
        self.every.setEnabled(self.named.isChecked())

    def _chose_look(self) -> None:
        if self.look.currentData() != "…browse…":
            return
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "A recipe file", str(Path.home()),
            "Darkimiya recipe (*.json)")
        if chosen:
            self._custom_recipe = chosen
            self.look.setItemText(
                self.look.currentIndex(), Path(chosen).name)
        else:
            self.look.setCurrentIndex(0)

    def _choose_where(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "The timelapse folder", self.where.text())
        if chosen:
            self.where.setText(chosen)

    # --- the run -------------------------------------------------------------

    def run_request(self) -> dict[str, Any] | None:
        """The program and its parameters, or None with the reason shown."""
        home = Path(self.where.text().strip()).expanduser()
        recipe = (self._custom_recipe
                  if self.look.currentData() == "…browse…"
                  else str(self.look.currentData() or ""))
        parameters = {
            "photos": self.photos,
            "pattern": self.pattern,
            "frames_dir": str(home / "frames"),
            "recipe": recipe,
            "output": str(home / "timelapse.json"),
        }
        if self.sun.isChecked():
            return {"program": "eclipse_timelapse.kim",
                    "parameters": parameters}
        if self.marked.isChecked():
            box = self.box_field.text().replace(" ", "")
            parts = box.split(",")
            if len(parts) != 4 or not all(
                    part.replace(".", "", 1).replace("-", "", 1).isdigit()
                    for part in parts):
                self.status.setText(
                    "The subject box is four numbers: x0, y0, x1, y1 on "
                    "the first frame.")
                return None
            parameters["subject_box"] = box
            return {"program": "subject_timelapse.kim",
                    "parameters": parameters}
        subject = self.name_field.text().strip()
        if not subject:
            self.status.setText(
                "Name the subject, so the model knows what it is "
                "anchoring.")
            return None
        parameters["subject"] = subject
        parameters["every"] = str(self.every.value())
        return {"program": "named_subject_timelapse.kim",
                "parameters": parameters}

    def _go(self) -> None:
        if self.run_request() is not None:
            self.accept()

