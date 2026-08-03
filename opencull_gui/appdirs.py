"""Per-platform locations for private application state.

Application support, cache and log directories were hardcoded to
``~/Library/...``, which is correct on macOS and wrong everywhere else: on
Linux it produced a stray ``~/Library`` tree, and on Windows a folder whose
name is not where anything looks.

macOS keeps exactly the paths that shipped, so existing installations are
untouched. Linux follows the XDG Base Directory specification and Windows
uses ``%LOCALAPPDATA%``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Darkimiya"
LEGACY_APP_NAME = "OpenCull"


def _xdg(variable: str, default: str) -> Path:
    # An XDG variable is only honoured when absolute, per the specification.
    value = os.environ.get(variable, "").strip()
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / default


def _windows_local_app_data() -> Path:
    value = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(value) if value else Path.home() / "AppData" / "Local"


def support_dir(app_name: str = APP_NAME) -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    if sys.platform == "win32":
        return _windows_local_app_data() / app_name
    return _xdg("XDG_DATA_HOME", ".local/share") / app_name


def cache_dir(app_name: str = APP_NAME) -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / app_name
    if sys.platform == "win32":
        return _windows_local_app_data() / app_name / "Cache"
    return _xdg("XDG_CACHE_HOME", ".cache") / app_name


def logs_dir(app_name: str = APP_NAME) -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / app_name
    if sys.platform == "win32":
        return _windows_local_app_data() / app_name / "Logs"
    # XDG has no log directory; state is its closest documented match and is
    # what most Linux applications use for logs.
    return _xdg("XDG_STATE_HOME", ".local/state") / app_name


def legacy_support_dir() -> Path:
    """Where a pre-rename installation kept its state, for one-time import."""
    return support_dir(LEGACY_APP_NAME)
