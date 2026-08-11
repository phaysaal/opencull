"""Scenes: the grouping one treatment is shared across.

The cull already groups near-duplicates -- frames so alike that only one of
them deserves to survive. A scene is the coarser order above that: same
place, same light, same subject, where every frame deserves to survive and
one treatment honestly serves them all. A wedding's forty frames in the
same church light are forty near-duplicate groups but a handful of scenes,
and asking a model forty times how to edit the same light is paying forty
times for one answer -- and risking forty answers that disagree about it.

Scenes are computed locally, for nothing, from evidence already in hand:
capture times where the files carry them, frame numbers where they do not.
Two adjacent frames stay in one scene while the gap between them is small;
a frame from a near-duplicate group never lands in a different scene than
its siblings, because frames alike enough to be duplicates are alike enough
to share a treatment. Where neither time nor sequence is known the scene
breaks, deliberately: sharing a treatment across frames nothing says belong
together would damage photographs to save a call, and that is the wrong
side of the trade.

The plan a photographer approves is written down, and the sharing happens
by derivation: the representative's directions are copied to its scene's
other marked frames as a new directions file that says exactly where each
entry came from. The suggestion evidence itself is never edited.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from .directions import DIRECTIONS_FORMAT, _atomic_json, _sha256_of

PLAN_FORMAT = "opencull-scene-plan-v1"

# Ten minutes: long enough to cross a room, short enough that the light is
# still the light. A gap beyond this is treated as a change of scene.
TIME_GAP_SECONDS = 600.0

# When no capture time is readable, the shutter count stands in for the
# clock. Forty frames of gap between two consecutive kept frames means the
# camera did a lot of work in between that this selection never saw.
SEQUENCE_GAP = 40

_EXIF_DATETIME_ORIGINAL = 36867
_EXIF_DATETIME = 306
_EXIF_IFD = 0x8769

# A raw file is not an image format PIL can open, so its clock was invisible
# here and a whole shoot collapsed into one scene. But a camera writes an
# ordinary JPEG preview inside the raw for its own screen, and that preview
# carries the usual EXIF block, introduced by this marker. Reading the head
# of the file and looking for it costs a megabyte of disk and no decode at
# all -- 177 Fujifilm frames in under a third of a second.
_EXIF_MARKER = b"Exif\x00\x00"
_HEAD_BYTES = 4 << 20


def _stamp(value: Any) -> float | None:
    try:
        return datetime.strptime(
            str(value).strip(), "%Y:%m:%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return None


def _from_exif(exif: Any) -> float | None:
    """The moment of exposure, preferring the tag that means exactly that."""
    values = [exif.get(_EXIF_DATETIME_ORIGINAL)]
    try:
        values.append(exif.get_ifd(_EXIF_IFD).get(_EXIF_DATETIME_ORIGINAL))
    except Exception:                                # noqa: BLE001 - absent
        pass
    values.append(exif.get(_EXIF_DATETIME))
    for value in values:
        stamp = _stamp(value) if value else None
        if stamp is not None:
            return stamp
    return None


def _embedded_capture_time(path: Path) -> float | None:
    """The EXIF of the preview a raw file carries, without decoding it."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_HEAD_BYTES)
    except OSError:
        return None
    at = head.find(_EXIF_MARKER)
    if at < 0:
        return None
    exif = Image.Exif()
    try:
        exif.load(head[at:])
    except Exception:                                # noqa: BLE001 - unreadable
        return None
    return _from_exif(exif)


@lru_cache(maxsize=8192)
def _capture_time(path: str, _stat: tuple[int, int]) -> float | None:
    try:
        with Image.open(path) as image:
            found = _from_exif(image.getexif())
    except Exception:                                # noqa: BLE001 - not an image
        found = None
    return found if found is not None else _embedded_capture_time(Path(path))


def capture_time(path: Path) -> float | None:
    """When the frame was taken, from its own EXIF, or None.

    Cached against the file's size and modification time, because a scene
    grouping asks about every frame of a shoot and is recomputed whenever a
    page that shows scenes is opened.
    """
    path = Path(path)
    try:
        status = path.stat()
    except OSError:
        return None
    return _capture_time(str(path), (status.st_size, status.st_mtime_ns))


def _sequence(name: str) -> int | None:
    """The frame counter in a camera filename, if there is one."""
    match = re.search(r"(\d+)(?!.*\d)", Path(name).stem)
    return int(match.group(1)) if match else None


