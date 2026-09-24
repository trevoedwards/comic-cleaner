"""The empty library's welcome page and the missing-archive-tool warning."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from comiccleaner.core import extern  # noqa: E402
from comiccleaner.gui.main_window import MainWindow  # noqa: E402

from .test_gui import scan_and_wait  # noqa: E402

# Enough of a RAR header for detect_kind; the body is junk, so it never extracts.
RAR_MAGIC = b"Rar!\x1a\x07\x00" + b"\x00" * 64


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
def no_tools(monkeypatch):
    """As if neither 7-Zip, UnRAR nor bsdtar were installed."""
    for where in ("comiccleaner.gui.main_window", "comiccleaner.gui.welcome"):
        monkeypatch.setattr(f"{where}.can_extract", lambda kind: False)


def _rar(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(RAR_MAGIC)
    return path


# -- welcome page ----------------------------------------------------------


def test_an_empty_library_shows_the_welcome_page(window, library):
    assert window.views.currentWidget() is window.welcome

    window.import_paths([library])
    assert window.views.currentWidget() is window.review

    for row in range(window.archive_list.count()):
        window.archive_list.item(row).setSelected(True)
    window.remove_selected_archives()
    assert window.views.currentWidget() is window.welcome


def test_the_welcome_buttons_import(window, library, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(library))

    window.welcome.btn_folder.click()

    assert len(window.archives) == 3


def test_dragging_over_lights_up_the_drop_target(window):
    window.welcome.set_drag_active(True)
    assert "solid" in window.welcome.frame.styleSheet()
    window.welcome.set_drag_active(False)
    assert "dashed" in window.welcome.frame.styleSheet()


def test_the_welcome_page_says_when_cbr_cannot_be_read(window, no_tools):
    window.welcome.refresh()

    assert "need an archive tool" in window.welcome.tools.text()
    assert extern.SEVENZIP_URL in window.welcome.tools.text()


def test_the_welcome_page_is_quiet_when_every_format_works(window, monkeypatch):
    monkeypatch.setattr("comiccleaner.gui.welcome.can_extract", lambda kind: True)

    window.welcome.refresh()

    assert "all supported" in window.welcome.tools.text()


# -- missing-tool banner ---------------------------------------------------


def test_importing_unreadable_books_raises_the_banner(window, tmp_path, no_tools):
    _rar(tmp_path / "rars", "Book.cbr")

    window.import_paths([tmp_path / "rars"])

    assert window.tools_banner.isVisibleTo(window)
    assert "1 book(s) cannot be read yet:" in window.tools_banner_text.text()


def test_a_cbz_never_raises_the_banner(window, library, no_tools):
    window.import_paths([library])

    assert not window.tools_banner.isVisibleTo(window)


def test_a_cbr_that_is_really_a_zip_never_raises_the_banner(window, library, no_tools):
    """Plenty of .cbr files are zips; those need no extra tool."""
    zipped = library / "Book 01.cbz"
    renamed = zipped.with_suffix(".cbr")
    zipped.rename(renamed)

    window.import_paths([renamed])

    assert not window.tools_banner.isVisibleTo(window)


def test_a_dismissed_banner_returns_only_for_more_books(window, tmp_path, no_tools):
    _rar(tmp_path / "one", "A.cbr")
    window.import_paths([tmp_path / "one"])
    window._dismiss_tools_banner()
    assert not window.tools_banner.isVisibleTo(window)

    window.import_paths([tmp_path / "one"])  # nothing new
    assert not window.tools_banner.isVisibleTo(window)

    _rar(tmp_path / "two", "B.cb7")
    window.import_paths([tmp_path / "two"])
    assert window.tools_banner.isVisibleTo(window)
    assert "2 book(s)" in window.tools_banner_text.text()


def test_check_again_picks_up_a_new_install(window, tmp_path, monkeypatch, no_tools):
    book = _rar(tmp_path / "rars", "Book.cbr").resolve()
    window.import_paths([tmp_path / "rars"])
    window.archives[book].error = "No tool found to read .cbr files."
    refreshed: list[bool] = []
    monkeypatch.setattr(
        "comiccleaner.gui.main_window.refresh_backends", lambda: refreshed.append(True)
    )

    # Still nothing installed: the banner stays and says so.
    window.recheck_archive_tools()
    assert refreshed
    assert window.tools_banner.isVisibleTo(window)
    assert "Still no 7-Zip" in window.status_label.text()

    # Installed now: the banner goes and the failed book is ready to retry.
    for where in ("comiccleaner.gui.main_window", "comiccleaner.gui.welcome"):
        monkeypatch.setattr(f"{where}.can_extract", lambda kind: True)
    window.recheck_archive_tools()
    assert not window.tools_banner.isVisibleTo(window)
    assert window.archives[book].error is None
    assert "Press Scan to read the 1 book(s)" in window.status_label.text()


def test_unreadable_cbr_after_a_scan_suggests_7zip(window, tmp_path, monkeypatch):
    """bsdtar or UnRAR may be present yet still fail where 7-Zip would not."""
    _rar(tmp_path / "rars", "Book.cbr")
    monkeypatch.setattr("comiccleaner.gui.main_window.sevenzip_path", lambda: None)
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))

    window.import_paths([tmp_path / "rars"])
    scan_and_wait(window)

    assert shown and "7-Zip reads more .cbr and .cb7 files" in shown[-1]


# -- detecting tools -------------------------------------------------------


@pytest.mark.parametrize(
    ("sevenzip", "unrar", "bsdtar", "rar", "sz"),
    [
        (None, None, None, False, False),
        ("7z", None, None, True, True),
        (None, "unrar", None, True, False),  # UnRAR reads RAR only
        (None, None, "bsdtar", True, True),
    ],
)
def test_can_extract_matches_what_each_tool_reads(monkeypatch, sevenzip, unrar, bsdtar, rar, sz):
    monkeypatch.setattr(extern, "sevenzip_path", lambda: sevenzip)
    monkeypatch.setattr(extern, "unrar_path", lambda: unrar)
    monkeypatch.setattr(extern, "bsdtar_path", lambda: bsdtar)

    assert extern.can_extract("rar") is rar
    assert extern.can_extract("7z") is sz


def test_refresh_backends_forgets_what_was_found(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(extern, "_first_existing", lambda names, c: calls.append("x") or None)
    extern.refresh_backends()

    extern.sevenzip_path()
    extern.sevenzip_path()  # cached
    extern.refresh_backends()
    extern.sevenzip_path()  # looked up again

    assert len(calls) == 2
    extern.refresh_backends()  # leave nothing stubbed behind in the cache


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_install_hint_names_7zip_on_every_platform(monkeypatch, platform):
    monkeypatch.setattr(extern.sys, "platform", platform)

    assert "7-Zip" in extern.install_hint()
