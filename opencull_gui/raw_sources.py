"""Persistent, read-only matching of JPEG photographs to external RAW files."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from scan import RAW_EXTENSIONS

from .project import is_managed_project_path
from .report import ReportIndex

RAW_SOURCE_FORMAT = "opencull-raw-source-v1"


class RawSourceError(ValueError):
    """An external RAW source cannot be indexed safely."""


class RawSourceStore:
    def __init__(self, path: Path, report: ReportIndex):
        self.path = path.expanduser().resolve()
        self.report = report
        self._lock = threading.RLock()
        self.root: Path | None = None
        self.matches: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if (
            not isinstance(value, dict)
            or value.get("format") != RAW_SOURCE_FORMAT
            or value.get("report_sha256") != self.report.sha256
        ):
            return
        saved_root = value.get("root")
        if not isinstance(saved_root, str) or not saved_root.strip():
            return
        root = Path(saved_root).expanduser()
        if root.is_dir():
            self.configure(root)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "format": RAW_SOURCE_FORMAT,
            "report_sha256": self.report.sha256,
            "root": str(self.root) if self.root else "",
        }
        handle, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def configure(self, root: Path) -> dict[str, Any]:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise RawSourceError(f"RAW folder does not exist: {resolved}")
        by_stem: dict[str, list[str]] = {}
        try:
            paths = sorted(
                path for path in resolved.rglob("*")
                if path.is_file()
                and path.suffix.lower() in RAW_EXTENSIONS
                and not is_managed_project_path(resolved, path)
            )
        except OSError as exc:
            raise RawSourceError(f"RAW folder cannot be scanned: {exc}") from exc
        for path in paths:
            relative = path.relative_to(resolved).as_posix()
            by_stem.setdefault(path.stem.casefold(), []).append(relative)
        matches = {
            name: list(by_stem.get(Path(name).stem.casefold(), []))
            for name in self.report.photo_names
        }
        with self._lock:
            self.root = resolved
            self.matches = matches
            self._save()
            return self.public()

    def public(self) -> dict[str, Any]:
        with self._lock:
            matched = sum(bool(files) for files in self.matches.values())
            ambiguous = sum(len(files) > 1 for files in self.matches.values())
            return {
                "configured": self.root is not None,
                "root": str(self.root) if self.root else "",
                "matches": dict(self.matches),
                "summary": {
                    "photos": len(self.report.photo_names),
                    "matched": matched,
                    "missing": len(self.report.photo_names) - matched,
                    "ambiguous": ambiguous,
                    "raw_files": len({
                        file for files in self.matches.values() for file in files
                    }),
                },
            }
