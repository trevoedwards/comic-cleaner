"""Writing a crash report when something goes unhandled.

Reports land in a `crashlog` folder beside wherever the app was launched from,
so a user who double-clicks the binary finds the log next to it rather than
buried in an application-data directory.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import logging
import os
import platform
import sys
import threading
import traceback
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)

CRASHLOG_DIRNAME = "crashlog"

# How many recent log records to replay into the report. Enough to show what
# the app was doing, small enough not to bloat the file.
_CONTEXT_RECORDS = 200

_recent: deque[str] = deque(maxlen=_CONTEXT_RECORDS)
_installed = False
_crash_dir: Path | None = None
_notifier: Callable[[Path | None, str], None] | None = None


class _RingBufferHandler(logging.Handler):
    """Keeps the most recent log lines so a crash report has context."""

    def emit(self, record: logging.LogRecord) -> None:
        # Logging must never be the thing that breaks.
        with contextlib.suppress(Exception):
            _recent.append(self.format(record))


def launch_directory() -> Path:
    """Where the app was launched from.

    For a frozen build this is the working directory the binary was started in,
    which for a double-clicked executable is the folder containing it. sys
    argv[0] is used as a fallback for launchers that start elsewhere.
    """
    try:
        return Path.cwd()
    except OSError:
        pass
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        parent = Path(argv0).resolve().parent
        if parent.is_dir():
            return parent
    return Path.home()


def _fallback_directory() -> Path:
    """Used when the launch directory cannot be written to.

    Binaries in Program Files, /usr/bin or a read-only mount would otherwise
    lose the report entirely, which is the one moment it matters.
    """
    try:
        from .gui.settings import data_dir

        return data_dir()
    except Exception:
        return Path.home() / ".comiccleaner"


def crash_dir(create: bool = True) -> Path | None:
    """The folder crash reports go in, or None if nowhere is writable."""
    global _crash_dir
    if _crash_dir is not None and not create:
        return _crash_dir

    for base in (launch_directory(), _fallback_directory()):
        candidate = base / CRASHLOG_DIRNAME
        if not create:
            return candidate
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".writable"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            continue
        _crash_dir = candidate
        return candidate
    return None


def _environment() -> str:
    from . import APP_NAME, __version__

    lines = [
        f"{APP_NAME} {__version__}",
        f"when      : {_datetime.datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"python    : {platform.python_version()} ({sys.executable})",
        f"platform  : {platform.platform()}",
        f"frozen    : {getattr(sys, 'frozen', False)}",
        f"launched  : {launch_directory()}",
        f"pid       : {os.getpid()}",
        f"argv      : {sys.argv}",
    ]
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        lines.append(f"pyside6   : {pyside_version} (Qt {qVersion()})")
    except Exception:
        lines.append("pyside6   : unavailable")
    try:
        from .core.extern import describe_backends

        tools = ", ".join(f"{n}={p or 'none'}" for n, p in describe_backends().items())
        lines.append(f"tools     : {tools}")
    except Exception:
        pass
    return "\n".join(lines)


def write_report(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
    *,
    thread: str | None = None,
) -> Path | None:
    """Write one crash report. Returns the path, or None if it could not be saved."""
    stamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    body = [
        "=" * 70,
        f"{exc_type.__name__}: {exc}",
        "=" * 70,
        "",
        _environment(),
    ]
    if thread:
        body.append(f"thread    : {thread}")
    body += [
        "",
        "-" * 70,
        "Traceback",
        "-" * 70,
        "".join(traceback.format_exception(exc_type, exc, tb)).rstrip(),
    ]
    if _recent:
        body += ["", "-" * 70, "Recent log", "-" * 70, *_recent]

    destination = crash_dir()
    if destination is None:
        return None

    path = _unique_path(destination, stamp)
    try:
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def _unique_path(destination: Path, stamp: str) -> Path:
    """A report filename that cannot clobber an earlier one.

    A cascade of failures can easily produce several crashes inside the same
    second and process, which a timestamp-plus-pid name alone would overwrite.
    """
    base = f"crash-{stamp}-{os.getpid()}"
    candidate = destination / f"{base}.log"
    counter = 2
    while candidate.exists():
        candidate = destination / f"{base}-{counter}.log"
        counter += 1
    return candidate


def _handle(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
    *,
    thread: str | None = None,
) -> None:
    # Ctrl+C is a user action, not a crash.
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return

    try:
        path = write_report(exc_type, exc, tb, thread=thread)
    except Exception:  # a failure in here must not mask the original crash
        path = None

    with contextlib.suppress(Exception):
        log.critical("Unhandled %s", exc_type.__name__, exc_info=(exc_type, exc, tb))

    if sys.stderr is not None:
        with contextlib.suppress(Exception):
            traceback.print_exception(exc_type, exc, tb)
            if path is not None:
                print(f"\nCrash report written to: {path}", file=sys.stderr)

    summary = f"{exc_type.__name__}: {exc}"
    if _notifier is not None:
        with contextlib.suppress(Exception):
            _notifier(path, summary)


def install(notifier: Callable[[Path | None, str], None] | None = None) -> None:
    """Route unhandled exceptions, on any thread, into a crash report.

    `notifier` is called afterwards so the GUI can tell the user where the
    report went. It is optional so the headless paths stay usable.
    """
    global _installed, _notifier

    _notifier = notifier
    if _installed:
        return

    handler = _RingBufferHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)

    sys.excepthook = _handle

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        # Worker threads die silently by default; route them through the same path.
        if args.exc_type is SystemExit:
            return
        _handle(
            args.exc_type,
            args.exc_value or args.exc_type(),
            args.exc_traceback,
            thread=getattr(args.thread, "name", None),
        )

    threading.excepthook = thread_hook
    _installed = True


def existing_reports() -> list[Path]:
    """Crash reports already on disk, newest first."""
    destination = crash_dir(create=False)
    if destination is None or not destination.is_dir():
        return []
    reports = [p for p in destination.glob("crash-*.log") if p.is_file()]
    return sorted(reports, key=lambda p: p.stat().st_mtime, reverse=True)
