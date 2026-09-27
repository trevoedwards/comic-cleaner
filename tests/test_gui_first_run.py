"""The empty library's welcome page and the missing-archive-tool warning."""

from __future__ import annotations

import os
import subprocess
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
    assert "Still no archive tool" in window.status_label.text()

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
    ("sevenzip", "reads_rar", "unrar", "bsdtar", "rar", "sz"),
    [
        (None, False, None, None, False, False),
        ("7z", True, "7z", None, True, True),
        # Debian and Ubuntu's 7-Zip: no RAR codec, and unrar_path falls back to it.
        ("/usr/bin/7z", False, "/usr/bin/7z", None, False, True),
        ("/usr/bin/7z", False, "/usr/bin/unrar", None, True, True),
        (None, False, r"C:\Program Files\WinRAR\UnRAR.exe", None, True, False),
        (None, False, None, "bsdtar", True, True),
    ],
)
def test_can_extract_matches_what_each_tool_reads(
    monkeypatch, sevenzip, reads_rar, unrar, bsdtar, rar, sz
):
    monkeypatch.setattr(extern, "sevenzip_path", lambda: sevenzip)
    monkeypatch.setattr(extern, "sevenzip_reads_rar", lambda: reads_rar)
    monkeypatch.setattr(extern, "unrar_path", lambda: unrar)
    monkeypatch.setattr(extern, "bsdtar_path", lambda: bsdtar)

    assert extern.can_extract("rar") is rar
    assert extern.can_extract("7z") is sz


