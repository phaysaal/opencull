"""Isolated darktable command-line adapter for renderer research."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DARKTABLE_FORMAT = "opencull-darktable-render-v1"
DARKTABLE_MACOS_CLI = Path(
    "/Applications/darktable.app/Contents/MacOS/darktable-cli")
DemosaicMode = str
DEMOSAIC_METHODS: dict[DemosaicMode, tuple[str, int]] = {
    "markesteijn-1-pass": ("Markesteijn 1-pass", 1024 | 1),
    "markesteijn-3-pass": ("Markesteijn 3-pass", 1024 | 2),
    "markesteijn-3-pass-vng": ("Markesteijn 3-pass + VNG dual", 2048 | 1024 | 2),
}
Runner = Callable[..., subprocess.CompletedProcess[str]]


class DarktableError(RuntimeError):
    """darktable is unavailable or did not produce a trustworthy artifact."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_darktable_cli(explicit: str | Path | None = None) -> Path:
    if explicit:
        resolved = Path(explicit).expanduser().resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
        raise DarktableError(f"configured darktable-cli is unavailable: {resolved}")
    candidates: list[Path] = []
    discovered = shutil.which("darktable-cli")
    if discovered:
        candidates.append(Path(discovered))
    candidates.append(DARKTABLE_MACOS_CLI)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise DarktableError(
        "darktable-cli is unavailable; install darktable or configure its executable")


