"""macOS application paths, bundled resources, and single-instance ownership."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Darkimiya"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
APP_CACHE = Path.home() / "Library" / "Caches" / APP_NAME
APP_LOGS = Path.home() / "Library" / "Logs" / APP_NAME
LEGACY_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "OpenCull"


def resource_root() -> Path:
    """Return source root or PyInstaller's immutable bundled resource root."""
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen).resolve() if frozen else Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class MacOSPaths:
    support: Path = APP_SUPPORT
    cache: Path = APP_CACHE
    logs: Path = APP_LOGS

    @classmethod
    def create(cls, base: Path | None = None) -> MacOSPaths:
        if base is None:
            value = cls()
        else:
            root = base.expanduser().resolve()
            value = cls(root / "Application Support", root / "Caches", root / "Logs")
        value.results.mkdir(parents=True, exist_ok=True)
        for directory in (value.support, value.cache, value.logs, value.results):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        if base is None:
            value._import_legacy_state()
        return value

    def _import_legacy_state(self) -> None:
        """Copy small durable indexes once; never move or rewrite old state."""
        for name in ("jobs.json", "providers.json", "onboarding-complete"):
            source = LEGACY_APP_SUPPORT / name
            destination = self.support / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
                os.chmod(destination, 0o600)

    @property
    def jobs(self) -> Path:
        return self.support / "jobs.json"

    @property
    def providers(self) -> Path:
        return self.support / "providers.json"

    @property
    def generated(self) -> Path:
        return self.support / "generated"

    @property
    def kimiya(self) -> Path:
        return self.support / "Kimiya"

    @property
    def results(self) -> Path:
        return self.support / "Results"

    @property
    def face_models(self) -> Path:
        bundled = resource_root() / ".opencull-models"
        return bundled if bundled.is_dir() else self.support / "models"

    @property
    def launcher_log(self) -> Path:
        return self.logs / "Darkimiya.log"

    @property
    def onboarding_marker(self) -> Path:
        return self.support / "onboarding-complete"


class InstanceLock:
    """Own a non-destructive advisory lock for the desktop queue controller."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._handle.close()
            self._handle = None
            return False
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        os.fchmod(self._handle.fileno(), 0o600)
        atexit.register(self.release)
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        import fcntl

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
