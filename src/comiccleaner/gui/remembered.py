"""Everything the app remembers about pages: known junk, and what was ignored."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.cache import HashCache, IgnoredEntry, KnownEntry
from ..core.model import PageEntry
from ..core.signatures import SignatureFileError, export_known, import_known
from .thumbs import ThumbnailCache

ROLE_ID = Qt.ItemDataRole.UserRole + 1
ROLE_THUMB_KEY = Qt.ItemDataRole.UserRole + 2
# Lower-case note, tags and id, for the search box.
ROLE_SEARCH = Qt.ItemDataRole.UserRole + 3

ICON_SIZE = 120
FILE_FILTER = "Known junk lists (*.json)"


def _date(stamp: float) -> str:
    return dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d") if stamp else "unknown date"


def _blank() -> QIcon:
    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    return QIcon(pixmap)


def _sample_page(entry: IgnoredEntry) -> PageEntry | None:
    """Enough of a PageEntry to fetch a thumbnail, if the sample still exists."""
    if entry.sample_path is None or not entry.sample_name or not entry.sample_path.is_file():
        return None
    return PageEntry(
        archive=entry.sample_path, name=entry.sample_name, index=0, size=0,
        width=0, height=0, content_sha="", dhash=0,
    )


def _grid() -> QListWidget:
    listing = QListWidget()
    listing.setViewMode(QListWidget.ViewMode.IconMode)
    listing.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
    listing.setGridSize(QSize(ICON_SIZE + 60, ICON_SIZE + 60))
    listing.setResizeMode(QListWidget.ResizeMode.Adjust)
    listing.setMovement(QListWidget.Movement.Static)
    listing.setWordWrap(True)
    listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    return listing


class _Tab(QWidget):
    """An intro line, a thumbnail grid, a note for when it is empty, and buttons."""

    def __init__(self, intro: str, empty: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        note = QLabel(intro)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.listing = _grid()
        layout.addWidget(self.listing, 1)
        self.empty_note = QLabel(empty)
        self.empty_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_note)
        self.buttons = QHBoxLayout()
        layout.addLayout(self.buttons)

    def button(self, text: str, handler, *, stretch_before: bool = False) -> QPushButton:
        if stretch_before:
            self.buttons.addStretch(1)
        button = QPushButton(text)
        # Through a lambda: clicked passes a "checked" flag the handlers do not take.
        button.clicked.connect(lambda: handler())
        self.buttons.addWidget(button)
        return button

    def selected_ids(self) -> list[str]:
        return [item.data(ROLE_ID) for item in self.listing.selectedItems()]

    def finish(self) -> None:
        self.empty_note.setVisible(self.listing.count() == 0)


class RememberedDialog(QDialog):
    """Known junk (removed on sight) and ignored pages (never shown), side by side.

    `changed` tells the caller whether groups need rebuilding afterwards.
    """

    def __init__(
        self,
        cache: HashCache,
        thumbs: ThumbnailCache,
        parent: QWidget | None = None,
        *,
        tab: str = "known",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remembered pages")
        self.resize(760, 560)
        self._cache = cache
        self._thumbs = thumbs
        self.changed = False
        self._known: dict[str, KnownEntry] = {}

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self.known = _Tab(
            "Pages removed from your library before. Wherever one turns up again, even "
            "in a single new book, it is grouped and marked for removal. Export the "
            "list to share it, or import someone else's.",
            "Nothing remembered yet. Pages are added here when you remove them.",
        )
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search notes, tags and ids")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search known junk")
        self.search.textChanged.connect(self._apply_search)
        known_layout = self.known.layout()
        assert isinstance(known_layout, QVBoxLayout)
        known_layout.insertWidget(1, self.search)

        # Describe the one selected entry, so a long list stays searchable.
        editor = QWidget()
        form = QFormLayout(editor)
        form.setContentsMargins(0, 0, 0, 0)
        self.note_edit = QLineEdit()
        self.note_edit.setMaxLength(200)
        self.tags_edit = QLineEdit()
        self.tags_edit.setMaxLength(200)
        self.tags_edit.setPlaceholderText("e.g. credits, scanlator name")
        self.btn_save_note = QPushButton("Save")
        self.btn_save_note.clicked.connect(lambda: self.save_description())
        tags_row = QHBoxLayout()
        tags_row.addWidget(self.tags_edit, 1)
        tags_row.addWidget(self.btn_save_note)
        form.addRow("Note:", self.note_edit)
        form.addRow("Tags:", tags_row)
        known_layout.insertWidget(known_layout.count() - 1, editor)

        self.btn_forget = self.known.button("Forget selected", self.forget_selected)
        self.btn_forget_all = self.known.button("Forget all", self.forget_all)
        self.btn_import = self.known.button("Import...", self.import_list, stretch_before=True)
        self.btn_export = self.known.button("Export...", self.export_list)
        self.known.listing.itemSelectionChanged.connect(self._update_buttons)
        self.known.listing.itemSelectionChanged.connect(self._load_description)
        self.tabs.addTab(self.known, "Known junk")

        self.ignored = _Tab(
            "These pages are never shown as duplicates. Anything you restore is "
            "grouped again as soon as this window closes.",
            "Nothing is ignored.",
        )
        self.btn_restore = self.ignored.button("Restore selected", self.restore_selected)
        self.btn_restore_all = self.ignored.button("Restore all", self.restore_all)
        self.ignored.listing.itemSelectionChanged.connect(self._update_buttons)
        self.tabs.addTab(self.ignored, "Ignored")

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        close_row.addWidget(close)
        layout.addLayout(close_row)

        thumbs.ready.connect(self._on_thumb_ready)
        self.finished.connect(lambda _: thumbs.ready.disconnect(self._on_thumb_ready))
        self._populate()
        self.tabs.setCurrentWidget(self.ignored if tab == "ignored" else self.known)

    # -- filling -----------------------------------------------------------
    def _populate(self) -> None:
        self._populate_known()
        self._populate_ignored()
        self._update_buttons()
        self._load_description()

    def _populate_known(self) -> None:
        listing = self.known.listing
        listing.clear()
        self._known = {entry.sid: entry for entry in self._cache.known_entries()}
        for entry in self._known.values():
            origin = (
                "imported" if entry.source == "imported"
                else f"removed {_date(entry.created_at)}"
            )
            tags = f"\n{entry.tags}" if entry.tags else ""
            item = QListWidgetItem(f"{entry.note or 'Known junk'}\n{origin}{tags}")
            item.setData(ROLE_ID, entry.sid)
            item.setData(ROLE_SEARCH, f"{entry.note} {entry.tags} {entry.sid}".lower())
            pixmap = QPixmap()
            if entry.thumbnail and pixmap.loadFromData(entry.thumbnail):
                item.setIcon(QIcon(pixmap))
            else:
                item.setIcon(_blank())
            item.setToolTip(f"{len(entry.hashes)} hash(es), id {entry.sid}")
            listing.addItem(item)
        self.known.finish()
        self._apply_search()

    def _populate_ignored(self) -> None:
        listing = self.ignored.listing
        listing.clear()
        for entry in self._cache.ignored_entries():
            item = QListWidgetItem(
                f"{entry.note or 'Ignored group'}\nignored {_date(entry.created_at)}"
            )
            item.setData(ROLE_ID, entry.gid)
            page = _sample_page(entry)
            if page is not None:
                item.setData(ROLE_THUMB_KEY, self._thumbs.key_for(page))
                item.setIcon(self._thumbs.icon(page))
                item.setToolTip(f"{entry.sample_path}\n{entry.sample_name}")
            else:
                item.setIcon(_blank())
                item.setToolTip("The book this was ignored from is no longer available.")
            listing.addItem(item)
        self.ignored.finish()

    def _apply_search(self) -> None:
        needle = self.search.text().strip().lower()
        listing = self.known.listing
        for row in range(listing.count()):
            item = listing.item(row)
            hidden = bool(needle) and needle not in str(item.data(ROLE_SEARCH) or "")
            item.setHidden(hidden)
            if hidden and item.isSelected():
                item.setSelected(False)

    def _selected_known(self) -> KnownEntry | None:
        chosen = self.known.selected_ids()
        return self._known.get(chosen[0]) if len(chosen) == 1 else None

    def _load_description(self) -> None:
        entry = self._selected_known()
        for edit in (self.note_edit, self.tags_edit):
            edit.setEnabled(entry is not None)
        self.btn_save_note.setEnabled(entry is not None)
        self.note_edit.setText(entry.note if entry else "")
        self.tags_edit.setText(entry.tags if entry else "")

    def save_description(self) -> None:
        """Store the note and tags typed for the selected entry."""
        entry = self._selected_known()
        if entry is None:
            return
        self._cache.describe_known(
            entry.sid, note=self.note_edit.text().strip(), tags=self.tags_edit.text().strip()
        )
        self._populate_known()
        for row in range(self.known.listing.count()):
            item = self.known.listing.item(row)
            if item.data(ROLE_ID) == entry.sid and not item.isHidden():
                item.setSelected(True)

    def _update_buttons(self) -> None:
        has_known = self.known.listing.count() > 0
        self.btn_forget.setEnabled(bool(self.known.listing.selectedItems()))
        self.btn_forget_all.setEnabled(has_known)
        self.btn_export.setEnabled(has_known)
        self.btn_restore.setEnabled(bool(self.ignored.listing.selectedItems()))
        self.btn_restore_all.setEnabled(self.ignored.listing.count() > 0)

    def _on_thumb_ready(self, key: str, pixmap: QPixmap) -> None:
        listing = self.ignored.listing
        for row in range(listing.count()):
            item = listing.item(row)
            if item.data(ROLE_THUMB_KEY) == key:
                item.setIcon(QIcon(pixmap))

    # -- known junk --------------------------------------------------------
    def forget_selected(self) -> None:
        for sid in self.known.selected_ids():
            self._cache.forget(sid)
            self.changed = True
        self._populate()

    def forget_all(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Forget all known junk",
            "Forget every remembered page? New books will need reviewing from scratch.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._cache.clear_known()
        self.changed = True
        self._populate()

    def export_list(self, path: Path | None = None) -> None:
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self, "Export known junk", "known-junk.json", FILE_FILTER
            )
            if not chosen:
                return
            path = Path(chosen)
        try:
            count = export_known(self._cache.known_entries(), path)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write {path}:\n{exc}")
            return
        QMessageBox.information(
            self, "Exported", f"Wrote {count} remembered page(s) to {path.name}."
        )

    def import_list(self, path: Path | None = None) -> None:
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(self, "Import known junk", "", FILE_FILTER)
            if not chosen:
                return
            path = Path(chosen)
        try:
            result = import_known(self._cache, path)
        except SignatureFileError as exc:
            QMessageBox.warning(self, "Import failed", f"{exc}\n\nNothing was imported.")
            return
        self.changed = self.changed or bool(result.added or result.merged)
        self._populate()
        QMessageBox.information(
            self,
            "Imported",
            f"Added {result.added} new page(s); {result.merged} were already known. "
            "Matching pages will be marked for removal when you close this window.",
        )

    # -- ignored -----------------------------------------------------------
    def restore_selected(self) -> None:
        for gid in self.ignored.selected_ids():
            self._cache.unignore(gid)
            self.changed = True
        self._populate()

    def restore_all(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Restore all ignored pages",
            "Restore every ignored page? They will show up in the groups again.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._cache.clear_ignored()
        self.changed = True
        self._populate()
