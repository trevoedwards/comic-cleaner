"""Reviewing and restoring pages hidden with "Ignore"."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.cache import HashCache, IgnoredEntry
from ..core.model import PageEntry
from .thumbs import ThumbnailCache

ROLE_GID = Qt.ItemDataRole.UserRole + 1
ROLE_THUMB_KEY = Qt.ItemDataRole.UserRole + 2

ICON_SIZE = 120


def _sample_page(entry: IgnoredEntry) -> PageEntry | None:
    """Enough of a PageEntry to fetch a thumbnail, if the sample still exists."""
    if entry.sample_path is None or not entry.sample_name or not entry.sample_path.is_file():
        return None
    return PageEntry(
        archive=entry.sample_path, name=entry.sample_name, index=0, size=0,
        width=0, height=0, content_sha="", dhash=0,
    )


class IgnoredDialog(QDialog):
    """Lists every ignore and lets any of them be undone.

    `changed` tells the caller whether groups need rebuilding afterwards.
    """

    def __init__(
        self, cache: HashCache, thumbs: ThumbnailCache, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ignored pages")
        self.resize(720, 520)
        self._cache = cache
        self._thumbs = thumbs
        self.changed = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "These pages are never shown as duplicates. Anything you restore is "
            "grouped again as soon as this window closes."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.listing = QListWidget()
        self.listing.setViewMode(QListWidget.ViewMode.IconMode)
        self.listing.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.listing.setGridSize(QSize(ICON_SIZE + 60, ICON_SIZE + 60))
        self.listing.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.listing.setMovement(QListWidget.Movement.Static)
        self.listing.setWordWrap(True)
        self.listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.listing.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.listing, 1)

        self.empty_note = QLabel("Nothing is ignored.")
        self.empty_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_note)

        buttons = QHBoxLayout()
        self.btn_restore = QPushButton("Restore selected")
        self.btn_restore.clicked.connect(self.restore_selected)
        self.btn_restore_all = QPushButton("Restore all")
        self.btn_restore_all.clicked.connect(self.restore_all)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.btn_restore)
        buttons.addWidget(self.btn_restore_all)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        thumbs.ready.connect(self._on_thumb_ready)
        self.finished.connect(lambda _: thumbs.ready.disconnect(self._on_thumb_ready))
        self._populate()

    def _populate(self) -> None:
        self.listing.clear()
        blank = QPixmap(ICON_SIZE, ICON_SIZE)
        blank.fill(Qt.GlobalColor.transparent)
        for entry in self._cache.ignored_entries():
            when = dt.datetime.fromtimestamp(entry.created_at).strftime("%Y-%m-%d")
            item = QListWidgetItem(f"{entry.note or 'Ignored group'}\nignored {when}")
            item.setData(ROLE_GID, entry.gid)
            page = _sample_page(entry)
            if page is not None:
                item.setData(ROLE_THUMB_KEY, self._thumbs.key_for(page))
                item.setIcon(self._thumbs.icon(page))
                item.setToolTip(f"{entry.sample_path}\n{entry.sample_name}")
            else:
                item.setIcon(QIcon(blank))
                item.setToolTip("The book this was ignored from is no longer available.")
            self.listing.addItem(item)
        self.empty_note.setVisible(self.listing.count() == 0)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.btn_restore.setEnabled(bool(self.listing.selectedItems()))
        self.btn_restore_all.setEnabled(self.listing.count() > 0)

    def _on_thumb_ready(self, key: str, pixmap: QPixmap) -> None:
        for row in range(self.listing.count()):
            item = self.listing.item(row)
            if item.data(ROLE_THUMB_KEY) == key:
                item.setIcon(QIcon(pixmap))

    def restore_selected(self) -> None:
        for item in self.listing.selectedItems():
            self._cache.unignore(item.data(ROLE_GID))
            self.changed = True
        self._populate()

    def restore_all(self) -> None:
        self._cache.clear_ignored()
        self.changed = True
        self._populate()
