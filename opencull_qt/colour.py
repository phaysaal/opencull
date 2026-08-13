"""Colour, from the file to the screen it is judged on.

A photograph is numbers until something decides what they mean. The
files this application writes are sRGB -- colour_profile now says so in
the file itself -- but saying so only helps programs that listen, and
this one did not. It drew the file's numbers straight to the display.
An image viewer beside it read the same numbers, converted them for the
monitor's own profile, and drew different ones. Two pictures, one file,
and a photographer deciding which frame to keep by looking at whichever
window happened to be in front.

The screen here is wide-gamut, so the difference is not academic: sRGB
drawn unmanaged on it comes out more saturated than the photograph is.
Every edit judged that way was judged against a lie the screen told.

So pixels are converted from sRGB to the display's profile at the point
where they become something drawable, which is the only place there is
one funnel for all of them. Where no display profile can be found the
pixels pass through untouched -- the old behaviour, but now because the
screen never said what it was, not because nobody asked.

This is Qt's own conversion rather than a detour through PIL: previews
arrive as files, become QImage, and go to the screen, and QColorSpace
converts them in that form without a round trip through another
library's idea of an image.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QColorSpace, QImage, QPixmap

from colour_profile import srgb_profile

__all__ = [
    "PROFILE_ENVIRONMENT", "display_profile", "for_screen", "load_for_screen",
    "managed", "srgb_profile",
]

# A photographer who has named a profile has named it deliberately, so it
# outranks anything discovered. The value "none" turns the conversion off
# -- worth having on a desktop whose compositor manages colour itself,
# where converting here would be the second conversion of the same pixels.
PROFILE_ENVIRONMENT = "DARKIMIYA_DISPLAY_PROFILE"


def _from_colord() -> Path | None:
    """The profile colord holds for the screen, if it holds one.

    Two screens are ordinary -- a laptop and something better beside it --
    and colord marks one primary. That is the one a window is most likely
    being judged on, and guessing between them is worse than preferring
    the screen the desktop itself prefers.
    """
    try:
        listed = subprocess.run(
            ["colormgr", "get-devices-by-kind", "display"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    devices: list[tuple[bool, Path]] = []
    profile: Path | None = None
    primary = False
    # A trailing sentinel closes the last device's block without repeating
    # the bookkeeping after the loop.
    for line in [*listed.stdout.splitlines(), "Object Path:"]:
        stripped = line.strip()
        if stripped.startswith("Object Path:"):
            if profile is not None:
                devices.append((primary, profile))
            profile, primary = None, False
            continue
        if "OutputPriority=primary" in stripped:
            primary = True
        if stripped.endswith(".icc") and profile is None:
            candidate = Path(stripped.split()[-1])
            if candidate.is_file():
                profile = candidate
    for wanted in (True, False):
        for is_primary, path in devices:
            if is_primary is wanted:
                return path
    return None


@lru_cache(maxsize=1)
def display_profile() -> bytes | None:
    """The screen's own profile, or None when the screen has not said."""
    named = os.environ.get(PROFILE_ENVIRONMENT, "").strip()
    if named.lower() == "none":
        return None
    candidates = [Path(named)] if named else []
    found = _from_colord()
    if found is not None:
        candidates.append(found)
    for path in candidates:
        try:
            if path.is_file():
                return path.read_bytes()
        except OSError:
            continue
    return None


@lru_cache(maxsize=1)
def _display_space() -> QColorSpace | None:
    profile = display_profile()
    if profile is None:
        return None
    space = QColorSpace.fromIccProfile(profile)
    return space if space.isValid() else None


@lru_cache(maxsize=1)
def _srgb() -> QColorSpace:
    return QColorSpace(QColorSpace.NamedColorSpace.SRgb)


def managed() -> bool:
    """Whether pixels are being converted for a screen profile at all."""
    return _display_space() is not None


def for_screen(image: QImage) -> QImage:
    """One image, as this screen should show it.

    An image that carries its own profile is converted from that; one
    that carries none is taken as sRGB, which is what this application
    writes and what an untagged file means everywhere else.
    """
    space = _display_space()
    if space is None or image.isNull():
        return image
    if not image.colorSpace().isValid():
        image.setColorSpace(_srgb())
    if image.colorSpace() == space:
        return image
    converted = image.convertedToColorSpace(space)
    return image if converted.isNull() else converted


def load_for_screen(path: str | Path) -> QPixmap:
    """Read a preview from disk as something safe to draw on this screen.

    Loading straight into a QPixmap would throw the file's profile away
    before anything could act on it, so the file becomes a QImage first,
    which is where Qt keeps what the file said about its own colour.
    """
    image = QImage(str(path))
    if image.isNull():
        return QPixmap()
    return QPixmap.fromImage(for_screen(image))
