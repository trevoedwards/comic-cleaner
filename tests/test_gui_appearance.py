"""Theme switching, column alignment, icon assets and backup cleanup."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from comiccleaner import GITHUB_URL  # noqa: E402
from comiccleaner.core.model import Decision  # noqa: E402
from comiccleaner.gui.about import AboutDialog  # noqa: E402
from comiccleaner.gui.main_window import (  # noqa: E402
    PANEL_FOOTER_HEIGHT,
    PANEL_HEADER_HEIGHT,
    MainWindow,
)
from comiccleaner.gui.settings import AppSettings  # noqa: E402
from comiccleaner.gui.theme import Theme, apply_theme, colour, effective_is_dark  # noqa: E402

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
        "comiccleaner.gui.main_window.cache_path", lambda: tmp_path / "cache.sqlite"
    )
    win = MainWindow()
    yield win
    win.close()
    apply_theme(Theme.LIGHT)  # leave a predictable state for the next test


# -- theme -----------------------------------------------------------------


def test_theme_parses_leniently():
    assert Theme.parse("dark") is Theme.DARK
    assert Theme.parse("LIGHT") is Theme.LIGHT
    assert Theme.parse("nonsense") is Theme.SYSTEM
    assert Theme.parse(None) is Theme.SYSTEM


def test_apply_theme_changes_the_palette(qapp):
    apply_theme(Theme.LIGHT)
    light = qapp.palette().color(QPalette.ColorRole.Window)
    assert effective_is_dark() is False

    apply_theme(Theme.DARK)
    dark = qapp.palette().color(QPalette.ColorRole.Window)
    assert effective_is_dark() is True

    assert dark.lightness() < light.lightness()
    apply_theme(Theme.LIGHT)


def test_follow_system_clears_the_override_before_asking_the_os(qapp, monkeypatch):
    """A forced Light/Dark scheme masks the OS, so it must be dropped first.

    Otherwise switching from a forced theme to "Follow system" reads back the
    forced choice and never follows the OS. Offscreen Qt cannot reproduce the
    masking itself, so this checks the order of the two calls.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QStyleHints

    from comiccleaner.gui import theme

    events: list[str] = []
    real_set = QStyleHints.setColorScheme

    def recording_set(self, scheme):
        events.append("reset" if scheme is Qt.ColorScheme.Unknown else "force")
        return real_set(self, scheme)

    def recording_query() -> bool:
        events.append("query")
        return False

    monkeypatch.setattr(QStyleHints, "setColorScheme", recording_set)
    monkeypatch.setattr(theme, "system_is_dark", recording_query)

    apply_theme(Theme.SYSTEM)

    assert events == ["reset", "query"]


