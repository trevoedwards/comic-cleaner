"""Drives the real window offscreen: import -> scan -> mark -> apply."""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from comicdedupe.core.archive import is_page_name  # noqa: E402
from comicdedupe.core.model import Decision  # noqa: E402
from comicdedupe.gui.main_window import MainWindow, human_bytes  # noqa: E402


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
        "comicdedupe.gui.main_window.cache_path", lambda: tmp_path / "cache.sqlite"
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
        "comicdedupe.gui.main_window._ConfirmDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "comicdedupe.gui.main_window._ConfirmDialog.dry_run", lambda self: dry_run
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)

    window.apply_removals()
    assert pump_until(lambda: window._removal_worker is None), "removal did not finish"


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


def test_dry_run_leaves_files_alone(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_and_wait(window, monkeypatch, dry_run=True)

    assert _page_count(library / "Book 01.cbz") == 5
    assert not list(library.glob("*.bak"))


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
