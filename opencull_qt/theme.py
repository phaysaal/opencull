"""Darkimiya's visual language, expressed for Qt.

The palette is a darkroom rather than the usual blue-black application chrome:
a warm near-black wall, warm paper white, and a single safelight amber that
means "work in flight" wherever it appears.

A darkroom is also an alchemical space -- one dark room, one non-actinic light,
chemical baths, a latent image transmuted into a visible one -- which is the
other half of the name. The accents happen to trace the stages of the magnum
opus, and are kept in that order:

    INK       nigredo     the blackening; the wall everything sits on
    PAPER     albedo      the whitening; the print, and all primary text
    SAFELIGHT citrinitas  the yellowing; the light that is on while you work
    ALARM     rubedo      the reddening; failure, and the irreversible
    FIXED     verdigris   oxidised copper; the patina of a finished thing

FIXED keeps its name because fixing is the darkroom step that makes an image
permanent, which is exactly what it marks. Its value is verdigris rather than
the interface-library green it started as: that was the one colour in the
palette belonging to neither the darkroom nor the alchemy.

Qt differs from CSS in one way that matters here: a ``font-size`` in a
stylesheet rule cascades to child widgets and silently overrides any font set
with ``setFont``. Sizes are therefore set on widgets, never in the sheet.

Contrast is measured, not judged by eye. Every colour used for text clears
WCAG AA (4.5:1) against INK, SURFACE and RAISED. The state badges are
separated by hue rather than by luminance, so each one always carries its word
as well as its colour.
"""

from __future__ import annotations

from PySide6.QtGui import QFont

INK = "#131211"
SURFACE = "#1B1918"
RAISED = "#232120"
EDGE = "#302C29"
EDGE_SOFT = "#262322"
PAPER = "#EDE7DE"
MUTED = "#98908A"
# Lightened from #6B645F, which read at 2.76:1 on RAISED and failed AA.
FAINT = "#908880"
SAFELIGHT = "#FF9B47"
SAFELIGHT_BRIGHT = "#FFAC63"
SAFELIGHT_DEEP = "#F08D3B"
FIXED = "#5CB39E"
ALARM = "#E8735A"

DISPLAY_FAMILIES = [
    "Ubuntu Condensed", "SF Compact Display", "Roboto Condensed",
    "Inter Tight", "DejaVu Sans Condensed",
]
BODY_FAMILIES = [
    "Ubuntu", "SF Pro Text", "Adwaita Sans", "Cantarell",
    "Segoe UI Variable Text", "Noto Sans", "DejaVu Sans",
]
MONO_FAMILIES = [
    "Ubuntu Mono", "SF Mono", "JetBrains Mono", "DejaVu Sans Mono", "monospace",
]


def _font(families: list[str], size: int, weight: QFont.Weight) -> QFont:
    font = QFont()
    font.setFamilies(families)
    font.setPointSize(size)
    font.setWeight(weight)
    return font


def display(size: int, weight: QFont.Weight = QFont.Weight.Bold) -> QFont:
    return _font(DISPLAY_FAMILIES, size, weight)


