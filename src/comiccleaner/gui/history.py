"""Past removal runs, and putting books back as they were."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
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

from ..core.cache import HashCache
from ..core.history import Run, RunItem, export_csv, load_history, restore, when
from ..units import human_bytes
from .theme import colour

ROLE_ITEM = Qt.ItemDataRole.UserRole + 1


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
        self.btn_export = QPushButton("Export CSV...")
        self.btn_export.clicked.connect(lambda: self.export(None))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.btn_restore)
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
            top = QTreeWidgetItem([
                f"{when(run.started_at)} — {books} book(s) ({run.source.upper()})",
                str(run.removed), human_bytes(run.bytes_freed), "",
            ])
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

    def _update_buttons(self) -> None:
        self.btn_restore.setEnabled(any(i.restorable for i in self.selected_items()))

    def restore_selected(self, *, confirm: bool = True) -> None:
        items = [i for i in self.selected_items() if i.restorable]
        if not items:
            return
        if confirm:
            answer = QMessageBox.question(
                self,
                "Restore books",
                f"Put back the original of {len(items)} book(s)? The cleaned versions are "
                "discarded, and the removed pages return.",
            )
            if answer != QMessageBox.StandardButton.Yes:
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
