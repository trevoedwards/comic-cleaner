"""Past removal runs, and putting books back as they were."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.archive import ArchiveError, ComicArchive
from ..core.cache import HashCache
from ..core.hashing import make_thumbnail
from ..core.history import (
    Run,
    RunItem,
    delete_run_backups,
    export_csv,
    load_history,
    restore,
    when,
)
from ..units import human_bytes
from .theme import colour

log = logging.getLogger(__name__)

ROLE_ITEM = Qt.ItemDataRole.UserRole + 1
ROLE_RUN = Qt.ItemDataRole.UserRole + 2

# The first page of a backup, shown before a restore so it is clear what returns.
PREVIEW_SIZE = 160


def backup_thumbnail(item: RunItem, size: int = PREVIEW_SIZE) -> QPixmap | None:
    """The first page of a run item's backup, or None if there is none to show."""
    if item.backup is None or not item.backup.is_file():
        return None
    try:
        with ComicArchive(item.backup) as arc:
            names = arc.page_names()
            if not names:
                return None
            png = make_thumbnail(arc.read(names[0]), size)
    except (ArchiveError, OSError) as exc:
        log.debug("no preview of %s: %s", item.backup, exc)
        return None
    except Exception as exc:  # Pillow raises a wide variety of types
        log.debug("no preview of %s: %s", item.backup, exc)
        return None
    pixmap = QPixmap()
    return pixmap if pixmap.loadFromData(png) else None


