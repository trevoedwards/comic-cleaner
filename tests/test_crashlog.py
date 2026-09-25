"""Crash reports land next to wherever the app was launched from."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from comiccleaner import crashlog


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Run each test in its own launch directory with fresh module state."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(crashlog, "_crash_dir", None)
    monkeypatch.setattr(crashlog, "_notifier", None)
    crashlog._recent.clear()
    yield tmp_path


def _boom(message: str = "kaboom") -> tuple:
    try:
        raise ValueError(message)
    except ValueError:
        return sys.exc_info()


# -- where reports go ------------------------------------------------------


def test_report_lands_in_a_crashlog_folder_beside_the_launch_dir(isolated):
    path = crashlog.write_report(*_boom())

    assert path is not None
    assert path.parent == isolated / "crashlog"
    assert path.parent.is_dir()
    assert path.name.startswith("crash-")
    assert path.suffix == ".log"


def test_launch_directory_is_the_working_directory(isolated):
    assert crashlog.launch_directory() == isolated


def _unwritable(tmp_path: Path, name: str) -> Path:
    """A directory that cannot be created, portably.

    Creating a directory *inside a regular file* fails on every platform, which
    beats guessing at a protected path - "/nonexistent" is happily creatable at
    the root of the current drive on Windows.
    """
    blocker = tmp_path / name
    blocker.write_text("not a directory", encoding="utf-8")
    return blocker / "sub"


def test_falls_back_when_the_launch_directory_is_not_writable(
    isolated, tmp_path, monkeypatch
):
    """A binary in Program Files or /usr/bin must not silently lose the report."""
    fallback = tmp_path / "appdata"
    monkeypatch.setattr(
        crashlog, "launch_directory", lambda: _unwritable(tmp_path, "blocked")
    )
    monkeypatch.setattr(crashlog, "_fallback_directory", lambda: fallback)

    path = crashlog.write_report(*_boom())

    assert path is not None
    assert path.parent == fallback / "crashlog"


def test_the_fallback_is_the_shared_data_folder_without_importing_the_gui():
    """A headless crash must not pull in Qt just to find somewhere to write."""
    script = (
        "import sys\n"
        "from comiccleaner import crashlog, paths\n"
        "assert crashlog._fallback_directory() == paths.data_dir()\n"
        "loaded = sorted(m for m in sys.modules\n"
        "                if m.startswith(('PySide6', 'comiccleaner.gui')))\n"
        "assert not loaded, loaded\n"
    )
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60
    )

    assert result.returncode == 0, result.stderr


def test_returns_none_when_nowhere_is_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        crashlog, "launch_directory", lambda: _unwritable(tmp_path, "blocked-a")
    )
    monkeypatch.setattr(
        crashlog, "_fallback_directory", lambda: _unwritable(tmp_path, "blocked-b")
    )

    assert crashlog.write_report(*_boom()) is None


# -- contents --------------------------------------------------------------


def test_report_contains_the_traceback_and_environment(isolated):
    path = crashlog.write_report(*_boom("something specific"))

    text = path.read_text(encoding="utf-8")

    assert "ValueError: something specific" in text
    assert "Traceback" in text
    assert "Comic Cleaner" in text
    assert "platform  :" in text
    assert "frozen    :" in text


def test_report_replays_recent_log_lines(isolated):
    crashlog.install()
    logging.getLogger("demo").warning("an archive was unreadable")

    path = crashlog.write_report(*_boom())

    assert "an archive was unreadable" in path.read_text(encoding="utf-8")


def test_report_records_the_thread_name(isolated):
    path = crashlog.write_report(*_boom(), thread="ScanWorker")

    assert "thread    : ScanWorker" in path.read_text(encoding="utf-8")


# -- collisions ------------------------------------------------------------


def test_two_crashes_in_the_same_second_both_survive(isolated):
    """Timestamp plus pid alone would make the second report clobber the first."""
    first = crashlog.write_report(*_boom("first"))
    second = crashlog.write_report(*_boom("second"))

    assert first != second
    assert "first" in first.read_text(encoding="utf-8")
    assert "second" in second.read_text(encoding="utf-8")
    assert len(list((isolated / "crashlog").glob("crash-*.log"))) == 2


# -- hooks -----------------------------------------------------------------


def test_installed_excepthook_writes_a_report(isolated, monkeypatch):
    monkeypatch.setattr(crashlog, "_installed", False)
    crashlog.install()
    try:
        sys.excepthook(*_boom("via excepthook"))
    finally:
        sys.excepthook = sys.__excepthook__

    reports = list((isolated / "crashlog").glob("crash-*.log"))
    assert len(reports) == 1
    assert "via excepthook" in reports[0].read_text(encoding="utf-8")


def test_worker_thread_crashes_are_reported(isolated, monkeypatch):
    """Threads die silently by default, which would hide a scan-worker crash."""
    monkeypatch.setattr(crashlog, "_installed", False)
    original = threading.excepthook
    crashlog.install()
    try:
        worker = threading.Thread(
            target=lambda: (_ for _ in ()).throw(RuntimeError("worker died")),
            name="ScanWorker",
        )
        worker.start()
        worker.join()
    finally:
        threading.excepthook = original
        sys.excepthook = sys.__excepthook__

    reports = list((isolated / "crashlog").glob("crash-*.log"))
    assert reports, "a worker-thread crash produced no report"
    text = reports[0].read_text(encoding="utf-8")
    assert "worker died" in text
    assert "ScanWorker" in text


def test_keyboard_interrupt_is_not_a_crash(isolated, monkeypatch):
    """Ctrl+C is a user action; it must not litter the folder with reports."""
    monkeypatch.setattr(crashlog, "_installed", False)
    calls = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: calls.append(a))
    crashlog.install()
    try:
        crashlog._handle(KeyboardInterrupt, KeyboardInterrupt(), None)
    finally:
        sys.excepthook = sys.__excepthook__

    assert calls, "KeyboardInterrupt should reach the default hook"
    assert not list((isolated / "crashlog").glob("crash-*.log"))


def test_notifier_is_told_where_the_report_went(isolated, monkeypatch):
    monkeypatch.setattr(crashlog, "_installed", False)
    seen: list[tuple] = []
    crashlog.install(notifier=lambda path, summary: seen.append((path, summary)))
    try:
        crashlog._handle(*_boom("notify me"))
    finally:
        sys.excepthook = sys.__excepthook__

    assert len(seen) == 1
    path, summary = seen[0]
    assert path is not None and path.exists()
    assert "notify me" in summary


def test_a_broken_notifier_does_not_mask_the_crash(isolated, monkeypatch):
    monkeypatch.setattr(crashlog, "_installed", False)

    def broken(path, summary):
        raise RuntimeError("the notifier itself is broken")

    crashlog.install(notifier=broken)
    try:
        crashlog._handle(*_boom("original problem"))
    finally:
        sys.excepthook = sys.__excepthook__

    reports = list((isolated / "crashlog").glob("crash-*.log"))
    assert reports, "the report must still be written"
    assert "original problem" in reports[0].read_text(encoding="utf-8")


# -- listing ---------------------------------------------------------------


def test_existing_reports_are_listed_newest_first(isolated):
    import os
    import time

    older = crashlog.write_report(*_boom("older"))
    time.sleep(0.01)
    newer = crashlog.write_report(*_boom("newer"))
    os.utime(older, (1, 1))  # force a clearly older mtime

    found = crashlog.existing_reports()

    assert found[0] == newer
    assert older in found


def test_no_reports_when_nothing_has_crashed(isolated):
    assert crashlog.existing_reports() == []
