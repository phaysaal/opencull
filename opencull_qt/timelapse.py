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

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
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
                 look: str = "", selection: list[str] | None = None):
        super().__init__(parent)
        self.photos = str(photos)
        self.selection = [str(name) for name in (selection or [])]
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
            "Something I mark with a box on a frame — free")
        self.marked.setFont(theme.body(10))
        column.addWidget(self.marked)
        self.named = QRadioButton(
            "Something I name in words — a model anchors it (paid, a "
            "handful of calls)")
        self.named.setFont(theme.body(10))
        column.addWidget(self.named)

        asks = QFormLayout()
        self.box_field = QLineEdit()
        self.box_field.setPlaceholderText("x0, y0, x1, y1 — or Mark it…")
        self.box_field.setFont(theme.mono(9))
        self.box_field.setToolTip(tooltip(
            "The subject's box in pixels of the frame it was marked "
            "on, origin top-left. Mark it on the picture rather than "
            "typing it."))
        # Which frame the box was drawn on; empty means the first. Typing
        # over the numbers by hand orphans the association, so it clears.
        self._subject_frame = ""
        self.box_field.textEdited.connect(
            lambda _text: setattr(self, "_subject_frame", ""))
        box_row = QHBoxLayout()
        box_row.setSpacing(8)
        box_row.addWidget(self.box_field, 1)
        self.mark_button = QPushButton("Mark it…")
        self.mark_button.setObjectName("ghost")
        self.mark_button.setFont(theme.body(9))
        self.mark_button.setToolTip(tooltip(
            "Walk the frames, then drag a box over the subject; the "
            "numbers fill themselves in, in the frame's own pixels, and "
            "tracking starts at the frame you marked."))
        self.mark_button.clicked.connect(self._mark_box)
        box_row.addWidget(self.mark_button)
        asks.addRow("Subject box", box_row)
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
        self.fps = QSpinBox()
        self.fps.setRange(2, 60)
        self.fps.setValue(12)
        self.fps.setSuffix(" fps")
        self.fps.setToolTip(tooltip(
            "How fast the film plays: frames of the sequence per second "
            "of video. Slower shows more of each frame; 12 is a classic "
            "timelapse pace."))
        self.fps.valueChanged.connect(self._retell_speed)
        asks.addRow("Speed", self.fps)
        column.addLayout(asks)
        self.demosaic = QCheckBox(
            "Ultimate quality — demosaic every frame (much slower)")
        self.demosaic.setFont(theme.body(9))
        self.demosaic.setToolTip(tooltip(
            "Develop each frame from the raw sensor data in sixteen-bit "
            "float before the look is applied, instead of using the "
            "camera's embedded rendering. Extreme moves -- a big kelvin "
            "swing on infrared -- stay clean instead of clipping. Costs "
            "a full raw decode per frame: minutes, not seconds."))
        column.addWidget(self.demosaic)
        self.speed_note = QLabel("")
        self.speed_note.setObjectName("hint")
        self.speed_note.setFont(theme.body(9))
        column.addWidget(self.speed_note)
        # How many frames the folder holds, for saying what a speed means
        # in seconds of film. A count, not a survey: excluded frames make
        # it an estimate, and it says so.
        self._frame_count = len(list(
            Path(self.photos).glob(str(defaults["pattern"]))))
        self._retell_speed()

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
        folder_count = self._frame_count
        # The cull already said which frames matter; the timelapse aims at
        # them by default rather than at everything the folder holds. The
        # whole folder stays one click away, and a folder with no real
        # selection simply is not asked.
        self.only_selected = QCheckBox("")
        self.only_selected.setFont(theme.body(9))
        real = bool(self.selection) and len(self.selection) < folder_count
        if real:
            self.only_selected.setText(
                f"Only the {len(self.selection)} selected photographs "
                f"(untick for all {folder_count})")
            self.only_selected.setChecked(True)
            self.only_selected.toggled.connect(self._retell_speed)
            column.addWidget(self.only_selected)
            self._retell_speed()
        else:
            self.only_selected.setChecked(False)
            self.only_selected.hide()
            told = QLabel(
                f"Frames: every {self.pattern} in the folder, "
                "in capture order.")
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
        self.mark_button.setEnabled(self.marked.isChecked())
        self.name_field.setEnabled(self.named.isChecked())
        self.every.setEnabled(self.named.isChecked())

    def _counted(self) -> int:
        if getattr(self, "only_selected", None) is not None \
                and self.only_selected.isChecked():
            return len(self.selection)
        return self._frame_count

    def _retell_speed(self) -> None:
        counted = self._counted()
        if not counted:
            self.speed_note.setText("")
            return
        seconds = counted / max(self.fps.value(), 1)
        self.speed_note.setText(
            f"≈ {seconds:.0f} seconds of video from about "
            f"{counted} frames.")

    def _mark_box(self) -> None:
        """Open the frames; a dragged rectangle fills the field.

        The sequence often starts with shots the subject is not in yet,
        so the picker walks the frames -- the mark is made wherever the
        subject shows clearly, and the run is told which frame that was
        so tracking starts there and the earlier shots are set aside.
        """
        from timelapse_kernel import list_frames

        try:
            frames = list_frames(self.photos, self.pattern)
        except Exception as exc:                     # noqa: BLE001 - reported
            self.status.setText(f"The frames could not be listed: {exc}")
            return
        if not frames:
            self.status.setText(
                f"No {self.pattern} frames were found in the folder.")
            return
        wanted = (set(self.selection)
                  if self.only_selected.isChecked() and self.selection
                  else None)
        paths = [Path(self.photos) / item["name"] for item in frames
                 if wanted is None or item["name"] in wanted]
        if not paths:
            self.status.setText("The selection holds no frames to mark.")
            return
        picker = BoxPicker(paths, self)
        if picker.exec() == QDialog.DialogCode.Accepted and picker.box:
            self.box_field.setText(", ".join(
                str(value) for value in picker.box))
            self._subject_frame = picker.frame
            self.status.setText(
                f"Box marked on {picker.frame}. Tracking starts there; "
                "earlier frames are set aside.")

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
            "fps": str(self.fps.value()),
            "demosaic": "true" if self.demosaic.isChecked() else "false",
            "only": "",
        }
        if self.only_selected.isChecked() and self.selection:
            # Three hundred names do not fit in a parameter; they ride in
            # a small file beside the run's own outputs.
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
            parameters["subject_frame"] = self._subject_frame
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