def scene_groups(
    entries: list[dict[str, Any]],
    photos_root: Path | None = None,
    *,
    time_gap: float = TIME_GAP_SECONDS,
    sequence_gap: int = SEQUENCE_GAP,
) -> list[dict[str, Any]]:
    """Partition shortlist entries into scenes, in shooting order.

    Between two adjacent frames, the clock decides where it can and the
    frame counter decides where it cannot; where neither is known the scene
    breaks, because sharing a treatment on no evidence is worse than paying
    for another call. Frames of one near-duplicate cluster never separate.
    """
    ordered = sorted(
        (dict(entry) for entry in entries if entry.get("photo")),
        key=lambda entry: str(entry["photo"]))
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    def times(entry: dict[str, Any]) -> float | None:
        if photos_root is None:
            return None
        return capture_time(Path(photos_root) / str(entry["photo"]))

    previous: dict[str, Any] | None = None
    previous_time: float | None = None
    for entry in ordered:
        entry_time = times(entry)
        if previous is not None:
            same_cluster = (
                entry.get("cluster_id") is not None
                and entry.get("cluster_id") == previous.get("cluster_id"))
            if same_cluster:
                broken = False
            elif previous_time is not None and entry_time is not None:
                broken = (entry_time - previous_time) > time_gap
            else:
                first = _sequence(str(previous["photo"]))
                second = _sequence(str(entry["photo"]))
                if first is not None and second is not None:
                    broken = (second - first) > sequence_gap
                else:
                    broken = True
            if broken:
                groups.append(current)
                current = []
        current.append(entry)
        previous, previous_time = entry, entry_time
    if current:
        groups.append(current)
    return [
        {"id": f"scene-{index:04d}",
         "photos": [str(entry["photo"]) for entry in group]}
        for index, group in enumerate(groups, start=1)
    ]


def photos_root_of(shortlist: Any) -> Path | None:
    """Where a shortlist's photographs are, according to the shortlist.

    Asking every caller to remember the folder is how the clock came to be
    ignored everywhere: one call site omitted it and every scene on that
    path was computed from filenames alone. The index already knows.
    """
    root = getattr(getattr(shortlist, "assets", None), "root", None)
    return Path(root) if root else None


def plan_for(
    shortlist: Any, targets: list[str],
    photos_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Which frame speaks for each scene, among the frames being asked about.

    The representative is the best-ranked target in its scene: the frame
    the assessment thought most of is the one whose treatment the others
    inherit.
    """
    if photos_root is None:
        photos_root = photos_root_of(shortlist)
    wanted = set(targets)
    rank = {
        str(entry["photo"]): int(entry.get("rank", 10**9))
        for entry in shortlist.entries
    }
    plan = []
    for group in scene_groups(list(shortlist.entries), photos_root):
        members = [photo for photo in group["photos"] if photo in wanted]
        if not members:
            continue
        plan.append({
            "id": group["id"],
            "photos": members,
            "representative": min(
                members, key=lambda photo: rank.get(photo, 10**9)),
        })
    return plan


def plan_path(recipes: Path, shortlist_stem: str) -> Path:
    return Path(recipes) / f"{shortlist_stem}.scene-plan.json"


def write_plan(
    recipes: Path, shortlist_path: Path, revision: int,
    plan: list[dict[str, Any]],
) -> Path:
    """Record the sharing a photographer agreed to, before the run starts.

    Bound to the shortlist by hash so a plan can never quietly apply to a
    different assessment than the one it was approved against.
    """
    destination = plan_path(Path(recipes), Path(shortlist_path).stem)
    _atomic_json(destination, {
        "format": PLAN_FORMAT,
        "shortlist_sha256": _sha256_of(Path(shortlist_path)),
        "review_revision": int(revision),
        "scenes": plan,
    })
    return destination


def derive_scene_entries(index: Any) -> Path | None:
    """Extend a scene's answered representative to its unanswered siblings.

    Runs when directions are read, so the sharing happens the moment the
    representative's answer exists, wherever it is read from. The derived
    file says on every entry which frame answered for it; the original
    suggestion evidence is never touched.
    """
    shortlist_path = Path(index.shortlist.path)
    source = plan_path(index.recipes, shortlist_path.stem)
    if not source.is_file():
        return None
    try:
        plan = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = _sha256_of(shortlist_path)
    if (
        not isinstance(plan, dict)
        or plan.get("format") != PLAN_FORMAT
        or plan.get("shortlist_sha256") != sha
    ):
        # A plan for a different assessment authorises nothing about this
        # one. It is left where it is and ignored.
        return None

    known: dict[str, dict[str, Any]] = {}
    style_sha = ""
    for _path, report in index.reports(sha):
        for entry in report.get("entries", []) or []:
            photo = entry.get("photo") if isinstance(entry, dict) else None
            if isinstance(photo, str) and photo not in known:
                known[photo] = entry
                if not style_sha:
                    style_sha = str(report.get("style_profile_sha256", ""))

    derived = []
    for scene in plan.get("scenes", []) or []:
        if not isinstance(scene, dict):
            continue
        representative = str(scene.get("representative", ""))
        answer = known.get(representative)
        if answer is None:
            continue
        for photo in scene.get("photos", []) or []:
            if photo == representative or photo in known:
                continue
            derived.append({
                **json.loads(json.dumps(answer)),
                "photo": photo,
                "derived_from": representative,
                "scene": str(scene.get("id", "")),
            })
    if not derived:
        return None

    revision = int(plan.get("review_revision", 0) or 0)
    destination = index.recipes / (
        f"{shortlist_path.stem}.edit-directions-r{revision}-scenes.json")
    value = {
        "format": DIRECTIONS_FORMAT,
        "shortlist_sha256": sha,
        "review_revision": revision,
        "style_profile_sha256": style_sha,
        "derived": True,
        "entries": derived,
    }
    if destination.is_file():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if existing == value:
            return destination
    _atomic_json(destination, value)
    return destination