def darktable_version(
    executable: Path, runner: Runner = subprocess.run,
) -> str:
    try:
        result = runner(
            [str(executable), "--version"], capture_output=True, text=True,
            check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DarktableError(f"cannot start darktable-cli: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic output").strip()
        raise DarktableError(
            f"darktable-cli version probe failed with status {result.returncode}: {detail}")
    match = re.search(r"(?m)^darktable\s+([^\s]+)", result.stdout)
    if not match:
        raise DarktableError("darktable-cli returned an unrecognized version response")
    return match.group(1)


def _xmp_packet(jpeg: Path) -> str:
    data = jpeg.read_bytes()
    start = data.find(b"<?xpacket begin=")
    end_marker = b'<?xpacket end="w"?>'
    end = data.find(end_marker, start)
    if start < 0 or end < 0:
        raise DarktableError("darktable output omitted its processing-history XMP")
    return data[start:end + len(end_marker)].decode("utf-8", errors="strict")


def _demosaic_params(packet: str) -> tuple[str, int]:
    match = re.search(
        r'(darktable:operation="demosaic"[^>]*darktable:params=")([0-9a-fA-F]+)(")',
        packet)
    if not match:
        raise DarktableError("darktable history omitted uncompressed demosaic parameters")
    raw = bytes.fromhex(match.group(2))
    if len(raw) < 16:
        raise DarktableError("darktable demosaic parameter block is too short")
    return match.group(2), struct.unpack_from("<I", raw, 12)[0]


def _xmp_with_demosaic(packet: str, method: int) -> str:
    encoded, _ = _demosaic_params(packet)
    raw = bytearray.fromhex(encoded)
    struct.pack_into("<I", raw, 12, method)
    return packet.replace(encoded, raw.hex(), 1)


def render_darktable_default(
    source: Path,
    output_dir: Path,
    *,
    executable: str | Path | None = None,
    runner: Runner = subprocess.run,
    max_dimension: int | None = None,
    demosaic_mode: DemosaicMode = "markesteijn-1-pass",
) -> dict[str, Any]:
    """Render with darktable defaults in disposable catalog/config state.

    The source is exposed through a temporary symlink so darktable cannot find
    or create a source-adjacent XMP sidecar. This baseline intentionally does
    not claim to execute an OpenCull recipe; it is the native-engine control in
    paired renderer experiments.
    """
    source = source.expanduser().resolve()
    destination = output_dir.expanduser().resolve()
    if not source.is_file():
        raise DarktableError(f"development source is unavailable: {source}")
    if destination == source.parent:
        raise DarktableError("darktable output must not be written beside the source")
    destination.mkdir(parents=True, exist_ok=True)
    if demosaic_mode not in DEMOSAIC_METHODS:
        raise DarktableError(f"unsupported darktable demosaic mode: {demosaic_mode}")
    demosaic_label, demosaic_method = DEMOSAIC_METHODS[demosaic_mode]
    is_jpeg = source.suffix.casefold() in {".jpg", ".jpeg"}
    cli = find_darktable_cli(executable)
    version = darktable_version(cli, runner)
    output = destination / f"{source.stem}.darktable-{demosaic_mode}.jpg"
    provenance = destination / f"{source.stem}.darktable-{demosaic_mode}.render.json"

    with tempfile.TemporaryDirectory(prefix=".darktable-", dir=destination) as name:
        work = Path(name)
        input_link = work / f"source{source.suffix}"
        input_link.symlink_to(source)
        default_output = work / "default.jpg"
        config = work / "config"
        cache = work / "cache"
        config.mkdir()
        cache.mkdir()
        command = [
            str(cli), str(input_link), str(default_output),
            "--hq", "true", "--apply-custom-presets", "false",
            "--icc-type", "sRGB",
        ]
        if max_dimension:
            command.extend([
                "--width", str(max_dimension), "--height", str(max_dimension)])
        command.extend([
            "--core", "--configdir", str(config), "--cachedir", str(cache),
            "--library", str(work / "library.db"),
            "--conf", "plugins/imageio/format/jpeg/quality=95",
        ])
        try:
            result = runner(
                command, capture_output=True, text=True, check=False, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DarktableError(f"darktable render could not complete: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no diagnostic output").strip()
            raise DarktableError(
                f"darktable-cli failed with status {result.returncode}: {detail[-2000:]}")
        if not default_output.is_file() or default_output.stat().st_size == 0:
            raise DarktableError("darktable-cli reported success without an output image")
        default_packet = _xmp_packet(default_output)
        default_method: int | None
        applied_method: int | None
        if is_jpeg:
            # A rendered RGB photograph has no mosaic and therefore no
            # demosaic history node. Requiring one made otherwise valid JPEG
            # engine comparisons fail with a misleading RAW-specific error.
            default_method = None
            applied_method = None
            selected_output = default_output
        else:
            _, default_method = _demosaic_params(default_packet)
            if default_method == demosaic_method:
                selected_output = default_output
            else:
                selected_xmp = work / "selected-demosaic.xmp"
                selected_xmp.write_text(
                    _xmp_with_demosaic(default_packet, demosaic_method), encoding="utf-8")
                selected_output = work / "selected.jpg"
                selected_command = [
                    str(cli), str(input_link), str(selected_xmp), str(selected_output),
                    "--hq", "true", "--apply-custom-presets", "false",
                    "--icc-type", "sRGB",
                ]
                if max_dimension:
                    selected_command.extend([
                        "--width", str(max_dimension), "--height", str(max_dimension)])
                selected_command.extend([
                    "--core", "--configdir", str(config), "--cachedir", str(cache),
                    "--library", str(work / "library.db"),
                    "--conf", "plugins/imageio/format/jpeg/quality=95",
                ])
                try:
                    selected_result = runner(
                        selected_command, capture_output=True, text=True,
                        check=False, timeout=900)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise DarktableError(
                        f"selected darktable demosaic render could not complete: {exc}") from exc
                if selected_result.returncode != 0:
                    detail = (selected_result.stderr or selected_result.stdout
                              or "no diagnostic output").strip()
                    raise DarktableError(
                        f"darktable demosaic render failed with status "
                        f"{selected_result.returncode}: {detail[-2000:]}")
        if not selected_output.is_file() or selected_output.stat().st_size == 0:
            raise DarktableError("selected darktable demosaic render produced no image")
        if not is_jpeg:
            selected_packet = _xmp_packet(selected_output)
            _, applied_method = _demosaic_params(selected_packet)
            if applied_method != demosaic_method:
                raise DarktableError(
                    f"darktable stored demosaic method {applied_method}, "
                    f"expected {demosaic_method}")
        os.replace(selected_output, output)

    record: dict[str, Any] = {
        "format": DARKTABLE_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"path": str(source), "sha256": _sha256(source)},
        "engine": {
            "name": "darktable", "version": version,
            "executable": str(cli), "mode": "isolated-default-export",
            "demosaic": {
                "applicable": not is_jpeg,
                # Preserve the configured global preference for provenance,
                # while ``applicable`` states whether it affected this file.
                "requested": demosaic_mode,
                "label": demosaic_label if not is_jpeg else "Not applicable (JPEG source)",
                "method_id": demosaic_method if not is_jpeg else None,
                "applied_method_id": applied_method,
                "verified_from_embedded_xmp": not is_jpeg,
            },
        },
        "recipe_execution": {
            "mode": "native-control", "translated_operations": [],
            "notice": "No OpenCull creative operations are claimed for this control render.",
        },
        "isolation": {
            "disposable_config": True, "disposable_library": True,
            "source_sidecar_read": False, "source_sidecar_write": False,
            "custom_presets": False,
        },
        "output": {"path": str(output), "sha256": _sha256(output),
                   "size": output.stat().st_size,
                   "max_dimension": max_dimension},
    }
    provenance.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    record["provenance_path"] = str(provenance)
    return record
