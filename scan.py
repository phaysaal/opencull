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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import ExifTags, Image, ImageOps

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
        (path for path in iterator if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS),
        key=lambda path: path.name.casefold(),
    )


def raw_preview(path: Path) -> Image.Image:
    if rawpy is None:
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
            "RAW decoding needs optional rawpy or the macOS `sips` tool")
    with tempfile.TemporaryDirectory(prefix="opencull-raw-") as directory:
        output = Path(directory) / "preview.jpg"
        result = subprocess.run(
            [
                executable, "-s", "format", "jpeg", "-Z", "1024",
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


def measure(path: Path, root: Path) -> Measured:
    image = open_preview(path)
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
    return Measured(
        path=path,
        name=path.relative_to(root).as_posix(),
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
        sha256_prefix=file_sha256_prefix(path),
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
) -> str:
    """Read a photo directory and return its complete manifest as JSON.

    This is the Kimiya-facing kernel function: deterministic for a fixed
    directory state, read-only, and free of output-file side effects.
    """
    root = Path(str(directory)).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")

    paths = image_files(root, bool(recursive))
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