def test_refresh_backends_forgets_what_was_found(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(extern, "_override", lambda: None)
    monkeypatch.setattr(extern, "_all_existing", lambda names, c: calls.append("x") or [])
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
    monkeypatch.setattr(extern, "sevenzip_path", lambda: None)

    assert "7-Zip" in extern.install_hint()


# What `7z i` lists, trimmed. p7zip-full knows the RAR format but has no codec
# to unpack it without the non-free p7zip-rar; Debian's 7zip lacks both.
_P7ZIP_FULL = """
Formats:
 0  ...F..................  Rar      rar r00       R a r ! 1A 07 00
 0 C...F..........c.a.m+..  7z       7z            7 z BC AF ' 1C

Codecs:
 0 4ED   303011B BCJ2
 0  ED   30101   LZMA
"""
_WITH_RAR = _P7ZIP_FULL + """ 0  D    40301   Rar1
 0  D    40305   Rar5
"""


@pytest.mark.parametrize(
    ("listing", "reads"),
    [(_P7ZIP_FULL, False), (_WITH_RAR, True), ("7-Zip 9.20, no listing\n", True)],
)
def test_sevenzip_reads_rar_goes_by_the_codecs(monkeypatch, listing, reads):
    monkeypatch.setattr(
        extern, "_run",
        lambda cmd, timeout=600: subprocess.CompletedProcess(cmd, 0, listing.encode(), b""),
    )
    extern._reads_rar.cache_clear()
    try:
        assert extern._reads_rar("/usr/bin/7z") is reads
    finally:
        extern._reads_rar.cache_clear()


def test_sevenzip_that_cannot_list_is_trusted(monkeypatch):
    def fail(cmd, timeout=600):
        raise OSError("gone")

    monkeypatch.setattr(extern, "_run", fail)
    extern._reads_rar.cache_clear()
    try:
        assert extern._reads_rar("/usr/bin/7z") is True
    finally:
        extern._reads_rar.cache_clear()


@pytest.mark.parametrize(
    ("found", "chosen"),
    [
        # A distribution's 7z first on PATH, the official 7zz after it.
        (["/usr/bin/7z", "/usr/local/bin/7zz"], "/usr/local/bin/7zz"),
        # Nothing reads RAR: the first found still reads .cb7.
        (["/usr/bin/7z", "/usr/bin/7za"], "/usr/bin/7z"),
        ([], None),
    ],
)
def test_the_7zip_that_reads_rar_is_preferred(monkeypatch, found, chosen):
    monkeypatch.setattr(extern, "_override", lambda: None)
    monkeypatch.setattr(extern, "_all_existing", lambda names, candidates: list(found))
    monkeypatch.setattr(extern, "_reads_rar", lambda path: path.endswith("7zz"))
    extern.sevenzip_path.cache_clear()
    try:
        assert extern.sevenzip_path() == chosen
    finally:
        extern.sevenzip_path.cache_clear()


def test_the_override_wins_even_without_rar(monkeypatch, tmp_path):
    mine = tmp_path / "7z"
    mine.write_bytes(b"")
    monkeypatch.setenv("COMICCLEANER_7Z", str(mine))
    monkeypatch.setattr(extern, "_all_existing", lambda names, c: ["/usr/local/bin/7zz"])
    monkeypatch.setattr(extern, "_reads_rar", lambda path: path.endswith("7zz"))
    extern.sevenzip_path.cache_clear()
    try:
        assert extern.sevenzip_path() == str(mine)
    finally:
        extern.sevenzip_path.cache_clear()


def test_the_unrar_row_names_only_unrar(monkeypatch):
    """unrar_path falls back to 7-Zip, which then showed up as "UnRAR: ...7z.exe"."""
    monkeypatch.setattr(extern, "sevenzip_path", lambda: r"C:\7zip\7z.exe")
    monkeypatch.setattr(extern, "bsdtar_path", lambda: None)
    monkeypatch.setattr(extern, "unrar_path", lambda: r"C:\7zip\7z.exe")
    assert extern.describe_backends()["UnRAR"] is None

    monkeypatch.setattr(extern, "unrar_path", lambda: "/usr/bin/unrar")
    assert extern.describe_backends()["UnRAR"] == "/usr/bin/unrar"


@pytest.mark.parametrize(("platform", "system"), [("linux", "Linux"), ("darwin", "macOS")])
def test_hint_asks_for_the_official_7zip_when_the_one_found_lacks_rar(
    monkeypatch, platform, system
):
    """Debian's, Ubuntu's and Homebrew's 7-Zip read .cb7 but no compressed .cbr."""
    monkeypatch.setattr(extern.sys, "platform", platform)
    monkeypatch.setattr(extern, "sevenzip_path", lambda: "/usr/bin/7z")
    monkeypatch.setattr(extern, "sevenzip_reads_rar", lambda: False)

    hint = extern.install_hint()
    assert hint.startswith(f"Install the official 7-Zip for {system}, 7zz from 7-zip.org")
    assert "without RAR support" in hint
    assert "unrar" not in hint.lower()


def test_a_rar_that_7zip_cannot_read_says_to_get_the_official_7zip(monkeypatch, tmp_path):
    """Debian's 7-Zip fails a .cbr with nothing on stderr: 'Tried: 7z exited 2'."""
    monkeypatch.setattr(extern.sys, "platform", "linux")
    monkeypatch.setattr(extern, "sevenzip_path", lambda: "/usr/bin/7z")
    monkeypatch.setattr(extern, "unrar_path", lambda: "/usr/bin/7z")
    monkeypatch.setattr(extern, "bsdtar_path", lambda: None)
    monkeypatch.setattr(extern, "sevenzip_reads_rar", lambda: False)
    monkeypatch.setattr(
        extern, "_run", lambda cmd, timeout=600: subprocess.CompletedProcess(cmd, 2, b"", b"")
    )

    with pytest.raises(extern.ExtractionError) as caught:
        extern.extract_all(tmp_path / "Book.cbr", tmp_path / "out", kind="rar")

    assert "7z exited 2" in str(caught.value)
    assert "official 7-Zip for Linux" in str(caught.value)


def test_no_tool_at_all_gives_the_platform_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(extern.sys, "platform", "win32")
    for finder in ("sevenzip_path", "unrar_path", "bsdtar_path"):
        monkeypatch.setattr(extern, finder, lambda: None)

    with pytest.raises(extern.ExtractionError) as caught:
        extern.extract_all(tmp_path / "Book.cbr", tmp_path / "out", kind="rar")

    assert "No tool found to read .cbr files" in str(caught.value)
    assert "7-zip.org" in str(caught.value)
