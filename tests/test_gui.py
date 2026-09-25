"""Drives the real window offscreen: import -> scan -> mark -> apply."""

from __future__ import annotations

import os
import threading
import time
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from comiccleaner.core.archive import is_page_name  # noqa: E402
from comiccleaner.core.model import Decision  # noqa: E402
from comiccleaner.gui.main_window import ROLE_GID, MainWindow, human_bytes  # noqa: E402

from .conftest import make_page, write_archive  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    """A window whose settings and hash cache are isolated per test."""
    monkeypatch.setattr(QSettings, "value", lambda self, key, default=None: default)
    monkeypatch.setattr(QSettings, "setValue", lambda self, key, value: None)
    monkeypatch.setattr(QSettings, "sync", lambda self: None)
    monkeypatch.setattr(
        "comiccleaner.gui.main_window.cache_path", lambda: tmp_path / "cache.sqlite"
    )

    win = MainWindow()
    # The advert sits on page 2 of every book, so covers are not a factor here;
    # min_archives=2 is the shipped default and suits this fixture.
    yield win
    win.close()


def pump_until(predicate, timeout: float = 30.0) -> bool:
    """Spin the Qt event loop until `predicate` holds or we give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def scan_and_wait(window: MainWindow) -> None:
    window.start_scan()
    assert pump_until(lambda: window._scan_worker is None), "scan did not finish"


def apply_and_wait(window: MainWindow, monkeypatch, *, dry_run: bool = False) -> None:
    """Accept the confirmation dialog automatically, then run the removal."""
    from PySide6.QtWidgets import QDialog, QMessageBox

    monkeypatch.setattr(
        "comiccleaner.gui.main_window._ConfirmDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "comiccleaner.gui.main_window._ConfirmDialog.dry_run", lambda self: dry_run
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)

    window.apply_removals()
    assert pump_until(lambda: window._removal_worker is None), "removal did not finish"
    # The rewritten books are rescanned straight afterwards.
    assert pump_until(lambda: window._scan_worker is None), "rescan did not finish"


# -- tests -----------------------------------------------------------------


def test_import_discovers_archives(window, library):
    window.import_paths([library])

    assert len(window.archives) == 3
    assert window.archive_list.count() == 3


def test_scan_populates_groups(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)

    assert all(info.pages for info in window.archives.values())
    assert len(window.groups) == 1
    assert window.group_list.count() == 1
    assert window.groups[0].archive_count == 3


def test_selecting_a_group_lists_every_copy(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)

    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()

    assert window.page_list.count() == 3
    # Every copy is ticked for removal by default.
    from PySide6.QtCore import Qt

    states = [window.page_list.item(i).checkState() for i in range(3)]
    assert all(s is Qt.CheckState.Checked for s in states)


def test_unticking_a_page_spares_it(window, library, monkeypatch):
    from PySide6.QtCore import Qt

    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()

    # Spare whichever copy belongs to Book 01.
    for row in range(window.page_list.count()):
        item = window.page_list.item(row)
        if "Book 01" in item.text():
            item.setCheckState(Qt.CheckState.Unchecked)
            break
    QCoreApplication.processEvents()

    window._set_all_decisions(Decision.DELETE)
    apply_and_wait(window, monkeypatch)

    assert _page_count(library / "Book 01.cbz") == 5
    assert _page_count(library / "Book 02.cbz") == 4


def test_full_workflow_removes_the_advert(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)

    window._set_all_decisions(Decision.DELETE)
    assert window.act_apply.isEnabled()

    apply_and_wait(window, monkeypatch)

    for name in ("Book 01.cbz", "Book 02.cbz", "Book 03.cbz"):
        assert _page_count(library / name) == 4
        assert (library / f"{name}.bak").exists()


def test_finished_removal_does_not_leave_stale_groups_to_reapply(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_and_wait(window, monkeypatch)

    # The rewritten books were rescanned on their own: what is on screen is what
    # is on disk now, and nothing is left to remove.
    assert all(info.page_count == 4 for info in window.archives.values())
    assert window.groups == []
    assert not window.act_apply.isEnabled()


def test_dry_run_leaves_files_alone(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_and_wait(window, monkeypatch, dry_run=True)

    assert _page_count(library / "Book 01.cbz") == 5
    assert not list(library.glob("*.bak"))


def test_dry_run_reports_honestly_and_keeps_the_library_intact(
    window, library, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    shown: list[str] = []
    real_set_text = QMessageBox.setText
    monkeypatch.setattr(
        QMessageBox, "setText", lambda self, text: (shown.append(text), real_set_text(self, text))
    )
    book = library / "Book 01.cbz"
    stat = book.stat()
    assert window.cache.get(book, stat.st_size, stat.st_mtime_ns) is not None

    apply_and_wait(window, monkeypatch, dry_run=True)

    assert shown and shown[-1].startswith("Dry run")
    assert "Nothing was changed" in shown[-1]
    # Nothing on disk moved, so the cached hashes and scanned pages are still good.
    assert window.cache.get(book, stat.st_size, stat.st_mtime_ns) is not None
    assert all(info.pages for info in window.archives.values())


def test_apply_stays_disabled_while_a_scan_or_removal_is_running(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    assert window.act_apply.isEnabled()

    window._scan_worker = object()  # stands in for a scan in flight
    try:
        window._refresh_status()
        assert not window.act_apply.isEnabled()
    finally:
        window._scan_worker = None

    window._removal_worker = object()
    try:
        window._refresh_status()
        assert not window.act_apply.isEnabled()
    finally:
        window._removal_worker = None
    window._refresh_status()
    assert window.act_apply.isEnabled()


def test_scan_cannot_start_while_archives_are_being_rewritten(window, library):
    window.import_paths([library])
    window._removal_worker = object()
    try:
        window.start_scan()
        assert window._scan_worker is None
    finally:
        window._removal_worker = None


def _library_with_a_duplicated_issue(root: Path) -> Path:
    """Two books sharing one advert, plus two copies of one issue.

    The copies match each other page for page. The command line skips books
    that would lose more than a quarter of their pages; the GUI must too.
    """
    ad = make_page(seed=9999)
    for number in (1, 2):
        story = [make_page(seed=number * 100 + i) for i in range(4)]
        write_archive(root / f"Book {number:02d}.cbz", [story[0], ad, *story[1:]])
    issue = [make_page(seed=500 + i) for i in range(4)]
    write_archive(root / "Issue 1.cbz", issue)
    write_archive(root / "Issue 1 (copy).cbz", [*issue, make_page(seed=599)])
    return root


def _confirming(monkeypatch, *, dry_run: bool) -> list:
    """Accept the confirmation, recording what it listed."""
    from PySide6.QtWidgets import QDialog, QMessageBox

    from comiccleaner.gui.main_window import _ConfirmDialog

    shown: list[_ConfirmDialog] = []

    def accept(self):
        shown.append(self)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(_ConfirmDialog, "exec", accept)
    monkeypatch.setattr(_ConfirmDialog, "dry_run", lambda self: dry_run)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)
    return shown


@pytest.mark.parametrize("dry_run", [False, True])
def test_books_that_would_lose_too_much_are_skipped_in_the_gui_too(
    window, tmp_path, monkeypatch, dry_run
):
    from PySide6.QtWidgets import QMessageBox

    library = _library_with_a_duplicated_issue(tmp_path / "lib")
    before = {p.name: p.read_bytes() for p in library.glob("Issue*.cbz")}
    window.import_paths([library])
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    shown = _confirming(monkeypatch, dry_run=dry_run)
    reports: list[str] = []
    real_set_text = QMessageBox.setText
    monkeypatch.setattr(
        QMessageBox, "setText", lambda self, text: (reports.append(text), real_set_text(self, text))
    )
    handed: list[list] = []
    from comiccleaner.gui import main_window

    real_worker = main_window.RemovalWorker
    monkeypatch.setattr(
        main_window, "RemovalWorker",
        lambda plans, **kw: (handed.append(list(plans)), real_worker(plans, **kw))[1],
    )

    window.apply_removals()
    assert pump_until(lambda: window._removal_worker is None), "removal did not finish"
    assert pump_until(lambda: window._scan_worker is None), "rescan did not finish"

    [dialog] = shown
    listed = [dialog.skipped_listing.item(i).text() for i in range(dialog.skipped_listing.count())]
    assert sorted(t.split("  ")[0] for t in listed) == ["Issue 1 (copy).cbz", "Issue 1.cbz"]
    assert [sorted(p.archive.name for p in plans) for plans in handed] == [
        ["Book 01.cbz", "Book 02.cbz"]
    ]
    assert {p.name: p.read_bytes() for p in library.glob("Issue*.cbz")} == before
    assert "2 book(s)" in reports[-1] and "25%" in reports[-1]
    expected = 5 if dry_run else 4
    assert _page_count(library / "Book 01.cbz") == expected


def test_nothing_is_applied_when_every_book_would_lose_too_much(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    library = _library_with_a_duplicated_issue(tmp_path / "lib")
    (library / "Book 01.cbz").unlink()
    (library / "Book 02.cbz").unlink()
    window.import_paths([library])
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    shown = _confirming(monkeypatch, dry_run=False)
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))

    window.apply_removals()

    assert shown == [] and window._removal_worker is None
    assert "Issue 1.cbz" in told[0] and "Issue 1 (copy).cbz" in told[0]


def test_scan_stays_off_for_the_whole_removal(window, tmp_path, monkeypatch):
    from comiccleaner.core import remover

    _three_ad_library(tmp_path)
    window.import_paths([tmp_path])
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    assert window.act_scan.isEnabled()

    entered, gate = threading.Event(), threading.Event()
    real_apply = remover.apply_plan

    def slow_apply(plan, **kwargs):
        entered.set()
        gate.wait(20)
        return real_apply(plan, **kwargs)

    monkeypatch.setattr(remover, "apply_plan", slow_apply)
    _confirming(monkeypatch, dry_run=False)
    finished_while_busy: list[bool] = []
    real_done = window._on_removal_done

    def done(report):
        real_done(report)
        # The removal thread has not exited yet, so a scan must still be refused.
        finished_while_busy.append(window.act_scan.isEnabled())

    monkeypatch.setattr(window, "_on_removal_done", done)
    window.apply_removals()
    try:
        assert pump_until(entered.is_set, 10), "removal never started"
        assert not window.act_scan.isEnabled()
    finally:
        gate.set()

    assert pump_until(lambda: window._removal_worker is None), "removal did not finish"
    assert finished_while_busy == [False]
    assert pump_until(lambda: window._scan_worker is None), "rescan did not finish"
    assert window.act_scan.isEnabled()
    assert window.act_scan.text() == "Scan"


def _three_ad_library(root: Path) -> None:
    ads = [make_page(seed=9000 + i) for i in range(3)]
    for number in range(3):
        # Twelve pages, so the three adverts are exactly the quarter a book may lose.
        story = [make_page(seed=number * 100 + i) for i in range(9)]
        write_archive(root / f"Book {number}.cbz", [story[0], *ads, *story[1:]])


def test_ignoring_a_group_keeps_your_place_in_the_list(window, tmp_path):
    _three_ad_library(tmp_path)
    window.import_paths([tmp_path])
    scan_and_wait(window)
    assert window.group_list.count() == 3
    gids = [window.group_list.item(i).data(ROLE_GID) for i in range(3)]

    window.group_list.setCurrentRow(1)
    window._ignore_current()

    # The group that slid up into row 1 is now under review, not the top one.
    assert window.group_list.currentRow() == 1
    assert window.group_list.currentItem().data(ROLE_GID) == gids[2]


def test_marking_everything_does_not_reset_the_selection(window, tmp_path):
    _three_ad_library(tmp_path)
    window.import_paths([tmp_path])
    scan_and_wait(window)
    window.group_list.setCurrentRow(2)
    chosen = window.group_list.currentItem().data(ROLE_GID)

    window._set_all_decisions(Decision.DELETE)

    assert window.group_list.currentItem().data(ROLE_GID) == chosen


def test_review_panels_are_locked_while_archives_are_rewritten(window, tmp_path, monkeypatch):
    """Browsing a group opens its archives for thumbnails, which blocks the swap on Windows."""
    from PySide6.QtWidgets import QDialog, QMessageBox

    from comiccleaner.core import remover

    _three_ad_library(tmp_path)
    window.import_paths([tmp_path])
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    entered, gate = threading.Event(), threading.Event()
    real_apply = remover.apply_plan

    def slow_apply(plan, **kwargs):
        entered.set()
        gate.wait(20)
        return real_apply(plan, **kwargs)

    monkeypatch.setattr(remover, "apply_plan", slow_apply)
    monkeypatch.setattr(
        "comiccleaner.gui.main_window._ConfirmDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr("comiccleaner.gui.main_window._ConfirmDialog.dry_run", lambda self: False)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)

    window.apply_removals()
    try:
        assert pump_until(entered.is_set, 10), "removal never started"
        assert not window.centralWidget().isEnabled()
    finally:
        gate.set()

    assert pump_until(lambda: window._removal_worker is None), "removal did not finish"
    assert window.centralWidget().isEnabled()


def test_ignoring_a_group_hides_it_permanently(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()

    window._ignore_current()
    assert window.groups == []

    window.rebuild_groups()
    assert window.groups == []


def test_threshold_change_regroups_without_rescanning(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    assert window.groups[0].page_count == 3

    window.settings.threshold = 0
    window.rebuild_groups()

    # Exact matching only sees the two byte-identical copies.
    assert window.groups[0].page_count == 2


def test_decisions_survive_regrouping(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window.groups[0].decision = Decision.DELETE

    window.rebuild_groups()

    assert window.groups[0].decision is Decision.DELETE


def test_apply_is_disabled_until_something_is_marked(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)

    assert not window.act_apply.isEnabled()
    window._set_all_decisions(Decision.DELETE)
    assert window.act_apply.isEnabled()


def test_unreadable_archive_is_reported_not_fatal(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    broken = tmp_path / "Broken.cbz"
    broken.write_bytes(b"this is not an archive at all")

    window.import_paths([broken])
    scan_and_wait(window)

    assert window.archives[broken].error is not None
    assert window.groups == []


def test_human_bytes_formats_sensibly():
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(5 * 1024 * 1024) == "5.0 MB"


def _page_count(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(1 for n in zf.namelist() if is_page_name(n))
