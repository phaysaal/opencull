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
# of the file and looking for it needs no decode at all.
#
# The marker sits within the first bytes of that preview -- byte 154 in
# every Fujifilm frame measured -- so a small head finds it while reading
# kilobytes, not megabytes. Only when the small head does not carry a
# usable time do we pay for the large one, which keeps a camera that
# buries EXIF deeper working. The old code read the large head every time:
# 4 MB from each of a few hundred raws is a gigabyte of disk to reach a
# timestamp 154 bytes in, and that is what made opening a folder slow.
_EXIF_MARKER = b"Exif\x00\x00"
_SMALL_HEAD = 64 << 10
_LARGE_HEAD = 4 << 20


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
    """The EXIF of the preview a raw file carries, without decoding it.

    A small head is tried first and answers almost every frame. The
    large head is read only when the small one carried no usable time --
    the marker was further in, or the EXIF block ran past the small
    head and truncated before the timestamp. A file shorter than the
    head it was given has nothing more to offer, so the fallback stops.
    """
    seen = -1
    for cap in (_SMALL_HEAD, _LARGE_HEAD):
        try:
            with open(path, "rb") as handle:
                head = handle.read(cap)
        except OSError:
            return None
        if len(head) == seen:
            break                                    # file shorter than cap
        seen = len(head)
        at = head.find(_EXIF_MARKER)
        if at >= 0:
            exif = Image.Exif()
            try:
                exif.load(head[at:])
                found = _from_exif(exif)
            except Exception:                        # noqa: BLE001 - unreadable
                found = None
            if found is not None:
                return found
    return None


_EXIF_MODEL = 0x0110


def _model_from_exif(exif: Any) -> str | None:
    value = exif.get(_EXIF_MODEL)
    settled = " ".join(str(value).split()) if value else ""
    return settled or None


def _embedded_camera_model(path: Path) -> str | None:
    """The camera's name from a raw file's head, without decoding it."""
    seen = -1
    for cap in (_SMALL_HEAD, _LARGE_HEAD):
        try:
            with open(path, "rb") as handle:
                head = handle.read(cap)
        except OSError:
            return None
        if len(head) == seen:
            break
        seen = len(head)
        at = head.find(_EXIF_MARKER)
        if at >= 0:
            exif = Image.Exif()
            try:
                exif.load(head[at:])
                found = _model_from_exif(exif)
            except Exception:                        # noqa: BLE001 - unreadable
                found = None
            if found is not None:
                return found
    return None


@lru_cache(maxsize=8192)
def _camera_model(path: str, _stat: tuple[int, int]) -> str | None:
    try:
        with Image.open(path) as image:
            found = _model_from_exif(image.getexif())
    except Exception:                                # noqa: BLE001 - not an image
        found = None
    return found if found is not None else _embedded_camera_model(Path(path))


def camera_model(path: Path) -> str | None:
    """Which camera took this frame, from its own EXIF, or None.

    The same two roads as capture_time: the image's EXIF where PIL can
    open it, else the EXIF block of the preview a raw file carries.
    Cached against size and mtime -- a per-camera look asks this for
    every render of every frame.
    """
    path = Path(path)
    try:
        status = path.stat()
    except OSError:
        return None
    return _camera_model(str(path), (status.st_size, status.st_mtime_ns))


@lru_cache(maxsize=8192)
def _tiff_capture_time(path: Path) -> float | None:
    """The moment of exposure recorded in a TIFF's own tags.

    A developer's export keeps the camera's timestamp but writes a
    file the imaging library will not always open -- a float TIFF, in
    particular, it refuses outright. The tags are still there and are
    still the truth about when the shutter opened, which is what a
    sequence of night frames is registered by.
    """
    if path.suffix.casefold() not in {".tif", ".tiff"}:
        return None
    try:
        import tifffile
    except ImportError:
        return None
    try:
        with tifffile.TiffFile(path) as opened:
            tags = {tag.name: tag.value for tag in opened.pages[0].tags}
    except Exception:                                # noqa: BLE001 - unreadable
        return None
    inner = tags.get("ExifTag")
    values = []
    if isinstance(inner, dict):
        values.append(inner.get("DateTimeOriginal"))
    # The top-level DateTime is when the file was WRITTEN, which for an
    # export is today rather than the night in question. It is last.
    values.append(tags.get("DateTime"))
    for value in values:
        stamp = _stamp(value) if value else None
        if stamp is not None:
            return stamp
    return None


