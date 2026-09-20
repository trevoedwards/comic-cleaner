"""Locating bundled assets, both from source and inside a PyInstaller build."""

from __future__ import annotations

import sys
from pathlib import Path

ASSET_DIR_NAME = "assets"
ICON_NAME = "icon.png"


def asset_root() -> Path:
    """Directory holding bundled assets.

    PyInstaller unpacks --add-data files under sys._MEIPASS, so the usual
    __file__-relative path does not exist in a frozen build.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "comiccleaner" / ASSET_DIR_NAME
    return Path(__file__).resolve().parent / ASSET_DIR_NAME


def icon_path() -> Path | None:
    """The app icon, or None if it did not make it into the build."""
    candidate = asset_root() / ICON_NAME
    return candidate if candidate.is_file() else None


def app_icon():
    """A QIcon for the window and taskbar, or an empty one if unavailable."""
    from PySide6.QtGui import QIcon

    found = icon_path()
    return QIcon(str(found)) if found else QIcon()
