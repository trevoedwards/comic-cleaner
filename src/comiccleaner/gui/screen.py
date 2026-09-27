"""Opening sizes that fit the screen a window opens on."""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

# resize() sets the inside of a window; its title bar and frame come on top.
# Windows 11 at 100% adds 16 x 39.
FRAME_ALLOWANCE = (24, 48)


def fit_to_screen(widget: QWidget, width: int, height: int) -> None:
    """Resize `widget` to width x height, or less where the screen is smaller.

    Fixed sizes ran off a 1280x800 screen: the main window opened at 1400x860,
    and the preview's Close button sat under the taskbar. The window's own
    minimum still wins, so a screen too small for that is scrolled, not squashed.
    """
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is not None:
        room = screen.availableGeometry()
        width = min(width, room.width() - FRAME_ALLOWANCE[0])
        height = min(height, room.height() - FRAME_ALLOWANCE[1])
    widget.resize(width, height)
