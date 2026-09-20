"""The About dialog."""

from __future__ import annotations

import platform
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, AUTHOR, GITHUB_URL, ORGANISATION, __version__
from ..core.extern import describe_backends
from ..resources import app_icon

ICON_SIZE = 96


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(520)

        layout = QHBoxLayout(self)
        layout.setSpacing(18)

        icon = QLabel()
        pixmap = app_icon().pixmap(ICON_SIZE, ICON_SIZE)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(icon)

        body = QVBoxLayout()
        body.setSpacing(10)
        layout.addLayout(body, 1)

        title = QLabel(f"<h2 style='margin:0'>{APP_NAME}</h2>")
        body.addWidget(title)
        body.addWidget(QLabel(f"Version {__version__}"))

        blurb = QLabel(
            "Finds pages that repeat across comic archives - injected adverts, "
            "scanlation credits and other filler - and removes them, so the "
            "books read the way they were drawn."
        )
        blurb.setWordWrap(True)
        body.addWidget(blurb)

        credit = QLabel(
            f"<b>Developed by {AUTHOR}</b><br>"
            f"{ORGANISATION}<br><br>"
            f'<a href="{GITHUB_URL}">{GITHUB_URL}</a>'
        )
        credit.setOpenExternalLinks(True)
        credit.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        body.addWidget(credit)

        body.addWidget(_separator())
        body.addWidget(QLabel(_environment_summary()))

        body.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        body.addWidget(buttons)


def _separator() -> QWidget:
    line = QLabel()
    line.setFixedHeight(1)
    line.setStyleSheet("background: palette(mid);")
    return line


def _environment_summary() -> str:
    """Small print that makes a bug report useful."""
    from PySide6 import __version__ as pyside_version

    tools = [name for name, path in describe_backends().items() if path]
    return (
        f"Python {platform.python_version()} · PySide6 {pyside_version}\n"
        f"{platform.system()} {platform.release()}"
        + (" · frozen build" if getattr(sys, "frozen", False) else "")
        + "\n"
        + ("Archive tools: " + ", ".join(tools) if tools else "No archive tools found")
    )