def full_box(drag, shown_width: int, shown_height: int,
             full_width: int, full_height: int) -> tuple[int, int, int, int]:
    """A rectangle dragged on the scaled preview, in the frame's own pixels.

    Ordered, scaled back up, and clamped to the frame, so a drag that
    started at either corner or wandered off the edge still names a real
    region. Pure, so the scale-back -- the exact thing that goes wrong
    when numbers are read off a fit-to-window viewer -- is testable.
    """
    scale_x = full_width / max(shown_width, 1)
    scale_y = full_height / max(shown_height, 1)
    x0, x1 = sorted((drag.left(), drag.left() + drag.width()))
    y0, y1 = sorted((drag.top(), drag.top() + drag.height()))
    return (
        max(0, min(round(x0 * scale_x), full_width - 1)),
        max(0, min(round(y0 * scale_y), full_height - 1)),
        max(1, min(round(x1 * scale_x), full_width)),
        max(1, min(round(y1 * scale_y), full_height)),
    )


class _FrameLoader(QThread):
    """One frame's embedded rendering, extracted off the GUI thread."""

    ready = Signal(int, QImage, int, int)   # generation, image, full w, h

    def __init__(self, generation: int, path: Path, fit: tuple,
                 parent=None):
        super().__init__(parent)
        self._generation = generation
        self._path = Path(path)
        self._fit = fit

    def run(self) -> None:
        from timelapse_kernel import _preview

        try:
            image = _preview(self._path)
        except Exception:                            # noqa: BLE001 - shown
            self.ready.emit(self._generation, QImage(), 0, 0)
            return
        full_w, full_h = image.size
        shown = image.copy()
        shown.thumbnail(self._fit)
        data = shown.tobytes("raw", "RGB")
        made = QImage(data, shown.width, shown.height,
                      shown.width * 3, QImage.Format.Format_RGB888).copy()
        self.ready.emit(self._generation, made, full_w, full_h)


