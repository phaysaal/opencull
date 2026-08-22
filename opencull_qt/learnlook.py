"""One button's worth of film simulation: the camera is the teacher.

Fujifilm never published Provia, Velvia or Classic Chrome; the camera
publishes them with every RAW+JPEG frame. This dialog asks only what
is genuinely the photographer's to say -- which folder of pairs, and
what the look should be called -- and the deterministic fit does the
rest, saving the result as a preset every photograph's strip already
knows to offer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (  # noqa: E402  (grouped with kin)
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from timelapse_kernel import default_run

from . import theme
from .widgets import tooltip


class LearnLookDialog(QDialog):
    """Which pairs, and what to call what they teach."""

    def __init__(self, photos: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.photos = str(photos)
        self.setWindowTitle("Learn a look")
        self.setMinimumWidth(520)
        defaults = default_run(photos)

        column = QVBoxLayout(self)
        lead = QLabel(
            "A film simulation, learned from the camera's own hand. "
            "Shoot a handful of varied frames with the simulation set "
            "and RAW+JPEG on; every RAW with a JPEG beside it votes, "
            "and the fit is their agreement. The result is a preset "
            "that lays that rendering on any raw, whatever simulation "
            "it was shot in. Deterministic; no model is asked.")
        lead.setWordWrap(True)
        lead.setFont(theme.body(9))
        column.addWidget(lead)

        asks = QFormLayout()
        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.folder = QLineEdit(self.photos)
        self.folder.setFont(theme.body(9))
        self.folder.setToolTip(tooltip(
            "The folder holding the RAW+JPEG pairs. This project's "
            "own folder to start with; point it at a teaching folder "
            "shot specially -- one simulation per folder."))
        folder_row.addWidget(self.folder, 1)
        pick = QPushButton("Choose…")
        pick.setObjectName("ghost")
        pick.setFont(theme.body(9))
        pick.clicked.connect(self._choose_folder)
        folder_row.addWidget(pick)
        asks.addRow("The pairs", folder_row)

        self.name = QLineEdit("")
        self.name.setPlaceholderText(
            "e.g. Velvia — X-T30, Classic Chrome — X-T5")
        self.name.setFont(theme.body(9))
        self.name.setToolTip(tooltip(
            "What the preset will be called in every strip. Name the "
            "simulation and the camera: the look is theirs."))
        asks.addRow("Call it", self.name)

        self.most = QDoubleSpinBox()
        self.most.setRange(3, 64)
        self.most.setDecimals(0)
        self.most.setValue(12)
        self.most.setToolTip(tooltip(
            "How many pairs may teach. A dozen varied frames beat "
            "fifty of the same wall; clipped frames refuse themselves."))
        asks.addRow("Frames at most", self.most)
        column.addLayout(asks)

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
        self.go = QPushButton("Learn the look")
        self.go.setFont(theme.body(10))
        self.go.setDefault(True)
        self.go.clicked.connect(self.accept)
        buttons.addWidget(self.go)
        column.addLayout(buttons)

        self.pattern = str(defaults["pattern"])

    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "The RAW+JPEG pairs", self.folder.text() or self.photos)
        if chosen:
            self.folder.setText(chosen)

    def run_request(self) -> dict[str, Any] | None:
        """The program and its parameters, or None with the reason shown."""
        folder = Path(self.folder.text().strip()).expanduser()
        if not folder.is_dir():
            self.status.setText("That folder does not exist.")
            return None
        name = " ".join(self.name.text().split())
        if not name:
            self.status.setText(
                "Name the look -- the simulation and the camera.")
            return None
        home = folder / ".darkimiya" / "Recipes"
        slug = "-".join(
            part for part in "".join(
                ch if ch.isalnum() else " " for ch in name.lower()
            ).split()) or "learned-look"
        return {"program": "learn_look.kim", "parameters": {
            "photos": str(folder),
            "pattern": self.pattern,
            "name": name,
            "most_frames": str(int(self.most.value())),
            "output": str(home / f"learned-{slug}.json"),
        }}
