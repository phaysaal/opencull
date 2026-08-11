"""The assessment page: read what the models saw, and say which frames matter.

This is the second of the two human gates. The cull decides which frames
survive; this decides which of the survivors are worth developing. Only the
frames marked here reach the suggestion pass, so the mark is the gate on
everything downstream -- and on what the downstream costs.

The assessment itself is evidence and is never edited. What a person writes
here goes to the shortlist review sidecar beside it, the same separation the
culling report and its review already keep.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from opencull_gui.scenes import photos_root_of
from opencull_gui.shortlist import (
    ASSESSMENT_FIELDS,
    rated_by_hand,
    score_disagreements,
    settled_order,
    standing,
    tier_rank,
    tier_stars,
)
from opencull_gui.shortlist_reviews import ShortlistReviewError

from . import theme
from .develop import PhotoLabel
from .previews import PreviewLoader
from .sheet import ContactSheet
from .suggestions import ask_suggestion_scope, launch_suggestions

ROW = 32

# The models' own ranking, strongest first. A tier is a judgement, so it is
# shown as a word rather than only as a colour.
TIER_ORDER = ("exceptional", "strong", "promising", "ordinary", "reject")

# What each assessment axis is called when a person reads it.
AXIS_LABELS = {
    "composition": "Composition",
    "angle_and_perspective": "Angle and perspective",
    "subject_presentation": "Subject",
    "pose_and_expression": "Pose and expression",
    "moment_and_emotion": "Moment",
    "light_and_tonality": "Light and tonality",
    "surroundings": "Surroundings",
    "irrecoverable_defects": "Irrecoverable defects",
    "raw_editing_opportunities": "RAW editing opportunities",
    "distinctiveness": "Distinctiveness",
}


class _WrappedReading(QLabel):
    """A wrapped label that admits how tall its wrapping makes it.

    A plain word-wrapped QLabel reports the height of one line however
    many it draws, so anything sized from it reserves one line and cuts
    the rest off. Everything above this label -- the axis, the band,
    the page -- is sized from it, so it has to tell the truth.
    """

    def __init__(self, text: str):
        super().__init__(text)
        self.setWordWrap(True)
        policy = QSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def sizeHint(self):  # noqa: N802 - Qt naming
        hint = super().sizeHint()
        width = self.width() or hint.width()
        return QSize(hint.width(), self.heightForWidth(width))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.setMinimumHeight(self.heightForWidth(self.width()))
        self.updateGeometry()


class Axis(QWidget):
    """One line of the assessment: what was looked at, and what was seen."""

    def __init__(self, label: str, text: str):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        heading = QLabel(label.upper())
        heading.setObjectName("axisName")
        heading.setFont(theme.display(7))
        layout.addWidget(heading)

        body = _WrappedReading(text)
        body.setObjectName("axisBody")
        body.setFont(theme.body(9))
        layout.addWidget(body)


class ShortlistPage(QWidget):
    """Assessed frames on the left, one frame's evidence on the right."""

    closed = Signal()
    suggested = Signal(str, list)   # output path, photographs to ask about
    why_wanted = Signal(str)        # the frame whose story is asked for
    reassess_wanted = Signal()      # re-run the assessment from scratch
    reask_wanted = Signal(str)      # ask again about one photograph

    def __init__(self, shortlist, reviews, loader: PreviewLoader,
                 directions=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.shortlist = shortlist
        self.reviews = reviews
        self.loader = loader
        self.directions = directions
        self.entries = list(shortlist.entries)
        self.by_hand = rated_by_hand(shortlist)
        self.current = str(self.entries[0]["photo"]) if self.entries else ""
        self._icon_wants: dict[str, bool] = {}

        self.loader.ready.connect(self._painted)
        self._build()
        self._fill_entries()
        # The detail is filled for the first frame so it is ready the
        # moment one is opened; the page still lands on the grid.
        if self.current:
            self.show_entry(self.current)
        self.show_overview()

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
        self.list.setIconSize(QSize(96, 58))
        self.list.setObjectName("clusterList")
        self.list.setFixedWidth(330)
        self.list.currentRowChanged.connect(self._chose_row)
        split.addWidget(self.list)

        right = QWidget()
        right.setObjectName("page")
        column = QVBoxLayout(right)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        # The photograph is what is being judged, so it gets the room. The
        # judgement sits beside it in a column narrow enough to read, rather
        # than under a thumbnail with half the window left empty.
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        stage = QFrame()
        stage.setObjectName("pane")
        stage_column = QVBoxLayout(stage)
        stage_column.setContentsMargins(14, 12, 14, 12)
        stage_column.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.heading = QLabel("")
        self.heading.setObjectName("clusterTitle")
        self.heading.setFont(theme.display(17))
        head.addWidget(self.heading)
        why = QPushButton("Why?")
        why.setObjectName("ghost")
        why.setFont(theme.body(9))
        why.setCursor(Qt.CursorShape.PointingHandCursor)
        why.setToolTip(
            "The recorded story of this frame: culled, reviewed, assessed, "
            "rated. Nothing is computed; nothing is asked.")
        why.clicked.connect(lambda: self.why_wanted.emit(self.current))
        back_to_grid = QPushButton("← All frames")
        back_to_grid.setObjectName("ghost")
        back_to_grid.setFont(theme.body(9))
        back_to_grid.setCursor(Qt.CursorShape.PointingHandCursor)
        back_to_grid.setToolTip(
            "Back to every frame at once. Your ratings are saved as you "
            "make them.")
        back_to_grid.clicked.connect(
            lambda _checked=False: self.show_overview())
        head.insertWidget(0, back_to_grid)
        head.addWidget(why)
        self.reask_button = QPushButton("Assess this frame again")
        self.reask_button.setObjectName("ghost")
        self.reask_button.setFont(theme.body(9))
        self.reask_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reask_button.setToolTip(
            "Ask the models about this one photograph again. Every other "
            "frame's rating is kept, so this costs one frame.")
        self.reask_button.clicked.connect(
            lambda _checked=False: self.reask_wanted.emit(self.current))
        head.addWidget(self.reask_button)
        if not self.by_hand:
            self.detail_button = QPushButton("\U0001F441  Why this rating")
            self.detail_button.setObjectName("ghost")
            self.detail_button.setFont(theme.body(9))
            self.detail_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.detail_button.setToolTip(
                "The model's full response for this frame: every axis it "
                "judged, the score, and its reason.")
            self.detail_button.clicked.connect(self._show_assessment_detail)
            head.addWidget(self.detail_button)
        head.addStretch(1)
        stage_column.addLayout(head)
        self.frame = PhotoLabel()
        self.frame.setObjectName("paneImage")
        stage_column.addWidget(self.frame, 1)

        panel = QFrame()
        panel.setObjectName("panel")
        # A band beneath the photograph rather than a column beside it:
        # the frame is what is being judged, so it gets the room, and
        # the reading is read across rather than down a narrow gutter.
        # The band takes the height its own readings need -- a reading
        # cut off at the fold is not a reading -- and the photograph
        # keeps whatever is left, which on any usual window is most of
        # it.
        panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        judgement = QVBoxLayout(panel)
        judgement.setContentsMargins(18, 12, 12, 12)
        judgement.setSpacing(6)

        self.verdict = QLabel("")
        self.verdict.setObjectName("rowState")
        self.verdict.setWordWrap(True)
        self.verdict.setFont(theme.display(9))
        judgement.addWidget(self.verdict)

        self.unread_note = QLabel("")
        self.unread_note.setObjectName("status")
        self.unread_note.setProperty("tone", "alarm")
        self.unread_note.setWordWrap(True)
        self.unread_note.setFont(theme.body(9))
        self.unread_note.hide()
        judgement.addWidget(self.unread_note)

        self.rationale = QLabel("")
        self.rationale.setObjectName("hint")
        self.rationale.setWordWrap(True)
        self.rationale.setFont(theme.body(10))
        judgement.addWidget(self.rationale)

        scroll = QScrollArea()
        scroll.setObjectName("controlScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Without this the holder grows to its widest child and every wrapped
        # paragraph then lays out at that width, off screen.
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The band is sized by what it holds, so the readings are shown
        # whole instead of scrolled; only a window too short for both
        # falls back to scrolling.
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setSizeAdjustPolicy(
            QScrollArea.SizeAdjustPolicy.AdjustToContents)
        holder = QWidget()
        holder.setObjectName("controls")
        # Ten readings across a full-width band read as columns, not as
        # one long scroll down a gutter.
        self.axes = QGridLayout(holder)
        self.axes.setContentsMargins(0, 4, 8, 4)
        self.axes.setHorizontalSpacing(22)
        self.axes.setVerticalSpacing(8)
        scroll.setWidget(holder)
        judgement.addWidget(scroll, 1)
        self._reading_scroll = scroll
        self._reading_holder = holder

        body.addWidget(stage, 1)
        body.addWidget(panel)

        column.addLayout(body, 1)
        decision = self._decision()
        decision.setContentsMargins(22, 0, 22, 0)
        column.addWidget(decision)

        overview = QWidget()
        overview.setObjectName("page")
        overview_column = QVBoxLayout(overview)
        overview_column.setContentsMargins(22, 18, 22, 18)
        overview_column.setSpacing(10)
        self.overview_heading = QLabel("")
        self.overview_heading.setObjectName("clusterTitle")
        self.overview_heading.setFont(theme.display(18))
        overview_column.addWidget(self.overview_heading)
        self.overview_tally = QLabel("")
        self.overview_tally.setObjectName("hint")
        self.overview_tally.setFont(theme.body(10))
        overview_column.addWidget(self.overview_tally)
        overview_hint = QLabel(
            "Open a frame to read its assessment, or the eye to see why "
            "the model rated it as it did. Mark the ones worth "
            "developing: the editing pass reads exactly those."
            if not self.by_hand else
            "Open a frame to rate it. Mark the ones worth developing: "
            "the editing pass reads exactly those.")
        overview_hint.setObjectName("hint")
        overview_hint.setWordWrap(True)
        overview_hint.setFont(theme.body(10))
        overview_column.addWidget(overview_hint)
        self._sheet_slot = QVBoxLayout()
        overview_column.addLayout(self._sheet_slot, 1)
        self.sheet = None

        self.views = QStackedWidget()
        self.views.addWidget(overview)
        self.views.addWidget(right)
        split.addWidget(self.views, 1)
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

        self.title = QLabel("ASSESSMENT")
        self.title.setObjectName("chromeTitle")
        self.title.setFont(theme.display(11))
        layout.addWidget(self.title)
        layout.addStretch(1)

        # By hand there is no model rating to reassess and no scores to
        # sort by, so these belong only to a model-assessed shortlist.
        if not self.by_hand:
            sort_label = QLabel("Sort")
            sort_label.setObjectName("hint")
            sort_label.setFont(theme.body(9))
            layout.addWidget(sort_label)
            self.sort_by = QComboBox()
            self.sort_by.setFont(theme.body(9))
            self.sort_by.addItem("Standing", "rating")
            self.sort_by.addItem("Time", "time")
            self.sort_by.currentIndexChanged.connect(
                lambda _i: (self._fill_entries(), self.show_overview()))
            layout.addWidget(self.sort_by)

            self.reassess_button = QPushButton("Reassess")
            self.reassess_button.setObjectName("ghost")
            self.reassess_button.setFont(theme.body(9))
            self.reassess_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.reassess_button.setToolTip(
                "Ask the models to assess this selection again from "
                "scratch. Your own ratings and marks are kept.")
            self.reassess_button.clicked.connect(self.reassess_wanted)
            layout.addWidget(self.reassess_button)

        self.progress = QLabel("")
        self.progress.setObjectName("hint")
        self.progress.setFont(theme.body(9))
        layout.addWidget(self.progress)
        self.indicator = self.progress
        return bar

    def _decision(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("decision")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.interesting = QCheckBox("Worth developing  (I)")
        self.interesting.setObjectName("gate")
        self.interesting.setFont(theme.body(10))
        self.interesting.setCursor(Qt.CursorShape.PointingHandCursor)
        self.interesting.setToolTip(
            "Only frames marked here are sent for editing suggestions.")
        self.interesting.clicked.connect(self.set_interesting)
        row.addWidget(self.interesting)

        self.edit_raw = QCheckBox("Develop from the RAW")
        self.edit_raw.setFont(theme.body(10))
        self.edit_raw.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_raw.clicked.connect(lambda: self.save())
        row.addWidget(self.edit_raw)
        row.addSpacing(10)

        # Your own rating. The assessment's word is a proposal like any other
        # in this application, and the store has always been able to hold a
        # human tier -- there was simply no way to say one.
        rating = QLabel("YOUR RATING")
        rating.setObjectName("eyebrow")
        rating.setFont(theme.display(7))
        row.addWidget(rating)

        self.tiers = QButtonGroup(self)
        self.tiers.setExclusive(True)
        for index, tier in enumerate(TIER_ORDER):
            button = QPushButton(tier.title())
            button.setObjectName("tier")
            button.setCheckable(True)
            button.setFont(theme.body(9))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("tone", _tone(tier))
            self.tiers.addButton(button, index)
            row.addWidget(button)
        self.tiers.idClicked.connect(self._rated)
        row.addStretch(1)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setFont(theme.body(9))
        row.addWidget(self.status)
        layout.addLayout(row)

        self.note = QPlainTextEdit()
        self.note.setObjectName("note")
        self.note.setFont(theme.body(9))
        self.note.setFixedHeight(54)
        self.note.setPlaceholderText(
            "Your note on this frame. It goes to the suggestion pass with it.")
        self.note.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.note)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.selected = QLabel("")
        self.selected.setObjectName("hint")
        self.selected.setWordWrap(True)
        self.selected.setFont(theme.body(9))
        actions.addWidget(self.selected, 1)

        save_note = QPushButton("Save note")
        save_note.setObjectName("ghost")
        save_note.setFont(theme.body(9))
        save_note.clicked.connect(lambda: self.save())
        actions.addWidget(save_note)

        self.suggest_button = QPushButton("Suggest edits →")
        self.suggest_button.setObjectName("primary")
        self.suggest_button.setFont(theme.body(10))
        self.suggest_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.suggest_button.setToolTip(
            "Ask the models how to develop each marked frame. One call per "
            "frame, so this costs.")
        self.suggest_button.clicked.connect(self.suggest)
        actions.addWidget(self.suggest_button)
        layout.addLayout(actions)
        return panel

    # --- entries --------------------------------------------------------

    def _state(self) -> dict:
        return self.reviews.public_state()

    def _effective_tier(self, photo: str, entry: dict) -> str:
        marks = self._state().get("entries", {})
        return str(marks.get(photo, {}).get("tier") or entry.get("tier", ""))

    def _ordered_entries(self) -> list[dict]:
        """The entries in the chosen order.

        Time is the frames in the sequence they were shot -- the shortlist
        carries them with their cluster and filename, both of which follow
        capture order. Rating stacks that under the effective tier, best
        first, so the strongest frames rise together without losing their
        chronology within a tier.
        """
        def when(entry: dict) -> tuple:
            return (str(entry.get("cluster_id", "")),
                    str(entry["photo"]).casefold())

        by_time = sorted(self.entries, key=when)
        mode = (self.sort_by.currentData()
                if getattr(self, "sort_by", None) is not None else "rating")
        if mode == "time":
            return by_time
        # The verdict leads and the score breaks its ties, except where
        # the two contradict each other -- there the frame sits between
        # what they each claim rather than obeying one of them.
        tiers = {str(entry["photo"]): self._effective_tier(
            str(entry["photo"]), entry) for entry in by_time}
        placed = settled_order(by_time, tiers)
        index = {photo: rank for rank, photo in enumerate(placed)}
        return sorted(
            by_time, key=lambda entry: index.get(str(entry["photo"]), 0))

    def _fill_entries(self) -> None:
        state = self._state()
        marks = state.get("entries", {})
        # Where the model's own number and verdict disagree about order,
        # say so rather than letting one of them pass unquestioned.
        self._disagreements = (
            {} if self.by_hand else score_disagreements(self.entries))
        self.title.setText(self.shortlist.path.stem.split(".")[0].upper())
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self._ordered_entries():
            photo = str(entry["photo"])
            mark = "✓" if marks.get(photo, {}).get("interesting") else " "
            # One line: the default item delegate does not wrap, it replaces a
            # newline with an ellipsis, so a second line silently truncates
            # the first.
            # Your rating where you gave one, because that is the tier the
            # rest of the pipeline reads. A list showing the assessment's
            # word beside a decision that overruled it is a list lying about
            # what will happen next.
            rated = str(marks.get(photo, {}).get("tier") or entry["tier"])
            evidence = ("" if self.by_hand else
                        f"  {standing(rated, entry.get('score', 0)):.0f}")
            overruled = " *" if rated != str(entry["tier"]) else ""
            odd = "  ⚠" if photo in self._disagreements else ""
            item = QListWidgetItem(
                f" {mark}  {entry['rank']:>2}.  {photo}"
                f"{evidence}{overruled}{odd}")
            item.setData(Qt.ItemDataRole.UserRole, photo)
            # Your rating where you gave one: the stars the icon wears are
            # the tier the rest of the pipeline reads.
            item.setData(Qt.ItemDataRole.UserRole + 1, tier_stars(rated))
            if odd:
                item.setToolTip(
                    f"The model {self._disagreements[photo]}.")
            item.setIcon(QIcon(self._entry_pixmap(photo, rated)))
            item.setSizeHint(QSize(0, max(ROW, 64)))
            self.list.addItem(item)
        self.list.blockSignals(False)
        summary = state.get("summary", {})
        chosen = int(summary.get("interesting", 0))
        self.progress.setText(
            f"{chosen} of {len(self.entries)} worth developing")
        done = len(self._already_suggested())
        if not chosen:
            self.selected.setText(
                "Nothing is marked yet. The suggestion pass reads exactly the "
                "frames marked here, so it would have nothing to read.")
        elif done >= chosen:
            self.selected.setText(
                f"{chosen} marked, and all of them already have editing "
                "directions.")
        else:
            waiting = chosen - done
            self.selected.setText(
                f"{chosen} frame{'' if chosen == 1 else 's'} marked, "
                f"{waiting} still without editing directions."
                if done else
                f"{chosen} frame{'' if chosen == 1 else 's'} marked. The "
                "suggestion pass reads exactly these.")
        self.suggest_button.setEnabled(
            bool(chosen) and self.directions is not None)

    def _entry_pixmap(self, photo: str, tier: str) -> QPixmap:
        """The photograph wearing its effective tier as stars."""
        width, height = 96, 58
        canvas = QPixmap(width, height)
        canvas.fill(QColor(theme.RAISED))
        painter = QPainter(canvas)
        pixmap = self.loader.cached(photo, "thumb")
        if pixmap is None:
            self.loader.request(photo, "thumb")
            self._icon_wants.setdefault(photo, True)
        else:
            fitted = pixmap.scaled(
                width, height, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(
                (width - fitted.width()) // 2,
                (height - fitted.height()) // 2, fitted)
        stars = tier_stars(tier)
        if stars:
            painter.fillRect(0, height - 16, width, 16,
                             QColor(12, 11, 10, 190))
            painter.setPen(QPen(QColor(theme.SAFELIGHT)))
            painter.drawText(
                0, height - 16, width, 16,
                Qt.AlignmentFlag.AlignCenter, stars)
        painter.end()
        return canvas

    def _show_assessment_detail(self, photo: str = "") -> None:
        """The model's full assessment of one frame, verbatim."""
        photo = str(photo or self.current)
        entry = self.entry_for(photo)
        assessment = entry.get("assessment", {}) or {}
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Why {photo} was rated")
        dialog.setStyleSheet(theme.STYLESHEET)
        dialog.setMinimumWidth(560)
        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(10)
        from opencull_gui.shortlist import tier_stars

        head = QLabel(
            f"{tier_stars(entry.get('tier'))}   "
            f"{str(entry.get('tier', '')).title()}"
            f"   ·   score {float(entry.get('score', 0)):.0f}")
        head.setObjectName("clusterTitle")
        head.setFont(theme.display(13))
        outer.addWidget(head)
        note = QLabel(
            "This is the model's own assessment, kept immutable. Your "
            "rating, set beside the photograph, overrides it downstream.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        note.setFont(theme.body(9))
        outer.addWidget(note)
        odd = getattr(self, "_disagreements", {}).get(photo)
        if odd:
            warning = QLabel(
                f"Its own two readings disagree: {odd}. Neither is wrong "
                "on its own, but the verdict and the number are not "
                "saying the same thing about this frame.")
            warning.setObjectName("status")
            warning.setProperty("tone", "alarm")
            warning.setWordWrap(True)
            warning.setFont(theme.body(9))
            outer.addWidget(warning)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        body = QVBoxLayout(holder)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(8)
        rationale = str(entry.get("rationale", "")).strip()
        if rationale:
            body.addWidget(Axis("OVERALL", rationale))
        for field in ASSESSMENT_FIELDS:
            text = str(assessment.get(field, "")).strip()
            if text:
                body.addWidget(Axis(field.replace("_", " ").upper(), text))
        body.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)
        close = QPushButton("Close")
        close.setObjectName("ghost")
        close.setFont(theme.body(10))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(dialog.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        outer.addLayout(row)
        dialog.resize(600, 560)
        dialog.exec()

    def show_overview(self) -> None:
        """Every frame at once, each wearing its rating."""
        state = self._state()
        marks = state.get("entries", {})
        chosen = int(state.get("summary", {}).get("interesting", 0))
        total = len(self.entries) + len(self.unassessed())
        # A heading reports what is true, and nothing is wrong with a
        # shoot nobody has marked yet: it has just been assessed. The
        # marks appear in the heading once there are marks to report.
        headline = (
            f"{total} frames to rate" if self.by_hand
            else f"{total} frames assessed")
        if chosen:
            headline += f" · {chosen} marked for developing"
        self.overview_heading.setText(headline)
        self.overview_tally.setText(self._tally_line())
        self.overview_tally.setVisible(bool(self.overview_tally.text()))
        if self.sheet is not None:
            self._sheet_slot.removeWidget(self.sheet)
            self.sheet.deleteLater()
        order = [str(entry["photo"]) for entry in self._ordered_entries()]
        unread = self.unassessed()
        order = order + [photo for photo in unread if photo not in order]
        self.sheet = ContactSheet(
            order, self.loader, limit=len(order), opens=True)
        self.sheet.opened.connect(self.show_entry)
        self.sheet.inspect_wanted.connect(self._show_assessment_detail)
        for entry in self.entries:
            photo = str(entry["photo"])
            tile = self.sheet.tiles.get(photo)
            if tile is None:
                continue
            rated = self._effective_tier(photo, entry)
            stars = tier_stars(rated)
            if stars:
                tile.set_badge(stars, self._badge_tip(photo, entry, rated))
            tile.set_marked(bool(marks.get(photo, {}).get("interesting")))
            tile.set_inspectable(not self.by_hand)
        for photo, reason in unread.items():
            tile = self.sheet.tiles.get(photo)
            if tile is None:
                continue
            # No stars, because there is no verdict: the frame says so
            # itself rather than passing as the lowest tier.
            tile.set_badge("⚠ not assessed", reason)
            tile.set_marked(bool(marks.get(photo, {}).get("interesting")))
        self._sheet_slot.addWidget(self.sheet)
        self.views.setCurrentIndex(0)
        self.list.hide()

    def _refresh_tile(self, photo: str) -> None:
        """Keep one tile honest after a decision, without rebuilding all."""
        tile = self.sheet.tiles.get(photo) if self.sheet else None
        if tile is None:
            return
        entry = self.entry_for(photo)
        rated = self._effective_tier(photo, entry)
        stars = tier_stars(rated)
        if stars:
            tile.set_badge(stars, self._badge_tip(photo, entry, rated))
        marks = self._state().get("entries", {})
        tile.set_marked(bool(marks.get(photo, {}).get("interesting")))

    def _tally_line(self) -> str:
        """The shape of the assessment, best tier first."""
        if self.by_hand:
            return ""
        counts: dict[str, int] = {}
        for entry in self.entries:
            tier = self._effective_tier(str(entry["photo"]), entry)
            if tier:
                counts[tier] = counts.get(tier, 0) + 1
        parts = [
            f"{counts[tier]} {tier}"
            for tier in sorted(counts, key=tier_rank, reverse=True)
        ]
        unread = len(self.unassessed())
        if unread:
            parts.append(
                f"{unread} the model could not read")
        return "   ·   ".join(parts)

    def unassessed(self) -> dict[str, str]:
        """Frames the model could not read, and why, from the shortlist."""
        return {
            str(item.get("photo", "")): str(item.get("reason", ""))
            for item in (self.shortlist.data.get("unassessed") or [])
            if str(item.get("photo", ""))
        }

    def _badge_tip(self, photo: str, entry: dict, rated: str) -> str:
        parts = [
            f"{rated.title()} · standing "
            f"{standing(rated, entry.get('score', 0)):.0f} of 100"]
        if rated != str(entry.get("tier", "")):
            parts.append(f"your rating; the model said {entry['tier']}")
        if photo in getattr(self, "_disagreements", {}):
            parts.append(f"the model {self._disagreements[photo]}")
        return " · ".join(parts)

    def _chose_row(self, row: int) -> None:
        if 0 <= row < len(self.entries):
            self.show_entry(str(self.entries[row]["photo"]))

    def entry_for(self, photo: str) -> dict:
        return self.shortlist.entry_by_photo.get(photo, {})

    def show_entry(self, photo: str) -> None:
        self.views.setCurrentIndex(1)
        self.list.show()
        self.current = photo
        entry = self.entry_for(photo)
        self.loader.abandon()
        self.frame.set_message("…")
        pixmap = self.loader.request(photo, "detail")
        if pixmap is not None:
            self._set_frame(pixmap)

        self.heading.setText(photo)
        unread = self.unassessed().get(photo, "")
        self.reask_button.setVisible(not self.by_hand)
        self.unread_note.setText(
            f"The model could not assess this frame. {unread} You can ask "
            "again, rate it yourself below, or leave it as it is."
            if unread else "")
        self.unread_note.setVisible(bool(unread))
        mark = self._state().get("entries", {}).get(photo, {})
        self.set_rating(str(mark.get("tier") or entry.get("tier", "")))
        self._show_verdict()
        self.rationale.setText(
            "" if self.by_hand else str(entry.get("rationale", "")))

        while self.axes.count():
            item = self.axes.takeAt(0)
            # Held once: reparenting can release the layout item's own
            # reference, so asking it a second time can answer None.
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for column in range(self.axes.columnCount() + 1):
            self.axes.setColumnStretch(column, 0)
        if self.by_hand:
            # Ten rows of "Not assessed." is not information. Say once that
            # nobody was asked, and leave the page to the photograph.
            self.axes.addWidget(Axis(
                "NOT ASSESSED",
                "No model has looked at this shoot. The rating below is "
                "yours, and the frames are in the order the cull left them."),
                0, 0)
            self.axes.setColumnStretch(0, 1)
        else:
            assessment = entry.get("assessment", {}) or {}
            readings = [
                (AXIS_LABELS.get(field, field),
                 str(assessment.get(field, "")).strip())
                for field in ASSESSMENT_FIELDS
                if str(assessment.get(field, "")).strip()
            ]
            # Dealt down the columns, so the axes keep their order as the
            # eye travels: first column top to bottom, then the next.
            columns = 3 if len(readings) > 4 else max(1, len(readings))
            rows = -(-len(readings) // columns)
            for index, (label, text) in enumerate(readings):
                self.axes.addWidget(
                    Axis(label, text), index % rows, index // rows)
            for column in range(columns):
                self.axes.setColumnStretch(column, 1)
        self._fit_reading()

        mark = self._state().get("entries", {}).get(photo, {})
        self.interesting.setChecked(bool(mark.get("interesting")))
        self.edit_raw.setChecked(bool(mark.get("edit_raw")))
        self.note.setPlainText(str(mark.get("note", "")))

        row = [item["photo"] for item in self.entries].index(photo)
        if self.list.currentRow() != row:
            self.list.blockSignals(True)
            self.list.setCurrentRow(row)
            self.list.blockSignals(False)
        self._report("")

    def _fit_reading(self) -> None:
        """Let the band be as tall as the readings it holds.

        A reading cut off at the fold is not a reading. The photograph
        keeps whatever height is left, and only a window too short for
        both falls back to scrolling.
        """
        holder = getattr(self, "_reading_holder", None)
        scroll = getattr(self, "_reading_scroll", None)
        if holder is None or scroll is None:
            return
        holder.adjustSize()
        wanted = max(holder.sizeHint().height(), self.axes.sizeHint().height())
        ceiling = max(180, int(self.height() * 0.45))
        scroll.setMinimumHeight(min(wanted + 6, ceiling))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_reading()

    def _show_verdict(self) -> None:
        """What the assessment said, and what you said, in that order."""
        entry = self.entry_for(self.current)
        proposed = str(entry.get("tier", ""))
        yours = self.rating()
        if self.by_hand:
            shown = f"{yours.upper()}   ·   your rating"
        else:
            score = float(entry.get("score", 0))
            confidence = float(entry.get("confidence", 0))
            shown = (
                f"{yours.upper()}   ·   {tier_stars(yours)}   ·   "
                f"standing {standing(yours, score):.0f}/100")
            shown += (
                f"\n└─ {tier_rank(yours)} stars weighting the model's "
                f"score of {score:.0f}, at {confidence:.0%} confidence")
            if proposed and yours != proposed:
                # Never overwritten, only disagreed with: the shortlist is
                # immutable evidence and the two readings stay side by side.
                shown += (f"\n└─ the assessment said {proposed.upper()} · "
                          f"you set {yours.upper()}")
        self.verdict.setText(shown)
        self.verdict.setProperty("tone", _tone(yours))
        self.verdict.style().unpolish(self.verdict)
        self.verdict.style().polish(self.verdict)

    def _set_frame(self, pixmap) -> None:
        # PhotoLabel scales in its own resizeEvent: a parent's is too early,
        # because its children are not laid out when it runs.
        self.frame.set_source(pixmap)

    def _painted(self, photo: str, size: str, pixmap) -> None:
        if photo == self.current and size == "detail":
            self._set_frame(pixmap)
        if size == "thumb" and self._icon_wants.pop(photo, None):
            for row in range(self.list.count()):
                item = self.list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == photo:
                    marks = self._state().get("entries", {})
                    entry = self.entry_for(photo)
                    rated = str(
                        marks.get(photo, {}).get("tier")
                        or entry.get("tier", ""))
                    item.setIcon(QIcon(self._entry_pixmap(photo, rated)))
                    break

    # --- decisions ------------------------------------------------------

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def set_interesting(self, interesting: bool) -> None:
        self.save(interesting=interesting)

    def toggle_interesting(self) -> None:
        self.set_interesting(not self.interesting.isChecked())

    def rating(self) -> str:
        """The tier this page is currently showing as the photographer's."""
        checked = self.tiers.checkedId()
        if 0 <= checked < len(TIER_ORDER):
            return TIER_ORDER[checked]
        return str(self.entry_for(self.current).get("tier", "ordinary"))

    def set_rating(self, tier: str) -> None:
        button = self.tiers.button(
            TIER_ORDER.index(tier) if tier in TIER_ORDER else -1)
        if button is not None:
            button.setChecked(True)
        elif self.tiers.checkedButton() is not None:
            self.tiers.setExclusive(False)
            self.tiers.checkedButton().setChecked(False)
            self.tiers.setExclusive(True)

    def _rated(self, _index: int) -> None:
        self.save()
        self._show_verdict()

    def save(self, interesting: bool | None = None) -> None:
        """Record this frame's decision, including how you rated it.

        The assessment's tier is a proposal like every other judgement this
        application makes. What is written here is yours; the models' stays
        in the shortlist, which is immutable, so the two can always be read
        against each other.
        """
        if not self.current:
            return
        state = self._state()
        chosen = (self.interesting.isChecked() if interesting is None
                  else interesting)
        try:
            self.reviews.update(
                self.current, self.rating(),
                self.edit_raw.isChecked(), self.note.toPlainText(),
                True, state.get("revision"), chosen)
        except ShortlistReviewError as exc:
            self._report(str(exc), "alarm")
            self.interesting.setChecked(bool(
                state.get("entries", {}).get(self.current, {}).get(
                    "interesting")))
            return
        self.interesting.setChecked(chosen)
        self._fill_entries()
        if self.views.currentIndex() == 0:
            self.show_overview()
        else:
            self._refresh_tile(self.current)
        self._report("Saved." if chosen else "Not marked.", "ok")

    # --- asking for suggestions -----------------------------------------

    def _already_suggested(self) -> list[str]:
        """Marked frames that already have directions worth keeping."""
        if self.directions is None:
            return []
        try:
            return list(self.directions.payload().get(
                "processed_photos") or [])
        except Exception:
            return []

    def suggest(self) -> None:
        """Ask for directions from here too: same decision, same dialog."""
        if self.directions is None:
            return
        launch_suggestions(self, self.directions.payload())

    def _ask_scope(self, waiting: int, done: int, plan=()) -> str:
        return ask_suggestion_scope(
            self, waiting, done, plan, loader=self.loader,
            photos_root=photos_root_of(self.shortlist))

    def step(self, delta: int) -> None:
        if not self.entries:
            return
        names = [str(item["photo"]) for item in self.entries]
        row = names.index(self.current) + delta
        if 0 <= row < len(names):
            self.show_entry(names[row])

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if (event.key() == Qt.Key.Key_Escape
                and self.views.currentIndex() == 1):
            self.show_overview()
            return
        key = event.key()
        if self.note.hasFocus():
            super().keyPressEvent(event)
        elif key == Qt.Key.Key_I:
            self.toggle_interesting()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.step(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.step(-1)
        elif key == Qt.Key.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(event)


def _tone(tier: str) -> str:
    """Colour the verdict by what it says, not by where it ranks."""
    if tier in {"exceptional", "strong"}:
        return "ready"
    if tier == "reject":
        return "failed"
    return ""
