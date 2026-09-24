"""The opt-in update check. No test here touches the network."""

from __future__ import annotations

import io
import json
import os
import urllib.error

import pytest

from comiccleaner import __version__
from comiccleaner.core import updates
from comiccleaner.core.updates import (
    Release,
    UpdateCheckError,
    api_url,
    is_newer,
    latest_release,
    version_key,
)

# -- versions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [("0.1.0", (0, 1, 0)), ("v1.2", (1, 2)), ("2.0.0-beta.1", (2, 0, 0)), ("nonsense", ())],
)
def test_version_key(text, key):
    assert version_key(text) == key


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        ("0.2.0", "0.1.0", True),
        ("v0.1.1", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("0.1", "0.1.0", False),  # padded, so equal
        ("0.0.9", "0.1.0", False),
        ("0.10.0", "0.9.0", True),  # numeric, not alphabetical
        ("garbage", "0.1.0", False),
    ],
)
def test_is_newer(candidate, current, newer):
    assert is_newer(candidate, current) is newer


def test_api_url_comes_from_the_repository_link():
    assert api_url("https://github.com/someone/thing") == (
        "https://api.github.com/repos/someone/thing/releases/latest"
    )
    with pytest.raises(UpdateCheckError):
        api_url("https://example.com/someone/thing")


# -- asking GitHub ---------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answer(monkeypatch, payload=None, *, error=None, raw=None):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        if error is not None:
            raise error
        body = raw if raw is not None else json.dumps(payload).encode()
        return _Response(body)

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_the_latest_release_is_read(monkeypatch):
    seen = _answer(monkeypatch, {
        "tag_name": "v0.3.0",
        "html_url": "https://github.com/trevoedwards/comic-cleaner/releases/tag/v0.3.0",
    })

    release = latest_release()

    assert release == Release(
        "0.3.0", "https://github.com/trevoedwards/comic-cleaner/releases/tag/v0.3.0"
    )
    assert seen["request"].get_header("User-agent").endswith(__version__)


def test_an_odd_release_page_falls_back_to_the_releases_page(monkeypatch):
    _answer(monkeypatch, {"tag_name": "v0.3.0", "html_url": "https://evil.example/x"})

    assert latest_release().url.endswith("/releases/latest")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"error": urllib.error.HTTPError("u", 404, "nf", None, None)}, "no release"),
        ({"error": urllib.error.HTTPError("u", 403, "rate", None, None)}, "403"),
        ({"error": urllib.error.URLError("offline")}, "could not reach"),
        ({"raw": b"<html>"}, "not JSON"),
        ({"payload": {"tag_name": "latest"}}, "no version"),
    ],
    ids=["no-release", "http-error", "offline", "not-json", "no-version"],
)
def test_failures_become_one_kind_of_error(monkeypatch, kwargs, message):
    _answer(monkeypatch, **kwargs)

    with pytest.raises(UpdateCheckError, match=message):
        latest_release()


# -- the GUI ---------------------------------------------------------------


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from comiccleaner.gui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(QSettings, "value", lambda self, key, default=None: default)
    monkeypatch.setattr(QSettings, "setValue", lambda self, key, value: None)
    monkeypatch.setattr(QSettings, "sync", lambda self: None)
    monkeypatch.setattr(
        "comiccleaner.gui.main_window.cache_path", lambda: tmp_path / "data" / "c.sqlite"
    )
    win = MainWindow()
    yield win
    win.close()


def _latest(monkeypatch, release=None, error=None):
    def fake():
        if error:
            raise UpdateCheckError(error)
        return release

    monkeypatch.setattr("comiccleaner.gui.workers.latest_release", fake)


def _messages(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self.text()))
    return shown


def _wait(window):
    from .test_gui import pump_until

    assert pump_until(lambda: window._update_checker is None, 10), "check never finished"


def test_a_newer_release_shows_a_link_quietly(window, monkeypatch):
    shown = _messages(monkeypatch)
    _latest(monkeypatch, Release("99.0.0", "https://github.com/x/y/releases/tag/v99.0.0"))

    window.check_for_updates(quiet=True)
    _wait(window)

    assert window.update_link.isVisibleTo(window)
    assert "99.0.0 is available" in window.update_link.text()
    assert shown == []  # no pop-up at startup


def test_a_quiet_check_that_fails_says_nothing(window, monkeypatch):
    shown = _messages(monkeypatch)
    _latest(monkeypatch, error="could not reach GitHub")

    window.check_for_updates(quiet=True)
    _wait(window)

    assert shown == []
    assert not window.update_link.isVisibleTo(window)


def test_asking_by_hand_reports_either_way(window, monkeypatch):
    shown = _messages(monkeypatch)

    _latest(monkeypatch, Release(__version__, "https://github.com/x/y"))
    window.check_for_updates(quiet=False)
    _wait(window)
    _latest(monkeypatch, error="could not reach GitHub")
    window.check_for_updates(quiet=False)
    _wait(window)

    assert shown[0] == f"You have the latest version, {__version__}."
    assert "Could not reach GitHub" in shown[1]


def test_startup_does_not_check_unless_asked_to(window, monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr(
        "comiccleaner.gui.workers.UpdateChecker.start", lambda self: started.append(True)
    )

    window.open_startup()
    assert started == []  # off by default: it is the app's only network request

    window.settings.check_updates = True
    window.open_startup()
    assert started == [True]