def _capture_time(path: str, _stat: tuple[int, int]) -> float | None:
    try:
        with Image.open(path) as image:
            found = _from_exif(image.getexif())
    except Exception:                                # noqa: BLE001 - not an image
        found = None
    if found is None:
        found = _tiff_capture_time(Path(path))
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


# The map is written beside the photographs, under the house folder, so it
# travels with them: a folder moved to another disk keeps its answers,
# because they are keyed by filename rather than by path.
CAPTURE_TIMES_FORMAT = "opencull-capture-times-v1"
_CAPTURE_TIMES_FILE = "capture-times.json"


def _capture_times_path(root: Path) -> Path:
    return Path(root) / ".darkimiya" / _CAPTURE_TIMES_FILE


def _load_capture_times(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != CAPTURE_TIMES_FORMAT:
        return {}
    frames = data.get("frames")
    return frames if isinstance(frames, dict) else {}


def capture_times(root: Path, names: list[str]) -> dict[str, float | None]:
    """Capture times for many frames of one folder, read once and kept.

    The per-file scan is cheap now, but a fresh process still pays it for
    every frame each time the folder is opened -- and opening a folder is
    the very thing that was slow. So the answers are written beside the
    photographs, keyed by each file's size and modification time: a reopen
    stats the files (metadata, no content) and reads only the frames that
    are new or have changed since. Frames whose size and mtime still match
    are answered from the map without touching the file at all.

    The call is given the frames it needs, which may be a subset of the
    folder; entries it does not ask about are left in the map untouched,
    so a partial call never discards another view's cached answers. An
    entry for a file that has since been deleted simply lingers, harmless
    and tiny, until a frame of that name is asked for again.
    """
    root = Path(root)
    sidecar = _capture_times_path(root)
    stored = _load_capture_times(sidecar)
    merged = dict(stored)
    out: dict[str, float | None] = {}
    changed = False
    for name in names:
        try:
            status = (root / name).stat()
        except OSError:
            out[name] = None
            continue
        size, mtime_ns = status.st_size, status.st_mtime_ns
        record = stored.get(name)
        if (isinstance(record, dict) and record.get("size") == size
                and record.get("mtime_ns") == mtime_ns):
            out[name] = record.get("time")
            continue
        found = _capture_time(str(root / name), (size, mtime_ns))
        out[name] = found
        merged[name] = {"size": size, "mtime_ns": mtime_ns, "time": found}
        changed = True
    if changed:
        try:
            _atomic_json(sidecar, {"format": CAPTURE_TIMES_FORMAT,
                                   "frames": merged})
        except OSError:                              # a read-only folder still
            pass                                     # works, just uncached
    return out


def _sequence(name: str) -> int | None:
    """The frame counter in a camera filename, if there is one."""
    match = re.search(r"(\d+)(?!.*\d)", Path(name).stem)
    return int(match.group(1)) if match else None


def shooting_order(names: list[str], taken: dict[str, float | None]) -> list[str]:
    """Frames in the order they were shot, from the clock and the counter.

    The clock leads. But EXIF is one-second resolution and a burst
    puts several frames in one second, so within a tied second the
    camera's counter decides -- and the counter WRAPS: after 9999 the
    next frame is 1001 (or 0001), so among frames tied on the clock,
    a counter thousands lower than its neighbours' is the later frame,
    not the earlier. Read plainly, that second's frames come out
    9998, 9999, 1001, 1002 -- the order the shutter went. Read by name
    they came out 1001, 9998, 9999, which put the wrap's first frame
    before the frames shot just before it. Frames with no clock at all
    fall after the timed ones, by name.
    """
    def counter(name: str) -> int:
        found = _sequence(name)
        return found if found is not None else -1

    def key(name: str) -> tuple:
        when = taken.get(name)
        return (0 if when is not None else 1,
                when if when is not None else 0.0,
                name.casefold())

    ordered = sorted(names, key=key)
    # Within each tied second, place by counter, unwrapping. The wrap
    # is where a counter drops by more than half its span from the
    # previous frame; everything after it, within the tie, sorts as if
    # the counter had kept counting.
    result: list[str] = []
    index = 0
    while index < len(ordered):
        stamp = taken.get(ordered[index])
        run = [ordered[index]]
        index += 1
        while (index < len(ordered) and stamp is not None
               and taken.get(ordered[index]) == stamp):
            run.append(ordered[index])
            index += 1
        if len(run) > 1 and stamp is not None:
            counters = [counter(name) for name in run]
            if all(value >= 0 for value in counters):
                span = max(counters) - min(counters)
                if span > 5000:
                    # A wrap inside this second: the small numbers are
                    # the later frames. Lift them past the large ones.
                    pivot = min(counters) + span / 2
                    run.sort(key=lambda name: (
                        counter(name) < pivot, counter(name)))
                else:
                    run.sort(key=counter)
        result.extend(run)
    return result


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

    # One persisted read for the whole group, rather than a file touch per
    # entry: the grouping is recomputed every time a page that shows scenes
    # is opened, and this is what keeps that free after the first time.
    clock = (capture_times(Path(photos_root),
                           [str(entry["photo"]) for entry in ordered])
             if photos_root is not None else {})

    def times(entry: dict[str, Any]) -> float | None:
        return clock.get(str(entry["photo"]))

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


def detect_keyframes(shortlist: Any, reviews: Any,
                     recipes: Path) -> dict[str, Any]:
    """Find the scenes locally and mark each scene's keyframe to develop.

    Free: the grouping is the same local similarity the suggestion
    scoping uses, and the keyframe is each scene's best-ranked frame.
    Marks are added, never taken -- a frame the photographer already
    marked stays marked, whatever scene it fell into -- and the scene
    plan is written beside the recipes so the AI editing phase shares
    one answer per scene without being asked twice.
    """
    photos = [str(entry["photo"]) for entry in shortlist.entries]
    plan = plan_for(shortlist, photos)
    keyframes = [scene["representative"] for scene in plan]
    state = reviews.public_state()
    already = sum(
        1 for photo in keyframes
        if state.get("entries", {}).get(photo, {}).get("interesting")
        is True)
    # One revision, one write, whatever the count: the store's own bulk
    # mark leaves every other field of every entry exactly as it was.
    reviews.mark_all(True, state.get("revision"), photos=keyframes)
    marked = len(keyframes) - already
    written = write_plan(
        Path(recipes), shortlist.path,
        int(reviews.public_state().get("revision") or 0), plan)
    return {
        "scenes": len(plan),
        "keyframes": keyframes,
        "marked": marked,
        "already_marked": already,
        "plan_path": str(written),
    }


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
    # Which report each answer came from, newest first, so a scene's
    # siblings can tell whether the representative now speaks from a
    # later answer than their own.
    age: dict[str, int] = {}
    style_sha = ""
    for rank, (_path, report) in enumerate(index.reports(sha)):
        for entry in report.get("entries", []) or []:
            photo = entry.get("photo") if isinstance(entry, dict) else None
            if isinstance(photo, str) and photo not in known:
                known[photo] = entry
                age[photo] = rank
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
            # A sibling keeps its own answer, unless the frame that speaks
            # for its scene has since been asked again. Re-asking a scene
            # is meant to re-treat the scene; leaving the siblings on
            # answers from an older run would share nothing.
            stale = (photo in age and representative in age
                     and age[representative] < age[photo])
            if photo == representative or (photo in known and not stale):
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
