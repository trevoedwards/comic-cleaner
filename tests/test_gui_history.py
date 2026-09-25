"""The History dialog: listing runs and restoring books from the GUI."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from comiccleaner.core.archive import is_page_name  # noqa: E402
from comiccleaner.core.model import Decision  # noqa: E402
from comiccleaner.gui.history import HistoryDialog  # noqa: E402
from comiccleaner.gui.main_window import MainWindow  # noqa: E402

from .test_gui import apply_and_wait, pump_until, scan_and_wait  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(QSettings, "value", lambda self, key, default=None: default)
    monkeypatch.setattr(QSettings, "setValue", lambda self, key, value: None)
    monkeypatch.setattr(QSettings, "sync", lambda self: None)
    monkeypatch.setattr(
        "comiccleaner.gui.main_window.cache_path", lambda: tmp_path / "data" / "c.sqlite"
    )
    win = MainWindow()
    yield win
    win.close()


def _pages(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(1 for n in zf.namelist() if is_page_name(n))


def test_a_gui_removal_can_be_undone_from_history(window, library, monkeypatch):
    window.settings.remember_junk = False  # so the restored advert is not re-marked
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    apply_and_wait(window, monkeypatch)
    assert _pages(library / "Book 01.cbz") == 4

    dialog = HistoryDialog(window.cache, window)
    try:
        assert dialog.tree.topLevelItemCount() == 1
        run = dialog.tree.topLevelItem(0)
        assert "3 book(s) (GUI)" in run.text(0)
        assert run.text(3) == "3 of 3 can be restored"
        run.setSelected(True)
        assert dialog.btn_restore.isEnabled()
        dialog.restore_selected(confirm=False)
        assert len(dialog.restored) == 3
        assert dialog.tree.topLevelItem(0).text(3) == "0 of 3 can be restored"
    finally:
        dialog.done(QDialog.DialogCode.Accepted)

    window._after_restore(dialog.restored)
    assert pump_until(lambda: window._scan_worker is None), "rescan did not finish"

    assert all(_pages(library / f"Book 0{n}.cbz") == 5 for n in (1, 2, 3))
    assert len(window.groups) == 1  # the advert is back, and found again


def test_restoring_a_book_taken_out_of_the_library_brings_it_back(
    window, library, monkeypatch
):
    window.settings.remember_junk = False
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    apply_and_wait(window, monkeypatch)
    gone = (library / "Book 01.cbz").resolve()
    window.archive_list.clearSelection()
    for row in range(window.archive_list.count()):
        item = window.archive_list.item(row)
        item.setSelected(Path(item.data(0x0100)) == gone)
    window.remove_selected_archives()
    assert gone not in window.archives

    dialog = HistoryDialog(window.cache, window)
    try:
        dialog.tree.topLevelItem(0).setSelected(True)
        dialog.restore_selected(confirm=False)
    finally:
        dialog.done(QDialog.DialogCode.Accepted)
    window._after_restore(dialog.restored)
    assert pump_until(lambda: window._scan_worker is None), "rescan did not finish"

    assert gone in window.archives
    assert window.archives[gone].page_count == 5  # scanned again, advert and all
    assert "added back to the library" in window.status_label.text()
    assert len(window.groups) == 1 and window.groups[0].archive_count == 3


def test_a_dry_run_does_not_appear_in_history(window, library, monkeypatch):
    window.import_paths([library])
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_and_wait(window, monkeypatch, dry_run=True)

    dialog = HistoryDialog(window.cache, window)
    try:
        assert dialog.tree.topLevelItemCount() == 0
        assert dialog.empty_note.isVisibleTo(dialog)
    finally:
        dialog.done(QDialog.DialogCode.Accepted)
