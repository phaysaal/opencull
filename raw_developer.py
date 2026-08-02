"""Safe LibRaw baseline decoder for OpenCull RAW development."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


BASELINE_FORMAT = "opencull-raw-baseline-v1"
Runner = Callable[..., subprocess.CompletedProcess[str]]


class RawDevelopmentError(RuntimeError):
    """RAW decoding or provenance validation failed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tool(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise RawDevelopmentError(f"required tool is unavailable: {name}")
    return value


def _run(
    command: list[str], runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    result = runner(
        command, capture_output=True, text=True, check=False, timeout=600)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RawDevelopmentError(
            f"{Path(command[0]).name} failed with status "
            f"{result.returncode}: {detail}")
    return result


def parse_identification(text: str) -> dict[str, Any]:
    patterns = {
        "camera": r"(?m)^Camera:\s*(.+?)(?:\s+ID:.*)?$",
        "iso": r"(?m)^ISO speed:\s*(\d+)",
        "raw_images": r"(?m)^Number of raw images:\s*(\d+)",
        "full_size": r"(?m)^Full size:\s*(\d+)\s*x\s*(\d+)",
        "active_size": (
            r"(?m)^Raw inset, width x height:\s*(\d+)\s*x\s*(\d+)"
            r"\s+left:\s*(\d+)\s+top:\s*(\d+)"),
        "filter_pattern": r"(?m)^Filter pattern:\s*(\S+)",
        "black_repeat": r"(?m)^BlackLevelRepeatDim:\s*(\d+)\s*x\s*(\d+)",
        "lens": r"(?m)^Lens:\s*(.+)$",
    }
    result: dict[str, Any] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if not match:
            continue
        values = match.groups()
        if key in {"full_size", "active_size", "black_repeat"}:
            result[key] = [int(value) for value in values]
        elif key in {"iso", "raw_images"}:
            result[key] = int(values[0])
        else:
            result[key] = values[0].strip()
    required = {"camera", "full_size", "active_size", "filter_pattern"}
    missing = sorted(required - result.keys())
    if missing:
        raise RawDevelopmentError(
            f"LibRaw identification omitted: {', '.join(missing)}")
    return result


def identify_raw(
    source: Path, runner: Runner = subprocess.run,
    identify_tool: str | None = None,
) -> dict[str, Any]:
    resolved = source.expanduser().resolve()
    if not resolved.is_file():
        raise RawDevelopmentError(f"RAW source is unavailable: {resolved}")
    executable = identify_tool or _tool("raw-identify")
    result = _run([executable, "-v", str(resolved)], runner)
    return parse_identification(result.stdout)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_baseline(
    source: Path,
    output_dir: Path,
    runner: Runner = subprocess.run,
    dcraw_tool: str | None = None,
    identify_tool: str | None = None,
    image_tool: str | None = None,
) -> dict[str, Any]:
    raw = source.expanduser().resolve()
    destination = output_dir.expanduser().resolve()
    if not raw.is_file():
        raise RawDevelopmentError(f"RAW source is unavailable: {raw}")
    if destination == raw.parent:
        raise RawDevelopmentError(
            "baseline output must not be written beside the original RAW")
    destination.mkdir(parents=True, exist_ok=True)
    dcraw = dcraw_tool or _tool("dcraw_emu")
    identify = identify_tool or _tool("raw-identify")
    magick = image_tool or _tool("magick")
    metadata = identify_raw(raw, runner, identify)
    source_hash = sha256_file(raw)
    stem = raw.stem
    linear = destination / f"{stem}.baseline-linear-rec2020.tiff"
    preview = destination / f"{stem}.baseline-preview.jpg"
    provenance = destination / f"{stem}.baseline.json"

    with tempfile.TemporaryDirectory(
        prefix=".opencull-raw-", dir=destination
    ) as temporary:
        work = Path(temporary)
        linear_tmp = work / linear.name
        display_tmp = work / f"{stem}.display-srgb.tiff"
        preview_tmp = work / preview.name
        linear_command = [
            dcraw, "-T", "-4", "-W", "-w", "+M", "-o", "8",
            "-q", "3", "-H", "1", "-Z", str(linear_tmp), str(raw),
        ]
        display_command = [
            dcraw, "-T", "-6", "-W", "-w", "+M", "-o", "1",
            "-q", "3", "-H", "1", "-g", "2.4", "12.92",
            "-Z", str(display_tmp), str(raw),
        ]
        _run(linear_command, runner)
        _run(display_command, runner)
        _run([
            magick, str(display_tmp), "-auto-orient", "-resize",
            "2048x2048>", "-quality", "92", str(preview_tmp),
        ], runner)
        if not linear_tmp.is_file() or not preview_tmp.is_file():
            raise RawDevelopmentError("decoder did not produce expected outputs")
        os.replace(linear_tmp, linear)
        os.replace(preview_tmp, preview)

    record = {
        "format": BASELINE_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(raw),
            "sha256": source_hash,
            "size": raw.stat().st_size,
        },
        "decoder": {
            "name": "LibRaw dcraw_emu",
            "executable": dcraw,
            "linear_profile": {
                "bit_depth": 16,
                "transfer": "linear",
                "pixel_primaries": "Rec.2020 D65",
                "icc_profile_embedded": True,
                "icc_profile": "Rec.2020 gamma 1 / linear",
                "camera_white_balance": True,
                "camera_matrix": True,
                "automatic_brightness": False,
                "highlight_mode": "unclip",
                "interpolation_quality": 3,
            },
        },
        "camera": metadata,
        "outputs": {
            "linear_tiff": {
                "path": str(linear),
                "sha256": sha256_file(linear),
                "size": linear.stat().st_size,
            },
            "preview_jpeg": {
                "path": str(preview),
                "sha256": sha256_file(preview),
                "size": preview.stat().st_size,
            },
        },
    }
    _atomic_json(provenance, record)
    record["provenance_path"] = str(provenance)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = render_baseline(args.source, args.output_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
