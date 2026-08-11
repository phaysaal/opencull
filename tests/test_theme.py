"""The palette, held to the contrast it claims.

Colour was being judged by eye, which is how a secondary text colour sat at
2.76:1 for as long as it did. These measure it instead.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from opencull_qt import theme
except ImportError:  # pragma: no cover - exercised only without PySide6
    theme = None

AA_TEXT = 4.5
AA_LARGE = 3.0


def _channel(value: int) -> float:
    fraction = value / 255
    if fraction <= 0.04045:
        return fraction / 12.92
    return ((fraction + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    digits = colour.lstrip("#")
    red, green, blue = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * _channel(red) + 0.7152 * _channel(green)
            + 0.0722 * _channel(blue))


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@unittest.skipUnless(theme is not None, "PySide6 is not installed")
class ContrastTests(unittest.TestCase):
    def surfaces(self) -> dict[str, str]:
        # Every background a row or panel can present behind text.
        return {"INK": theme.INK, "SURFACE": theme.SURFACE, "RAISED": theme.RAISED}

    def assert_readable(self, name: str, colour: str, minimum: float) -> None:
        for surface, background in self.surfaces().items():
            measured = contrast(colour, background)
            self.assertGreaterEqual(
                measured, minimum,
                f"{name} ({colour}) reads at {measured:.2f}:1 on "
                f"{surface} ({background}); needs {minimum}")

    def test_primary_text_is_readable_everywhere(self):
        self.assert_readable("PAPER", theme.PAPER, AA_TEXT)

    def test_secondary_text_is_readable_everywhere(self):
        self.assert_readable("MUTED", theme.MUTED, AA_TEXT)

    def test_the_faintest_text_still_clears_aa(self):
        # Paths and section labels use this; it is small, so AA text applies.
        self.assert_readable("FAINT", theme.FAINT, AA_TEXT)

    def test_every_state_colour_is_readable(self):
        for name in ("SAFELIGHT", "FIXED", "ALARM"):
            self.assert_readable(name, getattr(theme, name), AA_TEXT)

    def test_the_primary_button_label_is_readable_on_its_fill(self):
        self.assertGreaterEqual(
            contrast("#241203", theme.SAFELIGHT), AA_TEXT)

    def test_the_palette_stays_warm(self):
        # A darkroom, not the usual blue-black chrome: red exceeds blue in
        # every neutral. This is the character the palette was chosen for.
        for name in ("INK", "SURFACE", "RAISED", "EDGE", "EDGE_SOFT",
                     "PAPER", "MUTED", "FAINT"):
            digits = getattr(theme, name).lstrip("#")
            red, blue = int(digits[0:2], 16), int(digits[4:6], 16)
            self.assertGreater(
                red, blue, f"{name} is not warm; a cool neutral breaks the room")

    def test_states_are_never_told_apart_by_colour_alone(self):
        # The badges differ in hue but barely in luminance, so the words are
        # load-bearing for anyone who cannot separate the hues.
        pairs = ((theme.SAFELIGHT, theme.FIXED), (theme.SAFELIGHT, theme.ALARM))
        for first, second in pairs:
            self.assertLess(
                contrast(first, second), 2.0,
                "these read as different brightnesses, so this assumption "
                "about needing text labels may no longer hold")


@unittest.skipUnless(theme is not None, "PySide6 is not installed")
class DialogButtonTests(unittest.TestCase):
    """A choice nobody can read is not a choice.

    A message box takes the smallest size its contents will accept, so a
    button whose stylesheet floor is narrower than its own label is clipped
    mid-word rather than the box growing to fit it.
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.application = QApplication.instance() or QApplication([])

    def test_no_dialog_button_is_narrower_than_its_own_label(self):
        from PySide6.QtWidgets import QMessageBox, QWidget

        host = QWidget()
        host.setStyleSheet(theme.STYLESHEET)
        self.addCleanup(host.deleteLater)
        box = QMessageBox(host)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("39 marked frames fall into 1 scene.")
        box.setInformativeText(
            "One call per scene writes a treatment for each scene's "
            "best-ranked frame and shares it with the rest.")
        for label in ("One per scene (1 call)", "Every frame (39 calls)",
                      "Cancel"):
            box.addButton(label, QMessageBox.ButtonRole.AcceptRole)
        self.addCleanup(box.deleteLater)
        box.show()
        self.addCleanup(box.hide)
        for button in box.buttons():
            self.assertGreaterEqual(
                button.width(), button.sizeHint().width(),
                f"{button.text()!r} is clipped to {button.width()}px")


if __name__ == "__main__":
    unittest.main()