class HistoryDialog(QDialog):
    """Every run, newest first; each book in it can be restored from its backup.

    `restored` lists what was put back, so the caller can rescan those books.
    """

    def __init__(self, cache: HashCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Removal history")
        self.resize(860, 560)
        self._cache = cache
        self.restored: list[RunItem] = []
        self._runs: list[Run] = []

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Every removal run is listed here. A book can be put back exactly as it was "
            "while its backup still exists; the cleaned version is then discarded."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Run / book", "Pages", "Freed", "Status"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 420)
        self.tree.setColumnWidth(1, 60)
        self.tree.setColumnWidth(2, 90)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.tree, 1)

        self.empty_note = QLabel("No removals yet.")
        self.empty_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_note)

        buttons = QHBoxLayout()
        self.btn_restore = QPushButton("Restore selected...")
        self.btn_restore.setToolTip(
            "Put the selected books (or every book in the selected runs) back from their "
            "backups."
        )
        self.btn_restore.clicked.connect(lambda: self.restore_selected())
        self.btn_pin = QPushButton("Pin")
        self.btn_pin.setToolTip(
            "A pinned run keeps its backups: Clean Up Backups leaves them alone, and "
            "they cannot be deleted from here until it is unpinned."
        )
        self.btn_pin.clicked.connect(lambda: self.toggle_pin())
        self.btn_delete_backups = QPushButton("Delete Backups for This Run...")
        self.btn_delete_backups.setToolTip(
            "Delete only the backups this run made. Its books can no longer be restored."
        )
        self.btn_delete_backups.clicked.connect(lambda: self.delete_backups())
        self.btn_export = QPushButton("Export CSV...")
        self.btn_export.clicked.connect(lambda: self.export(None))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.btn_restore)
        buttons.addWidget(self.btn_pin)
        buttons.addWidget(self.btn_delete_backups)
        buttons.addWidget(self.btn_export)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._populate()

    def _populate(self) -> None:
        self._runs = load_history(self._cache)
        self.tree.clear()
        muted = colour("muted")
        for run in self._runs:
            books = len(run.items)
            pinned = " — pinned" if run.pinned else ""
            top = QTreeWidgetItem([
                f"{when(run.started_at)} — {books} book(s) ({run.source.upper()}){pinned}",
                str(run.removed), human_bytes(run.bytes_freed), "",
            ])
            top.setData(0, ROLE_RUN, run.id)
            restorable = sum(1 for i in run.items if i.restorable)
            top.setText(3, f"{restorable} of {books} can be restored")
            for item in run.items:
                ok, reason = item.status()
                child = QTreeWidgetItem([
                    item.archive.name, str(item.removed), human_bytes(item.bytes_freed), reason,
                ])
                child.setData(0, ROLE_ITEM, item.id)
                child.setToolTip(0, "\n".join(
                    [str(item.archive)]
                    + ([f"Backup: {item.backup}"] if item.backup else [])
                    + ([f"Removed: {', '.join(item.pages)}"] if item.pages else [])
                ))
                if not ok:
                    child.setForeground(3, muted)
                top.addChild(child)
            self.tree.addTopLevelItem(top)
        self.tree.expandToDepth(0)
        self.empty_note.setVisible(not self._runs)
        self.btn_export.setEnabled(bool(self._runs))
        self._update_buttons()

    def _items_by_id(self) -> dict[int, RunItem]:
        return {item.id: item for run in self._runs for item in run.items}

    def selected_items(self) -> list[RunItem]:
        """The chosen books: a selected run stands for every book in it."""
        by_id = self._items_by_id()
        chosen: dict[int, RunItem] = {}
        for node in self.tree.selectedItems():
            nodes = [node.child(i) for i in range(node.childCount())] or [node]
            for leaf in nodes:
                item = by_id.get(leaf.data(0, ROLE_ITEM))
                if item is not None:
                    chosen[item.id] = item
        return list(chosen.values())

    def selected_runs(self) -> list[Run]:
        """The runs selected, or holding a selected book."""
        wanted: set[int] = set()
        for node in self.tree.selectedItems():
            top = node.parent() or node
            run_id = top.data(0, ROLE_RUN)
            if run_id is not None:
                wanted.add(int(run_id))
        return [run for run in self._runs if run.id in wanted]

    def _update_buttons(self) -> None:
        self.btn_restore.setEnabled(any(i.restorable for i in self.selected_items()))
        runs = self.selected_runs()
        self.btn_pin.setEnabled(bool(runs))
        self.btn_pin.setText("Unpin" if runs and all(r.pinned for r in runs) else "Pin")
        self.btn_delete_backups.setEnabled(len(runs) == 1)

    def toggle_pin(self) -> None:
        """Pin the selected runs, or unpin them if every one is pinned already."""
        runs = self.selected_runs()
        if not runs:
            return
        pin = not all(r.pinned for r in runs)
        chosen = {r.id for r in runs}
        for run in runs:
            self._cache.set_run_pinned(run.id, pin)
        self._populate()
        self._reselect(chosen)

    def _reselect(self, run_ids: set[int]) -> None:
        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            if top is not None and top.data(0, ROLE_RUN) in run_ids:
                top.setSelected(True)

    def delete_backups(self, *, confirm: bool = True) -> None:
        """Delete the backups of the one selected run, after asking."""
        runs = self.selected_runs()
        if len(runs) != 1:
            return
        run = runs[0]
        if run.pinned:
            QMessageBox.information(
                self, "Run is pinned",
                f"The run of {when(run.started_at)} is pinned, so its backups are kept. "
                "Unpin it first to delete them.",
            )
            return
        backups = run.backups
        if not backups:
            QMessageBox.information(
                self, "No backups", "This run has no backups left on disk."
            )
            return
        size = sum(b.stat().st_size for b in backups if b.exists())
        if confirm:
            answer = QMessageBox.question(
                self,
                "Delete backups",
                f"Delete the {len(backups)} backup(s) the run of {when(run.started_at)} made, "
                f"reclaiming {human_bytes(size)}? Its books can no longer be restored. "
                "This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        outcome = delete_run_backups(run)
        self._populate()
        message = f"Deleted {outcome.deleted} backup(s), reclaiming {human_bytes(outcome.freed)}."
        if outcome.errors:
            QMessageBox.warning(
                self, "Some backups were not deleted",
                message + "\n\n" + "\n".join(outcome.errors[:10]),
            )
        else:
            QMessageBox.information(self, "Backups deleted", message)

    def _confirm_restore(self, items: list[RunItem]) -> bool:
        """Ask before restoring, showing the first page that is coming back."""
        box = QMessageBox(self)
        box.setWindowTitle("Restore books")
        box.setText(
            f"Put back the original of {len(items)} book(s)? The cleaned versions are "
            "discarded, and the removed pages return."
        )
        thumbnail = next(
            (pix for pix in (backup_thumbnail(i) for i in items[:3]) if pix is not None), None
        )
        if thumbnail is not None:
            box.setIconPixmap(thumbnail)
            box.setInformativeText(f"First page of {items[0].archive.name} as it will be.")
        else:
            box.setIcon(QMessageBox.Icon.Question)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def restore_selected(self, *, confirm: bool = True) -> None:
        items = [i for i in self.selected_items() if i.restorable]
        if not items:
            return
        if confirm and not self._confirm_restore(items):
            return
        failures = []
        for item in items:
            error = restore(self._cache, item)
            if error is None:
                self.restored.append(item)
            else:
                failures.append(f"  {item.archive.name}: {error}")
        self._populate()
        if failures:
            QMessageBox.warning(
                self,
                "Some books were not restored",
                f"Restored {len(items) - len(failures)} of {len(items)}:\n\n"
                + "\n".join(failures[:10]),
            )

    def export(self, path: Path | None) -> None:
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self, "Export history", "comic-cleaner-history.csv", "CSV files (*.csv)"
            )
            if not chosen:
                return
            path = Path(chosen)
        try:
            rows = export_csv(self._runs, path)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write {path}:\n{exc}")
            return
        QMessageBox.information(self, "Exported", f"Wrote {rows} row(s) to {path.name}.")
