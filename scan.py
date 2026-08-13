#!/usr/bin/env python3
"""Audited Python kernel functions for OpenCull photo scanning.

Kimiya loads this file with ``use python "scan.py"`` and calls
``scan_directory`` directly. The command-line interface is retained as a thin
wrapper for inspecting or exporting a manifest independently. Scanning never
modifies source files.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import ExifTags, Image, ImageOps

from colour_profile import srgb_profile

try:
    import rawpy
except ImportError:  # JPEG-only use remains possible without rawpy.
    rawpy = None


RAW_EXTENSIONS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".dng", ".erf", ".fff", ".iiq",
    ".kdc", ".mef", ".mos", ".mrw", ".nef", ".nrw", ".orf", ".pef",
    ".raf", ".raw", ".rw2", ".rwl", ".sr2", ".srf", ".srw", ".x3f",
}
BITMAP_EXTENSIONS = {".avif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | BITMAP_EXTENSIONS
DATETIME_TAGS = {"DateTimeOriginal", "DateTimeDigitized", "DateTime"}
MANAGED_PROJECT_DIRECTORY_NAMES = {"darkimiya", ".darkimiya", ".opencull"}
# Order matters for writing: a folder that already carries a visible-era
# directory keeps using it; a fresh folder gets the hidden one.
MANAGED_DIRECTORY_CANDIDATES = (".darkimiya", "Darkimiya", ".opencull")
PREVIEW_BOUND = 2048


def managed_directory(root: Path) -> Path:
    for name in MANAGED_DIRECTORY_CANDIDATES:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return root / MANAGED_DIRECTORY_CANDIDATES[0]


def preview_cache_name(relative: str, sha256_prefix: str,
                       spectrum: str = "visible") -> str:
    """One flat, content-keyed name per source frame.

    An infrared frame is materialised as its own file rather than over
    the camera's own rendering. They are different pictures of the same
    capture, and a shoot marked infrared by mistake and marked back would
    otherwise be left looking at the wrong one.
    """
    mark = ".infrared" if str(spectrum) == "infrared" else ""
    return (f"{relative.replace('/', '__')}."
            f"{sha256_prefix[:8]}{mark}.preview.jpg")


def visible_photograph(root: Path, relative: str,
                       spectrum: str = "visible") -> Path:
    """The file a model can actually be shown for one frame.

    Raw sensor bytes are not an image to an image reader. The scanner
    materializes a content-keyed preview for every RAW frame under the
    project's Previews area; this resolves to that preview when the
    original is RAW, and to the original itself otherwise.
    """
    original = root / relative
    if original.suffix.lower() not in RAW_EXTENSIONS:
        return original
    flat = relative.replace("/", "__")
    # An infrared album has its own rendering of every frame, and that is
    # the one worth showing: the camera's is a guess about light its own
    # filter removed. Where one has not been made, the camera's stands
    # rather than nothing at all.
    wanted = ([f"{flat}.*.infrared.preview.jpg"]
              if str(spectrum) == "infrared" else [])
    for pattern in [*wanted, f"{flat}.*.preview.jpg"]:
        for managed in MANAGED_DIRECTORY_CANDIDATES:
            previews = root / managed / "Previews"
            if not previews.is_dir():
                continue
            matches = sorted(
                path for path in previews.glob(pattern)
                # The plain pattern also matches the infrared file, which
                # must never be served to somebody who asked for the
                # camera's own rendering.
                if str(spectrum) == "infrared"
                or not path.name.endswith(".infrared.preview.jpg"))
            if matches:
                return matches[-1]
    return original


@dataclass
class Measured:
    path: Path
    name: str
    captured: str
    timestamp: float
    width: int
    height: int
    dhash: int
    technical_score: float
    sharpness: float
    exposure: float
    contrast: float
    clipping: float
    composition_proxy: float
    sha256_prefix: str
    preview: str = ""

    def manifest_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.name,
            "captured": self.captured,
            "width": self.width,
            "height": self.height,
            "technical_score": round(self.technical_score, 2),
            "sharpness": round(self.sharpness, 2),
            "exposure": round(self.exposure, 2),
            "contrast": round(self.contrast, 2),
            "clipping": round(self.clipping, 3),
            "composition_proxy": round(self.composition_proxy, 2),
            "sha256_prefix": self.sha256_prefix,
            # Present only for frames whose original no image reader can
            # open: a JPEG manifest stays byte-identical to what it was
            # before previews existed, and its checkpoints stay valid.
            **({"preview": self.preview} if self.preview else {}),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory containing photographs")
    parser.add_argument("-o", "--output", type=Path, default=Path("manifest.json"))
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--time-window", type=float, default=8.0, help="maximum seconds within a burst")
    parser.add_argument("--hash-distance", type=int, default=14, help="maximum 64-bit dHash distance")
    parser.add_argument("--comparison-window", type=int, default=16, help="previous photographs compared")
    return parser.parse_args()


def image_files(directory: Path, recursive: bool) -> list[Path]:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        (
            path for path in iterator
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
            and not (
                path.relative_to(directory).parts
                and path.relative_to(directory).parts[0].casefold()
                in MANAGED_PROJECT_DIRECTORY_NAMES
            )
        ),
        key=lambda path: path.name.casefold(),
    )


RAW_DECODER_ADVICE = (
    "Install rawpy for fast RAW previews: pip install rawpy"
)
SIPS_PREVIEW_EDGE = 2560
_sips_fallback_announced = False


def raw_decoder_status() -> dict[str, Any]:
    """Describe the RAW decoder this process will use.

    The `sips` path spawns one subprocess per photograph and decodes the whole
    frame, where rawpy reads the preview already embedded in the file. The
    difference is large enough that a silent fallback reads to the user as a
    broken application, so callers surface this in the interface.
    """
    if rawpy is not None:
        return {
            "decoder": "rawpy",
            "available": True,
            "degraded": False,
            "detail": "rawpy reads embedded RAW previews directly.",
        }
    if shutil.which("sips"):
        return {
            "decoder": "sips",
            "available": True,
            "degraded": True,
            "detail": (
                "rawpy is missing, so RAW previews fall back to the macOS "
                f"`sips` tool: one subprocess per photograph. {RAW_DECODER_ADVICE}"
            ),
        }
    return {
        "decoder": None,
        "available": False,
        "degraded": True,
        "detail": f"No RAW decoder is available. {RAW_DECODER_ADVICE}",
    }


def _announce_sips_fallback() -> None:
    global _sips_fallback_announced
    if _sips_fallback_announced:
        return
    _sips_fallback_announced = True
    warnings.warn(raw_decoder_status()["detail"], RuntimeWarning, stacklevel=3)


def classify_folder(
    directory: Path, recursive: bool = True, samples: int = 3,
) -> dict[str, Any]:
    """Count what kinds of photograph a folder holds, and name a few.

    Which treatment a folder can receive follows from this: RAW files can be
    developed, rendered bitmaps can only be edited, and a folder holding both
    can do either. Counting extensions is a directory walk with no decoding,
    so it stays cheap on a large shoot.

    The sampled names are for showing the folder rather than describing it.
    They are spread across the folder instead of taken from the front, because
    the first few frames of a shoot are usually the least representative of it.
    """
    raw: list[Path] = []
    bitmap: list[Path] = []
    for path in image_files(directory, recursive):
        suffix = path.suffix.lower()
        if suffix in RAW_EXTENSIONS:
            raw.append(path)
        elif suffix in BITMAP_EXTENSIONS:
            bitmap.append(path)
    if raw and bitmap:
        kind = "mixed"
    elif raw:
        kind = "raw"
    elif bitmap:
        kind = "bitmap"
    else:
        kind = "empty"
    # Prefer rendered files for the sample: they decode without a RAW library
    # and are what the camera itself chose to show.
    pool = bitmap or raw
    chosen: list[Path] = []
    if pool and samples > 0:
        if len(pool) <= samples:
            chosen = list(pool)
        else:
            step = len(pool) / samples
            chosen = [pool[min(len(pool) - 1, int(index * step))]
                      for index in range(samples)]
    return {
        "raw": len(raw),
        "bitmap": len(bitmap),
        "total": len(raw) + len(bitmap),
        "kind": kind,
        "samples": [
            path.relative_to(directory).as_posix() for path in chosen],
    }


def raw_preview(path: Path) -> Image.Image:
    if rawpy is None:
        _announce_sips_fallback()
        return sips_preview(path)
    with rawpy.imread(str(path)) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(thumb.data)).convert("RGB")
            return Image.fromarray(thumb.data).convert("RGB")
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            try:
                rgb = raw.postprocess(
                    half_size=True, use_camera_wb=True, no_auto_bright=True)
                return Image.fromarray(rgb).convert("RGB")
            except Exception:
                return sips_preview(path)


def sips_preview(path: Path) -> Image.Image:
    executable = shutil.which("sips")
    if not executable:
        raise RuntimeError(
            f"RAW decoding needs rawpy or the macOS `sips` tool. {RAW_DECODER_ADVICE}")
    with tempfile.TemporaryDirectory(prefix="opencull-raw-") as directory:
        output = Path(directory) / "preview.jpg"
        result = subprocess.run(
            [
                executable, "-s", "format", "jpeg", "-Z", str(SIPS_PREVIEW_EDGE),
                str(path), "--out", str(output),
            ],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0 or not output.exists():
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"sips could not decode {path.name}: {detail[:200]}")
        with Image.open(output) as image:
            return image.convert("RGB").copy()


def open_preview(path: Path) -> Image.Image:
    if path.suffix.lower() in RAW_EXTENSIONS:
        return raw_preview(path)
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def capture_time(image: Image.Image, path: Path) -> tuple[str, float]:
    try:
        exif = image.getexif()
        for key, value in exif.items():
            if ExifTags.TAGS.get(key) in DATETIME_TAGS and value:
                parsed = datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
                return parsed.isoformat(), parsed.timestamp()
    except (AttributeError, TypeError, ValueError):
        pass
    timestamp = path.stat().st_mtime
    return datetime.fromtimestamp(timestamp).isoformat(), timestamp


def dhash(gray: np.ndarray) -> int:
    image = Image.fromarray(np.uint8(np.clip(gray * 255.0, 0, 255)))
    small = np.asarray(image.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.float32)
    bits = small[:, 1:] > small[:, :-1]
    value = 0
    for bit in bits.flat:
        value = (value << 1) | int(bit)
    return value


def clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def file_sha256_prefix(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


# How an infrared frame is made judgeable. The channels are brought into
# agreement -- past the filter's cut-off they recorded nearly the same
# light, so the cast is the filter and not the scene -- and the result is
# scaled so its brightest real tone reaches near the top of the range. A
# 760nm frame arrives at eighteen levels out of 255 with a heavy blue
# cast, and nobody, model or person, can judge composition through that.
INFRARED_HEADROOM = 0.92
INFRARED_QUANTILE = 99.0
_ENCODE_GAMMA = 2.2


def neutralized(image: Image.Image) -> Image.Image:
    """One infrared frame, shown as what the sensor recorded.

    Done in linear light, because a per-channel gain applied to
    gamma-encoded numbers scales the encoding rather than the light.
    """
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    linear = np.power(np.clip(rgb, 0.0, 1.0), _ENCODE_GAMMA)
    averages = linear.reshape(-1, 3).mean(axis=0)
    target = float(averages.mean())
    if target > 1e-6:
        linear = linear * np.where(
            averages > 1e-6, target / np.maximum(averages, 1e-6), 1.0)
    ceiling = float(np.percentile(linear, INFRARED_QUANTILE))
    if ceiling > 1e-6:
        linear = linear * (INFRARED_HEADROOM / ceiling)
    encoded = np.power(np.clip(linear, 0.0, 1.0), 1.0 / _ENCODE_GAMMA)
    return Image.fromarray(np.uint8(np.clip(encoded * 255 + 0.5, 0, 255)))


def measure(path: Path, root: Path, spectrum: str = "visible") -> Measured:
    image = open_preview(path)
    if str(spectrum) == "infrared":
        # Measured on the frame as it will be shown and developed, not on
        # the camera's guess. Exposure and clipping read from an
        # unbalanced infrared capture describe the filter, not the
        # photograph, and they are handed to the model as evidence.
        image = neutralized(image)
    captured, timestamp = capture_time(image, path)
    width, height = image.size
    preview = ImageOps.contain(image, (768, 768), Image.Resampling.LANCZOS)
    rgb = np.asarray(preview, dtype=np.float32) / 255.0
    gray = np.dot(rgb[..., :3], np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))

    gx = np.diff(gray, axis=1)
    gy = np.diff(gray, axis=0)
    gradient_energy = float(np.mean(gx * gx) + np.mean(gy * gy))
    sharpness = clamp(100.0 * (1.0 - math.exp(-gradient_energy * 180.0)))

    median = float(np.median(gray))
    exposure = clamp(100.0 - abs(median - 0.45) * 180.0)
    p05, p95 = np.percentile(gray, [5, 95])
    contrast = clamp(float(p95 - p05) * 125.0)
    clipping = float(np.mean((gray <= 0.01) | (gray >= 0.99)) * 100.0)

    # Explainable proxy, not a claim of artistic merit: reward edge energy
    # near thirds intersections relative to a uniform grid.
    edge = np.zeros_like(gray)
    edge[:, 1:] += np.abs(gx)
    edge[1:, :] += np.abs(gy)
    h, w = edge.shape
    yy, xx = np.mgrid[0:h, 0:w]
    sigma = max(1.0, min(h, w) * 0.16)
    weight = np.zeros_like(edge)
    for fx in (1 / 3, 2 / 3):
        for fy in (1 / 3, 2 / 3):
            weight += np.exp(-((xx - fx * w) ** 2 + (yy - fy * h) ** 2) / (2 * sigma**2))
    weighted = float(np.sum(edge * weight) / (np.sum(edge) + 1e-9))
    composition_proxy = clamp(weighted * 85.0)

    technical = clamp(
        0.45 * sharpness
        + 0.25 * exposure
        + 0.20 * contrast
        + 0.10 * composition_proxy
        - min(20.0, clipping * 0.8)
    )
    relative = path.relative_to(root).as_posix()
    sha256_prefix = file_sha256_prefix(path)
    preview_relative = ""
    if path.suffix.lower() in RAW_EXTENSIONS:
        # A model asked to look at a photograph must be handed something an
        # image reader can open; raw sensor bytes are not that. The camera's
        # embedded rendering is materialized once per frame, content-keyed,
        # into the project's own Previews area.
        managed = managed_directory(root)
        cache = managed / "Previews" / preview_cache_name(
            relative, sha256_prefix, spectrum)
        if not cache.is_file():
            cache.parent.mkdir(parents=True, exist_ok=True)
            bounded = ImageOps.contain(
                image, (PREVIEW_BOUND, PREVIEW_BOUND),
                Image.Resampling.LANCZOS)
            bounded.save(cache, "JPEG", quality=88, icc_profile=srgb_profile())
        preview_relative = (
            f"{managed.name}/Previews/{cache.name}")
    return Measured(
        path=path,
        name=relative,
        captured=captured,
        timestamp=timestamp,
        width=width,
        height=height,
        dhash=dhash(gray),
        technical_score=technical,
        sharpness=sharpness,
        exposure=exposure,
        contrast=contrast,
        clipping=clipping,
        composition_proxy=composition_proxy,
        sha256_prefix=sha256_prefix,
        preview=preview_relative,
    )


def group_similar(
    photos: list[Measured], time_window: float, hash_distance: int, comparison_window: int
) -> list[list[Measured]]:
    parent = list(range(len(photos)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    ordered = sorted(enumerate(photos), key=lambda item: (item[1].timestamp, item[1].name))
    for position, (index, photo) in enumerate(ordered):
        start = max(0, position - max(1, comparison_window))
        for other_index, other in ordered[start:position]:
            if abs(photo.timestamp - other.timestamp) > time_window:
                continue
            distance = (photo.dhash ^ other.dhash).bit_count()
            same_shape = abs(photo.width / photo.height - other.width / other.height) < 0.03
            if same_shape and distance <= hash_distance:
                union(index, other_index)

    buckets: dict[int, list[Measured]] = {}
    for index, photo in enumerate(photos):
        buckets.setdefault(find(index), []).append(photo)
    groups = list(buckets.values())
    for group in groups:
        group.sort(key=lambda item: (-item.technical_score, item.name))
    groups.sort(key=lambda group: min(item.timestamp for item in group))
    return groups


def scan_directory(
    directory: str,
    recursive: bool = False,
    time_window: float = 8.0,
    hash_distance: float = 14,
    comparison_window: float = 16,
    only_photos: str = "[]",
) -> str:
    """Read a photo directory and return its complete manifest as JSON.

    This is the Kimiya-facing kernel function: deterministic for a fixed
    directory state. Source photographs are read only and never modified;
    the one thing written is a content-keyed preview cache for RAW frames,
    inside the project's own managed directory, so that models asked to
    look at a photograph can be handed something an image reader opens.

    ``only_photos`` is a JSON list of root-relative names: the photographer's
    prefilter, applied before anything is measured so an excluded frame is
    never read at all. A name the folder does not hold is an error rather
    than a silent shrink -- a run must never quietly cover less than it was
    asked to. An empty list leaves the manifest byte-identical to an
    unfiltered scan, so existing checkpoints keyed on the manifest hash
    stay valid.
    """
    root = Path(str(directory)).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")

    try:
        wanted_list = json.loads(str(only_photos) or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"only_photos must be a JSON list of photograph names: {exc}"
        ) from exc
    if not isinstance(wanted_list, list) or any(
        not isinstance(name, str) for name in wanted_list
    ):
        raise ValueError("only_photos must be a JSON list of photograph names")
    wanted = {name for name in wanted_list if name.strip()}

    paths = image_files(root, bool(recursive))
    if wanted:
        names = {path.relative_to(root).as_posix() for path in paths}
        unknown = sorted(wanted - names)
        if unknown:
            raise ValueError(
                "only_photos names not present in the folder: "
                + ", ".join(unknown[:5])
                + ("…" if len(unknown) > 5 else ""))
        paths = [
            path for path in paths
            if path.relative_to(root).as_posix() in wanted
        ]
    if not paths:
        raise ValueError(f"no supported photographs in {root}")

    measured: list[Measured] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            measured.append(measure(path, root))
        except Exception as exc:
            errors.append({
                "name": path.relative_to(root).as_posix(),
                "error": f"{type(exc).__name__}: {exc}",
            })

    groups = group_similar(
        measured,
        float(time_window),
        int(hash_distance),
        int(comparison_window),
    )
    manifest = {
        "format": "opencull-manifest-v1",
        "source_directory": os.fspath(root),
        "settings": {
            "time_window_seconds": float(time_window),
            "hash_distance": int(hash_distance),
            "comparison_window": int(comparison_window),
            "recursive": bool(recursive),
            # Recorded only when a prefilter was applied: a full scan's
            # manifest -- and every checkpoint keyed on its hash -- must not
            # change because this parameter now exists.
            **({"only_photos": sorted(wanted)} if wanted else {}),
        },
        "groups": [
            {
                "id": f"group-{number:04d}",
                "candidate_count": len(group),
                "candidates": [photo.manifest_record() for photo in group],
            }
            for number, group in enumerate(groups, start=1)
        ],
        "errors": errors,
        "photo_count": len(measured),
        "safety": "Source files were read only and were not modified.",
    }
    return json.dumps(manifest, indent=2, sort_keys=True)


def main() -> int:
    args = parse_args()
    try:
        text = scan_directory(
            os.fspath(args.directory),
            args.recursive,
            args.time_window,
            args.hash_distance,
            args.comparison_window,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    manifest = json.loads(text)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text + "\n")
    print(
        f"wrote {args.output}: {manifest['photo_count']} photos, "
        f"{len(manifest['groups'])} groups, {len(manifest['errors'])} errors",
        file=sys.stderr,
    )
    return 0 if manifest["photo_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