class _LoadingFace(QLabel):
    """What the picker shows while a frame is being developed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hint")
        self.setFont(theme.body(10))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "background: rgba(19, 18, 17, 0.75); border-radius: 10px;")

    def say(self, text: str) -> None:
        self.setText(text)

    def show_over(self, canvas: QWidget) -> None:
        self.setGeometry(canvas.rect())
        self.raise_()
        self.show()


class _FrameCanvas(QLabel):
    """The first frame, with one rectangle draggable over it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.drag = None                       # QRect while dragging / kept
        self._down = None

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._down = event.position().toPoint()
        self.drag = QRect(self._down, self._down)
        self.update()

    def mouseMoveEvent(self, event) -> None:   # noqa: N802 - Qt naming
        if self._down is not None:
            self.drag = QRect(self._down, event.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._down is not None:
            self.drag = QRect(self._down, event.position().toPoint())
            self._down = None
            self.update()

    def paintEvent(self, event) -> None:       # noqa: N802 - Qt naming
        super().paintEvent(event)
        if self.drag is None:
            return
        painter = QPainter(self)
        painter.setPen(QPen(QColor(255, 155, 71), 2))
        painter.setBrush(QColor(255, 155, 71, 40))
        painter.drawRect(self.drag.normalized())
        painter.end()


class BoxPicker(QDialog):
    """Drag one box over the subject on the first frame.

    The picture is the same embedded rendering the survey reads, so the
    numbers agree with the tracker by construction; the drag happens on a
    fit-to-window copy and is scaled back to the frame's own pixels --
    the arithmetic nobody should be doing off an image viewer's rulers.
    """

    FIT = (980, 660)

    def __init__(self, frame_paths: list[Path] | Path,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Mark the subject")
        self.box: tuple[int, int, int, int] | None = None
        self.frame = ""
        self._paths = ([Path(frame_paths)] if isinstance(frame_paths, Path)
                       else [Path(item) for item in frame_paths])
        self._at = 0
        self._full = (1, 1)
        self._shown = (1, 1)

        column = QVBoxLayout(self)
        lead = QLabel(
            "Walk to a frame where the subject shows clearly -- the first "
            "shots of a sequence often are not it yet -- then drag a box "
            "over it. Tracking starts at the marked frame; everything "
            "before is set aside.")
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        lead.setFont(theme.body(9))
        column.addWidget(lead)
        self.canvas = _FrameCanvas()
        self.canvas.setMinimumSize(640, 420)
        column.addWidget(self.canvas)
        self._loading = _LoadingFace(self.canvas)
        self._loading.hide()

        walk = QHBoxLayout()
        walk.setSpacing(8)
        for label, step in (("◀◀", -10), ("◀", -1), ("▶", 1), ("▶▶", 10)):
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setFont(theme.body(9))
            button.clicked.connect(
                lambda _checked=False, s=step: self._walk(s))
            walk.addWidget(button)
        self.which = QLabel("")
        self.which.setObjectName("hint")
        self.which.setFont(theme.mono(9))
        walk.addWidget(self.which, 1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        walk.addWidget(cancel)
        use = QPushButton("Use this box")
        use.setObjectName("primary")
        use.clicked.connect(self._use)
        walk.addWidget(use)
        column.addLayout(walk)

        self._generation = 0
        self._loader = None
        self._show(0)

    def _walk(self, step: int) -> None:
        self._show(self._at + step)

    def _show(self, index: int) -> None:
        """Ask for a frame; the decode happens off the interface thread.

        A raw's embedded rendering takes a moment to extract, and a
        window that freezes for it reads as broken. The window stays
        live, the loading face says what is happening, and a result
        arriving for a frame the photographer has already walked past
        is dropped by generation.
        """
        self._at = max(0, min(int(index), len(self._paths) - 1))
        self._generation += 1
        self.which.setText(
            f"frame {self._at + 1} of {len(self._paths)} · "
            f"{self._paths[self._at].name}")
        # A box drawn on another frame means nothing on this one.
        self.canvas.drag = None
        self.canvas.update()
        self._loading.say(f"developing {self._paths[self._at].name}…")
        self._loading.show_over(self.canvas)
        loader = _FrameLoader(
            self._generation, self._paths[self._at], self.FIT, self)
        loader.ready.connect(self._arrived)
        loader.finished.connect(loader.deleteLater)
        self._loader = loader
        loader.start()

    def _arrived(self, generation: int, image: QImage,
                 full_w: int, full_h: int) -> None:
        if generation != self._generation:
            return                                   # walked past it
        self._loading.hide()
        if image.isNull():
            self.which.setText(
                self.which.text() + "  (could not be read)")
            return
        self._full = (full_w, full_h)
        self._shown = (image.width(), image.height())
        pixmap = QPixmap.fromImage(image)
        self.canvas.setPixmap(pixmap)
        self.canvas.setFixedSize(pixmap.size())
        self.canvas.update()

    def _use(self) -> None:
        drag = self.canvas.drag.normalized() if self.canvas.drag else None
        if drag is None or drag.width() < 4 or drag.height() < 4:
            return                              # nothing marked yet
        self.box = full_box(
            drag, self._shown[0], self._shown[1],
            self._full[0], self._full[1])
        self.frame = self._paths[self._at].name
        self.accept()
