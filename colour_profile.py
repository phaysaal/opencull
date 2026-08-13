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

# An ICC header carries the moment the profile was created, so asking
# littleCMS for sRGB twice gives two different byte strings for the same
# colour space. That would make two exports of one photograph differ in
# their headers, and it makes "does this file already carry our profile"
# unanswerable by comparison. The field is zeroed: the specification
# allows it, every reader ignores it, and the bytes become the same bytes
# every time.
_CREATED_AT = slice(24, 36)


@lru_cache(maxsize=1)
def srgb_profile() -> bytes:
    """The sRGB profile, as the bytes to embed in a written file."""
    built = bytearray(
        ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    built[_CREATED_AT] = bytes(12)
    return bytes(built)


# A JPEG carries its profile in an APP2 segment introduced by this string,
# followed by a chunk number and a chunk count -- profiles larger than a
# segment are split, though sRGB at 588 bytes never is.
_APP2 = b"\xff\xe2"
_ICC_TAG = b"ICC_PROFILE\x00"
_JPEG_START = b"\xff\xd8"
# Segments that belong before the profile: the JFIF header and the EXIF
# block. Anything else, the profile goes in front of.
_PRECEDING = {b"\xff\xe0", b"\xff\xe1"}
# Where the header ends and the compressed picture begins. Nothing past
# this point is a segment, and nothing past it may be touched.
_SCAN_START = b"\xff\xda"


def segments(data: bytes) -> list[tuple[int, bytes, bytes]]:
    """Walk a JPEG's header segments: where each is, what it is, what it says.

    The structure has to be walked rather than searched. A camera writes
    an EXIF block holding its own preview image, and on a Fujifilm frame
    that block runs to 65 448 bytes -- so looking for the profile within
    a fixed window near the front finds nothing on exactly the files most
    likely to already have one, and tags them a second time.
    """
    found: list[tuple[int, bytes, bytes]] = []
    at = 2
    while at + 4 <= len(data) and data[at] == 0xFF:
        marker = data[at:at + 2]
        if marker == _SCAN_START:
            break
        if marker == _JPEG_START or 0xD0 <= data[at + 1] <= 0xD9:
            at += 2                                  # a marker without a body
            continue
        length = int.from_bytes(data[at + 2:at + 4], "big")
        if length < 2:
            break
        found.append((at, marker, data[at + 4:at + 2 + length]))
        at += 2 + length
    return found


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
    header = segments(data)
    if any(marker == _APP2 and payload.startswith(_ICC_TAG)
           for _at, marker, payload in header):
        return False

    at = 2
    for start, marker, payload in header:
        if marker not in _PRECEDING:
            break
        at = start + 4 + len(payload)

    payload = _ICC_TAG + b"\x01\x01" + srgb_profile()
    segment = _APP2 + (len(payload) + 2).to_bytes(2, "big") + payload
    temporary = path.with_suffix(path.suffix + ".tagging")
    try:
        temporary.write_bytes(data[:at] + segment + data[at:])
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return True
