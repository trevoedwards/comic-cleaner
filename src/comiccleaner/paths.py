"""Where per-user data lives, worked out without Qt.

The command line runs on headless servers where importing Qt's widget stack
can fail outright, yet it must share the GUI's hash cache and ignore list. So
this mirrors what QStandardPaths.GenericDataLocation returns, and a test checks
the two agree on every platform CI runs on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME, ORGANISATION


def generic_data_location() -> Path:
    """The per-user data root Qt calls GenericDataLocation."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg = os.environ.get("XDG_DATA_HOME")
    # XDG says a relative value is invalid and must be ignored.
    if xdg and Path(xdg).is_absolute():
        return Path(xdg)
    return Path.home() / ".local" / "share"


def data_dir() -> Path:
    return generic_data_location() / ORGANISATION / APP_NAME


def cache_path() -> Path:
    """The hash cache and ignore list, shared by the GUI and the command line."""
    return data_dir() / "hashes.sqlite"