def body(size: int = 10, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    return _font(BODY_FAMILIES, size, weight)


def mono(size: int = 8) -> QFont:
    return _font(MONO_FAMILIES, size, QFont.Weight.Normal)


STYLESHEET = f"""
QMainWindow, QDialog, QWidget#page {{ background: {INK}; }}
QWidget {{ color: {PAPER}; }}
QToolTip {{
    background: {RAISED}; color: {PAPER};
    border: 1px solid {EDGE}; padding: 4px 6px;
}}

QFrame#chrome {{ background: {SURFACE}; border-bottom: 1px solid {EDGE_SOFT}; }}
QLabel#chromeTitle {{ letter-spacing: 2px; }}
QLabel#eyebrow, QLabel#bandTitle {{ color: {FAINT}; letter-spacing: 3px; }}
QLabel#heroSub, QLabel#hint {{ color: {MUTED}; }}
QLabel#rowName {{ font-weight: 600; }}
QLabel#rowPath {{ color: {FAINT}; }}
QLabel#rowState {{ color: {FAINT}; letter-spacing: 1px; }}
QLabel#rowState[tone="running"] {{ color: {SAFELIGHT}; }}
QLabel#rowState[tone="ready"] {{ color: {FIXED}; }}
QLabel#rowState[tone="failed"] {{ color: {ALARM}; }}
QLabel#status[tone="ok"] {{ color: {FIXED}; }}
QLabel#status[tone="alarm"] {{ color: {ALARM}; }}

QPushButton#primary {{
    background: {SAFELIGHT}; color: #241203;
    border: 1px solid {SAFELIGHT_BRIGHT}; border-radius: 8px;
    padding: 9px 18px; font-weight: 600;
}}
QPushButton#primary:hover {{ background: {SAFELIGHT_BRIGHT}; }}
QPushButton#primary:pressed {{ background: {SAFELIGHT_DEEP}; }}
QPushButton#primary:disabled {{ background: {EDGE}; color: {FAINT};
    border-color: {EDGE}; }}

QPushButton#ghost {{
    background: transparent; border: 1px solid {EDGE};
    border-radius: 8px; padding: 8px 16px;
}}
QPushButton#ghost:hover {{ background: {RAISED}; border-color: #3B3633; }}
QPushButton#ghost:pressed {{ background: {EDGE_SOFT}; }}
QPushButton#ghost:disabled {{ color: {FAINT}; border-color: {EDGE_SOFT}; }}

QFrame#rows {{
    background: {SURFACE}; border: 1px solid {EDGE_SOFT}; border-radius: 10px;
}}
QFrame#row {{ background: transparent; border-bottom: 1px solid {EDGE_SOFT}; }}
QFrame#row:hover, QFrame#rowLast:hover {{ background: {RAISED}; }}
QFrame#rowLast {{ background: transparent; border: none; }}

QFrame#notice {{
    background: rgba(255, 155, 71, 18);
    border: 1px solid #7A4A21; border-radius: 10px;
}}
QFrame#notice[tone="alarm"] {{
    background: rgba(232, 115, 90, 20); border-color: #6B3025;
}}

QLineEdit, QComboBox, QSpinBox {{
    background: {INK}; color: {PAPER};
    border: 1px solid {EDGE}; border-radius: 8px; padding: 8px 10px;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {SAFELIGHT}; }}
QLineEdit[stored="true"] {{ border-color: #2E5C45; color: {FIXED}; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; border: 1px solid {EDGE};
    selection-background-color: {EDGE};
}}

QProgressBar {{
    background: {EDGE}; border: none; border-radius: 0; height: 3px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {SAFELIGHT}; }}

QFrame#card {{
    background: {SURFACE}; border: 1px solid {EDGE_SOFT}; border-radius: 10px;
}}
QFrame#card:hover {{ border-color: {EDGE}; }}
QLabel#cardName {{ color: {PAPER}; }}
QPushButton#icon {{ background: transparent; border: none; border-radius: 13px; }}
QPushButton#icon:hover {{ background: {EDGE_SOFT}; }}
QLabel#cardCount {{ color: {MUTED}; }}

/* --- the phase bar --- */
QFrame#phaseBar {{
    background: {SURFACE}; border-bottom: 1px solid {EDGE_SOFT};
}}
QPushButton#phase {{
    background: transparent; border: none;
    border-bottom: 2px solid transparent;
    padding: 6px 11px; color: {PAPER}; letter-spacing: 1px;
}}
QPushButton#phase:hover {{ background: {RAISED}; }}
QPushButton#phase[state="done"] {{ color: {FIXED}; }}
QPushButton#phase[state="running"] {{ color: {SAFELIGHT}; }}
QPushButton#phase[state="blocked"] {{ color: {FAINT}; }}
/* A blocked phase says why when pressed, so it must not look pressable. */
QPushButton#phase[state="blocked"]:hover {{ background: transparent; }}
QPushButton#phase[current="true"] {{
    color: {PAPER}; border-bottom-color: {SAFELIGHT};
}}

/* --- review page --- */
QListWidget#clusterList {{
    background: {SURFACE}; border: none;
    border-right: 1px solid {EDGE_SOFT}; outline: none;
    padding: 6px 0;
}}
QListWidget#clusterList::item {{
    padding: 9px 12px; color: {MUTED}; border: none;
}}
QListWidget#clusterList::item:selected {{
    background: {RAISED}; color: {PAPER};
    border-left: 2px solid {SAFELIGHT};
}}
QListWidget#clusterList::item:hover {{ background: {RAISED}; }}

QLabel#clusterTitle {{ color: {PAPER}; }}
QFrame#frame {{
    background: {SURFACE}; border: 1px solid {EDGE_SOFT}; border-radius: 8px;
}}
QFrame#frame:hover {{ background: {RAISED}; border-color: {EDGE}; }}
QFrame#frame[kept="true"] {{ background: {RAISED}; border-color: {SAFELIGHT}; }}
QLabel#frameImage {{ background: {INK}; border-radius: 4px; color: {FAINT}; }}
QLabel#frameName {{ color: {FAINT}; }}
QLabel#frameNumber {{ color: {MUTED}; }}
QLabel#frameAi {{ color: {SAFELIGHT}; letter-spacing: 1px; }}

/* --- assessment page --- */
QFrame#decision {{
    background: {SURFACE}; border: 1px solid {EDGE_SOFT}; border-radius: 10px;
}}
QLabel#axisName {{ color: {FAINT}; letter-spacing: 2px; }}
QLabel#axisBody {{ color: {MUTED}; }}
QPlainTextEdit#note {{
    background: {INK}; color: {PAPER};
    border: 1px solid {EDGE}; border-radius: 8px; padding: 6px 8px;
}}
QPlainTextEdit#note:focus {{ border-color: {SAFELIGHT}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {EDGE}; border-radius: 4px; background: {INK};
}}
QCheckBox::indicator:hover {{ border-color: #3B3633; }}
QCheckBox::indicator:checked {{
    background: {SAFELIGHT}; border-color: {SAFELIGHT_BRIGHT};
}}
QCheckBox#gate {{ font-weight: 600; }}

/* --- develop page --- */
QFrame#panel {{ background: {SURFACE}; border-left: 1px solid {EDGE_SOFT}; }}
QFrame#pane {{
    background: {SURFACE}; border: 1px solid {EDGE_SOFT}; border-radius: 10px;
}}
QLabel#paneCaption {{ color: {FAINT}; letter-spacing: 2px; }}
/* The caption is the only thing that says which of the two is on screen,
   so it changes colour as well as words. */
QLabel#paneCaption[state="treated"] {{ color: {SAFELIGHT}; }}
QLabel#paneHint {{ color: {FAINT}; }}
/* Transparent, so the space a photograph does not fill reads as the frame
   around it rather than as a second empty box inside it. */
QLabel#paneImage {{ background: transparent; color: {FAINT}; }}
QLabel#verdict {{ color: {MUTED}; }}
QLabel#verdict[tone="ok"] {{ color: {FIXED}; }}
QLabel#verdict[tone="alarm"] {{ color: {ALARM}; }}
QListWidget#treatmentList {{
    background: {INK}; border: 1px solid {EDGE_SOFT};
    border-radius: 8px; outline: none; padding: 4px 0;
}}
/* No vertical padding: the row's height is set on the item, and the
   delegate centres the text within it. */
QListWidget#treatmentList::item {{
    padding: 0 10px; color: {MUTED}; border: none;
}}
QListWidget#treatmentList::item:selected {{
    background: {RAISED}; color: {PAPER};
    border-left: 2px solid {SAFELIGHT};
}}
QListWidget#treatmentList::item:hover {{ background: {RAISED}; }}

/* --- fine tuning --- */
/* The scrolled holder is a plain QWidget, so QFrame#panel cannot reach it
   and it would otherwise paint with the default light palette. */
QScrollArea#controlScroll, QWidget#controls {{ background: {SURFACE}; }}
QLabel#reading {{ color: {PAPER}; }}
/* A value that no longer says what the model asked for is marked, so a
   panel of twelve controls does not have to be read twelve times. */
QLabel#provenance {{ color: {FAINT}; }}
QLabel#provenance[moved="true"] {{ color: {SAFELIGHT}; }}
QLabel#guardrail {{ color: {FIXED}; }}
QSlider::groove:horizontal {{
    background: {EDGE}; height: 3px; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {EDGE}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {PAPER}; width: 11px; height: 11px;
    margin: -4px 0; border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: {SAFELIGHT_BRIGHT}; }}
QSlider:disabled::handle:horizontal {{ background: {EDGE}; }}

QScrollArea {{ border: none; background: {INK}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {EDGE}; border-radius: 5px; min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{ background: #3B3633; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""