def test_follow_system_tracks_the_os_while_running(window, monkeypatch):
    """Offscreen Qt never changes scheme by itself, so the OS's signal is emitted here."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    from comiccleaner.gui import theme

    hints = QGuiApplication.styleHints()
    window.settings.theme = "system"
    monkeypatch.setattr(theme, "system_is_dark", lambda: False)
    apply_theme(Theme.SYSTEM)
    assert effective_is_dark() is False

    monkeypatch.setattr(theme, "system_is_dark", lambda: True)
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    assert effective_is_dark() is True
    assert window.detail_hint.styleSheet().endswith(f"{colour('muted').name()};")

    # An explicit choice is left alone whatever the OS does.
    window.settings.theme = "light"
    apply_theme(Theme.LIGHT)
    hints.colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    assert effective_is_dark() is False
    apply_theme(Theme.LIGHT)


def test_semantic_colours_differ_between_themes(qapp):
    apply_theme(Theme.LIGHT)
    light_delete = colour("delete").name()
    apply_theme(Theme.DARK)
    dark_delete = colour("delete").name()
    apply_theme(Theme.LIGHT)

    assert light_delete != dark_delete


def test_settings_round_trip_theme():
    settings = AppSettings(theme="dark")
    assert settings.theme_mode() is Theme.DARK
    assert AppSettings().theme_mode() is Theme.SYSTEM


def test_restyle_recolours_group_rows_for_the_theme(window, library):
    """Group rows are painted with explicit colours, so they must be re-applied."""
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_theme(Theme.LIGHT)
    window._restyle()
    light_row = window.group_list.item(0).foreground().color().name()

    apply_theme(Theme.DARK)
    window._restyle()
    dark_row = window.group_list.item(0).foreground().color().name()

    assert light_row != dark_row


# -- column alignment ------------------------------------------------------


def test_all_three_columns_share_body_geometry(window, library):
    window.import_paths([library])  # an empty library shows the welcome page instead
    window.resize(1400, 860)
    window.show()
    QCoreApplication.processEvents()

    spans = []
    for widget in (window.archive_list, window.group_list, window.page_list):
        top = widget.mapTo(window, widget.rect().topLeft()).y()
        spans.append((top, top + widget.height()))

    assert len({s[0] for s in spans}) == 1, f"tops differ: {spans}"
    assert len({s[1] for s in spans}) == 1, f"bottoms differ: {spans}"


def test_panel_chrome_is_fixed_height(window, library):
    window.import_paths([library])
    window.show()
    QCoreApplication.processEvents()
    # The body starts below exactly one header plus the layout margin.
    top = window.archive_list.mapTo(window, window.archive_list.rect().topLeft()).y()
    assert PANEL_HEADER_HEIGHT > 0 and PANEL_FOOTER_HEIGHT > 0
    assert top > PANEL_HEADER_HEIGHT


# -- help ------------------------------------------------------------------


def test_about_dialog_credits_the_developer(qapp):
    from PySide6.QtWidgets import QLabel

    dialog = AboutDialog()
    text = " ".join(label.text() for label in dialog.findChildren(QLabel))

    assert "Trevor Edwards" in text
    assert "Playback Software" in text
    assert GITHUB_URL in text
    dialog.close()


def test_help_actions_exist(window):
    assert window.act_about is not None
    assert window.act_github is not None
    assert window.act_github.toolTip() == GITHUB_URL


# -- backup cleanup --------------------------------------------------------


def test_delete_backups_reports_what_it_removed(window, tmp_path):
    backups = []
    for index in range(3):
        path = tmp_path / f"Book {index}.cbz.bak"
        path.write_bytes(b"x" * 100)
        backups.append(path)

    removed, freed = window._delete_backups(backups)

    assert removed == 3
    assert freed == 300
    assert not any(b.exists() for b in backups)


def test_delete_backups_survives_a_locked_file(window, tmp_path):
    good = tmp_path / "a.cbz.bak"
    good.write_bytes(b"x" * 10)
    missing = tmp_path / "gone.cbz.bak"  # never created

    removed, freed = window._delete_backups([good, missing])

    assert removed == 1
    assert freed == 10


def test_auto_cleanup_removes_backups_after_a_clean_run(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    window.settings.delete_backups_after = True
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_and_wait(window, monkeypatch)

    assert not list(library.glob("*.bak"))
    with zipfile.ZipFile(library / "Book 01.cbz") as zf:
        assert sum(1 for n in zf.namelist() if n.endswith(".jpg")) == 4


def test_backups_are_kept_when_auto_cleanup_is_off(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    window.settings.delete_backups_after = False
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_and_wait(window, monkeypatch)

    assert len(list(library.glob("*.bak"))) == 3
    assert len(window._last_backups) == 3


def test_clean_up_backups_action_removes_them(window, library, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window.import_paths([library])
    for name in ("Book 01.cbz", "Book 02.cbz"):
        (library / f"{name}.bak").write_bytes(b"old backup")

    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    window.clean_up_backups()

    assert not list(library.glob("*.bak"))


def test_clean_up_backups_respects_a_no(window, library, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window.import_paths([library])
    (library / "Book 01.cbz.bak").write_bytes(b"old backup")

    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    window.clean_up_backups()

    assert len(list(library.glob("*.bak"))) == 1


def test_auto_cleanup_keeps_backups_when_something_failed(window, library, monkeypatch):
    """A partial failure is exactly when the originals are worth keeping."""

    window.import_paths([library])
    window.settings.threshold = 8
    window.settings.delete_backups_after = True
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    real_apply = None

    def failing_apply(plan, **kwargs):
        # Succeed for the first archive, fail for the rest.
        nonlocal real_apply
        result = real_apply(plan, **kwargs)
        if plan.archive.name != "Book 01.cbz":
            result.error = "simulated failure"
        return result

    import comiccleaner.core.remover as remover

    real_apply = remover.apply_plan
    monkeypatch.setattr(remover, "apply_plan", failing_apply)

    apply_and_wait(window, monkeypatch)

    # The successful archive's backup must survive the failed run.
    assert list(library.glob("*.bak"))


# -- icon ------------------------------------------------------------------


def test_icon_asset_is_present_and_square():
    from PIL import Image

    from comiccleaner.resources import icon_path

    found = icon_path()
    assert found is not None, "bundled icon.png is missing"
    with Image.open(found) as img:
        assert img.width == img.height, "icon must be square"
        assert img.width >= 256


def test_windows_ico_exists_for_the_build():
    ico = Path(__file__).resolve().parents[1] / "assets" / "comiccleaner.ico"
    assert ico.is_file(), "assets/comiccleaner.ico is needed by build.py"


def test_window_has_an_icon(window):
    assert not window.windowIcon().isNull()


def test_pump_until_helper_is_importable():
    assert callable(pump_until)


def test_damaged_saved_settings_fall_back_instead_of_crashing(qapp, monkeypatch):
    """Settings load while the window is built, so a bad value must not raise."""
    stored = {"threshold": "abc", "min_pages": "", "min_archives": "9999"}
    monkeypatch.setattr(
        QSettings, "value", lambda self, key, default=None: stored.get(key, default)
    )

    loaded = AppSettings.load()

    defaults = AppSettings()
    assert loaded.threshold == defaults.threshold
    assert loaded.min_pages == defaults.min_pages
    assert loaded.min_archives == 100  # out of range is clamped, not trusted
