"""Known junk in the GUI: learned on removal, found and marked in new books."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from comiccleaner.core.model import Decision  # noqa: E402
from comiccleaner.core.signatures import FILE_FORMAT  # noqa: E402
from comiccleaner.gui.main_window import MainWindow  # noqa: E402
from comiccleaner.gui.remembered import RememberedDialog  # noqa: E402

from .conftest import make_page, write_archive  # noqa: E402
from .test_gui import apply_and_wait, scan_and_wait  # noqa: E402


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


@pytest.fixture
def quiet_dialogs(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)


def _new_book(tmp_path: Path, ad_page: bytes) -> Path:
    folder = tmp_path / "incoming"
    write_archive(folder / "Book 04.cbz", [make_page(seed=400), ad_page, make_page(seed=401)])
    return folder


def _clean_library(window: MainWindow, library: Path, monkeypatch) -> None:
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    apply_and_wait(window, monkeypatch)


def test_a_removed_advert_is_found_and_marked_in_a_new_book(
    window, library, tmp_path, ad_page, monkeypatch
):
    _clean_library(window, library, monkeypatch)
    assert len(window.cache.known_entries()) == 1
    assert window.groups == []  # the library itself is clean now

    window.import_paths([_new_book(tmp_path, ad_page)])
    scan_and_wait(window)

    [group] = window.groups
    assert group.known
    assert group.page_count == 1  # below the two-book minimum, found anyway
    assert group.decision is Decision.DELETE
    assert "known junk" in window.group_list.item(0).text()
    assert window.act_apply.isEnabled()


def test_the_summary_says_what_was_remembered(window, library, monkeypatch):
    shown: list[str] = []
    real = QMessageBox.setText
    monkeypatch.setattr(QMessageBox, "setText", lambda self, t: (shown.append(t), real(self, t)))

    _clean_library(window, library, monkeypatch)

    assert any("1 page(s) remembered as known junk" in text for text in shown)


def test_a_dry_run_teaches_nothing(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_and_wait(window, monkeypatch, dry_run=True)

    assert window.cache.known_entries() == []


def test_nothing_is_learned_or_marked_when_the_setting_is_off(
    window, library, tmp_path, ad_page, monkeypatch
):
    window.settings.remember_junk = False
    _clean_library(window, library, monkeypatch)
    assert window.cache.known_entries() == []

    # Even with something remembered, the setting switches matching off too.
    window.cache.remember("00", {0})
    window.import_paths([_new_book(tmp_path, ad_page)])
    scan_and_wait(window)
    assert window.groups == []


def test_a_decision_made_on_a_known_group_is_respected(
    window, library, tmp_path, ad_page, monkeypatch
):
    _clean_library(window, library, monkeypatch)
    window.import_paths([_new_book(tmp_path, ad_page)])
    scan_and_wait(window)
    window.groups[0].decision = Decision.KEEP

    window.rebuild_groups()

    assert window.groups[0].decision is Decision.KEEP


# -- the dialog ------------------------------------------------------------


def test_forgetting_known_junk_stops_it_being_found(
    window, library, tmp_path, ad_page, monkeypatch
):
    _clean_library(window, library, monkeypatch)
    window.import_paths([_new_book(tmp_path, ad_page)])

    dialog = RememberedDialog(window.cache, window.thumbs, window)
    try:
        assert dialog.tabs.currentWidget() is dialog.known
        listing = dialog.known.listing
        assert listing.count() == 1
        assert listing.item(0).text().startswith("3 copies in 3 book(s)")
        assert not listing.item(0).icon().isNull()  # the saved thumbnail
        listing.item(0).setSelected(True)
        dialog.forget_selected()
        assert dialog.changed and listing.count() == 0
    finally:
        dialog.done(QDialog.DialogCode.Accepted)

    scan_and_wait(window)
    assert window.groups == []


def test_export_and_import_through_the_dialog(
    window, library, tmp_path, monkeypatch, quiet_dialogs
):
    _clean_library(window, library, monkeypatch)
    exported = tmp_path / "junk.json"

    dialog = RememberedDialog(window.cache, window.thumbs, window)
    try:
        dialog.export_list(exported)
        assert json.loads(exported.read_text(encoding="utf-8"))["format"] == FILE_FORMAT

        window.cache.clear_known()
        dialog.import_list(exported)
        assert dialog.changed
        assert dialog.known.listing.count() == 1
        assert "imported" in dialog.known.listing.item(0).text()
    finally:
        dialog.done(QDialog.DialogCode.Accepted)


def test_a_bad_import_changes_nothing(window, tmp_path, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")

    dialog = RememberedDialog(window.cache, window.thumbs, window)
    try:
        dialog.import_list(bad)
    finally:
        dialog.done(QDialog.DialogCode.Accepted)

    assert warned and "Nothing was imported" in warned[0]
    assert not dialog.changed
    assert window.cache.known_entries() == []


def test_the_dialog_buttons_do_not_trip_over_qts_checked_flag(window, monkeypatch):
    """clicked(bool) would otherwise arrive as export_list(path=False)."""
    monkeypatch.setattr(
        "comiccleaner.gui.remembered.QFileDialog.getSaveFileName", lambda *a, **k: ("", "")
    )
    window.cache.remember("0a", {0x0A})
    dialog = RememberedDialog(window.cache, window.thumbs, window)
    try:
        dialog.btn_export.click()  # cancelled in the file dialog; must not raise
    finally:
        dialog.done(QDialog.DialogCode.Accepted)
