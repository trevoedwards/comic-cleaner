"""What an empty library shows: a drop target and the first steps."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.extern import SEVENZIP_URL, can_extract, install_hint
from ..resources import app_icon
from .theme import colour

ICON_SIZE = 96


class WelcomePanel(QWidget):
    """Shown in place of the review columns until something is imported.

    Drops land on the main window, which forwards them; this panel only lights up
    while something is being dragged over it.
    """

    add_folder = Signal()
    add_files = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)

        self.frame = QFrame()
        self.frame.setObjectName("dropTarget")
        body = QVBoxLayout(self.frame)
        body.setContentsMargins(40, 40, 40, 40)
        body.setSpacing(14)
        body.addStretch(1)

        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(ICON_SIZE, ICON_SIZE))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(icon)

        self.title = QLabel("Drop comic archives or folders here")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.6)
        font.setBold(True)
        self.title.setFont(font)
        body.addWidget(self.title)

        self.blurb = QLabel(
            "Comic Cleaner finds the pages that repeat across your books, such as "
            "adverts, scanlation credits and \"read more at\" pages, and removes "
            "them from every book at once."
        )
        self.blurb.setWordWrap(True)
        self.blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The layout here never asks a wrapped label how tall it is at its width,
        # so it gets one line and is cut off; refresh() sets its height outright.
        self.blurb.setFixedWidth(520)
        body.addWidget(self.blurb, 0, Qt.AlignmentFlag.AlignHCenter)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_folder = QPushButton("Add Folder...")
        self.btn_folder.setDefault(True)
        self.btn_folder.clicked.connect(self.add_folder)
        self.btn_files = QPushButton("Add Files...")
        self.btn_files.clicked.connect(self.add_files)
        buttons.addWidget(self.btn_folder)
        buttons.addWidget(self.btn_files)
        buttons.addStretch(1)
        body.addLayout(buttons)

        self.steps = QLabel(
            "1. Import  →  2. Scan  →  3. Review what repeats  →  4. Apply, with a dry run"
            " and backups"
        )
        self.steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self.steps)

        self.tools = QLabel()
        self.tools.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tools.setWordWrap(True)
        self.tools.setOpenExternalLinks(True)
        self.tools.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        body.addWidget(self.tools)

        body.addStretch(2)
        outer.addWidget(self.frame)
        self.set_drag_active(False)

    def refresh(self) -> None:
        """Re-read the archive tools and re-apply theme colours."""
        muted = colour("muted").name()
        self.blurb.setStyleSheet(f"color: {muted};")
        self.blurb.setMinimumHeight(self.blurb.heightForWidth(self.blurb.width()))
        self.steps.setStyleSheet(f"color: {muted};")
        if can_extract("rar") and can_extract("7z"):
            self.tools.setText(".cbz, .cbr and .cb7 are all supported on this computer.")
            self.tools.setStyleSheet(f"color: {muted};")
        else:
            self.tools.setText(
                ".cbz works out of the box, but .cbr and .cb7 need an archive tool that "
                f"is not installed. {install_hint()} to read them. "
                f'<a href="{SEVENZIP_URL}">Get 7-Zip</a>'
            )
            self.tools.setStyleSheet(f"color: {colour('warning').name()};")
        self.set_drag_active(self._drag_active)

    def set_drag_active(self, active: bool) -> None:
        self._drag_active = active
        edge = colour("keep").name() if active else colour("muted").name()
        style = "solid" if active else "dashed"
        self.frame.setStyleSheet(
            f"#dropTarget {{ border: 2px {style} {edge}; border-radius: 12px; }}"
        )
