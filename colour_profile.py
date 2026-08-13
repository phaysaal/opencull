"""What a written file says its numbers mean.

Every JPEG this application wrote carried zero bytes of colour profile.
The numbers in them were sRGB -- that is what the pipeline produces --
but the files never said so, and a file that does not say leaves every
program downstream to guess. Most guess sRGB and are right. A colour
managed viewer on a wide-gamut screen guesses sRGB, converts to the
monitor, and shows one picture; this application drew the same numbers
to the same screen untouched and showed another. Two pictures, one
file, and no way to tell which was the photograph.

The profile is embedded on write so the file answers for itself. It is
588 bytes and costs nothing measurable. The other half of the fix --
converting for the screen before drawing -- lives in opencull_qt.colour,
because it needs to know what screen is being drawn on.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageCms


@lru_cache(maxsize=1)
def srgb_profile() -> bytes:
    """The sRGB profile, as the bytes to embed in a written file."""
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


# A JPEG carries its profile in an APP2 segment introduced by this string,
# followed by a chunk number and a chunk count -- profiles larger than a
# segment are split, though sRGB at 588 bytes never is.
_APP2 = b"\xff\xe2"
_ICC_TAG = b"ICC_PROFILE\x00"
_JPEG_START = b"\xff\xd8"
# Segments that belong before the profile: the JFIF header and the EXIF
# block. Anything else, the profile goes in front of.
_PRECEDING = {b"\xff\xe0", b"\xff\xe1"}


def tag_in_place(path: Path) -> bool:
    """Give an already-written JPEG its sRGB profile, without recompressing.

    Photographs exported before this application said anything about
    colour are finished work: re-saving them through an encoder to add
    twelve bytes of header would decode and re-encode every block and
    lose a little of the picture to do it. A profile lives in its own
    segment, so it can be spliced into the file the encoder already
    wrote and not one pixel changes.

    Returns whether the file needed tagging.
    """
    path = Path(path)
    data = path.read_bytes()
    if not data.startswith(_JPEG_START):
        raise ValueError(f"{path.name} is not a JPEG")
    if _ICC_TAG in data[:64 << 10]:
        return False

    at = 2
    while at + 4 <= len(data) and data[at:at + 2] in _PRECEDING:
        at += 2 + int.from_bytes(data[at + 2:at + 4], "big")

    profile = srgb_profile()
    payload = _ICC_TAG + b"\x01\x01" + profile
    segment = _APP2 + (len(payload) + 2).to_bytes(2, "big") + payload
    temporary = path.with_suffix(path.suffix + ".tagging")
    try:
        temporary.write_bytes(data[:at] + segment + data[at:])
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return True
