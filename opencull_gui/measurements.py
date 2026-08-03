"""Optional scanner-manifest enrichment for the review GUI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .report import ReportIndex


class ManifestError(ValueError):
    """The scanner manifest is invalid or belongs to another library."""


MEASUREMENT_FIELDS = (
    "captured",
    "width",
    "height",
    "technical_score",
    "sharpness",
    "exposure",
    "contrast",
    "clipping",
    "composition_proxy",
    "sha256_prefix",
)


def load_measurements(
    path: Path | None, report: ReportIndex
) -> tuple[dict[str, dict[str, Any]], str | None]:
    if path is None:
        return {}, None
    resolved = path.expanduser().resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {resolved}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid manifest JSON {resolved}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != "opencull-manifest-v1":
        raise ManifestError("unsupported scanner manifest format")
    groups = data.get("groups")
    if not isinstance(groups, list):
        raise ManifestError("scanner manifest has no groups")
    measurements: dict[str, dict[str, Any]] = {}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(
                group.get("candidates"), list):
            raise ManifestError("scanner manifest contains an invalid group")
        for candidate in group["candidates"]:
            if not isinstance(candidate, dict):
                raise ManifestError("scanner manifest contains an invalid photo")
            name = candidate.get("name")
            if not isinstance(name, str) or name in measurements:
                raise ManifestError("scanner manifest has invalid photo identities")
            measurements[name] = {
                field: candidate.get(field)
                for field in MEASUREMENT_FIELDS
                if field in candidate
            }
    report_names = set(report.photo_names)
    manifest_names = set(measurements)
    if report_names != manifest_names:
        missing = len(report_names - manifest_names)
        extra = len(manifest_names - report_names)
        raise ManifestError(
            f"manifest/report photo identities differ ({missing} missing, "
            f"{extra} extra)")
    return measurements, str(resolved)
