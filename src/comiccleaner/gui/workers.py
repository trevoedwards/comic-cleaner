"""Background threads for scanning and for applying removals."""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..core.cache import HashCache
from ..core.history import record_run
from ..core.model import ArchiveInfo, DuplicateGroup
from ..core.remover import (
    BackupPolicy,
    RemovalPlan,
    RemovalReport,
    apply_removals,
)
from ..core.scanner import scan_archives
from ..core.signatures import capture_samples, learn_from_run
from ..core.updates import UpdateCheckError, latest_release
from ..crashlog import write_report

log = logging.getLogger(__name__)


def _record(exc: Exception, worker: str) -> None:
    """Save a crash report for a worker failure.

    These are caught so the UI survives, which means they never reach the
    global excepthook - without this they would leave nothing on disk.
    """
    try:
        write_report(type(exc), exc, exc.__traceback__, thread=worker)
    except Exception:
        log.debug("could not write a crash report", exc_info=True)


class UpdateChecker(QObject):
    """Asks GitHub for the latest release without blocking the UI.

    A daemon thread rather than a QThread: a slow network must never hold up
    quitting, and Qt aborts if a QThread is still running when it is destroyed.
    """

    finished = Signal(object, str)  # the Release (or None), and an error message

    def start(self) -> None:
        threading.Thread(target=self._run, name="update-check", daemon=True).start()

    def _run(self) -> None:
        try:
            release, error = latest_release(), ""
        except UpdateCheckError as exc:
            release, error = None, str(exc)
        except Exception as exc:  # never let a surprise escape a background thread
            log.debug("update check failed", exc_info=True)
            release, error = None, str(exc)
        # The window may have closed, and this object with it, while waiting.
        with contextlib.suppress(RuntimeError):
            self.finished.emit(release, error)


class ScanWorker(QThread):
    """Hashes every page of the given archives without blocking the UI."""

    progressed = Signal(int, int, str)
    finished_scan = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        paths: list[Path],
        cache: HashCache | None = None,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._paths = paths
        self._cache = cache
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            results: list[ArchiveInfo] = scan_archives(
                self._paths,
                cache=self._cache,
                progress=lambda done, total, name: self.progressed.emit(done, total, name),
                should_cancel=lambda: self._cancelled,
            )
        except Exception as exc:
            log.exception("scan worker crashed")
            _record(exc, "ScanWorker")
            self.failed.emit(str(exc))
            return
        self.finished_scan.emit(results)


class RemovalWorker(QThread):
    """Rebuilds archives without the marked pages."""

    progressed = Signal(int, int, str)
    finished_removal = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        plans: list[RemovalPlan],
        *,
        backup: BackupPolicy,
        output_dir: Path | None = None,
        dry_run: bool = False,
        compress: bool = False,
        learn: list[DuplicateGroup] | None = None,
        cache: HashCache | None = None,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._plans = plans
        self._backup = backup
        self._output_dir = output_dir
        self._dry_run = dry_run
        self._compress = compress
        # Groups to remember as known junk once they are really gone.
        self._learn = [] if dry_run or cache is None else list(learn or [])
        # Also where the run is recorded, so it can be undone from History.
        self._cache = cache
        self._cancelled = False
        self.learned = 0

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            # Thumbnails first: the pages will not exist once the run is over.
            samples = capture_samples(self._learn) if self._learn else {}
            report: RemovalReport = apply_removals(
                self._plans,
                backup=self._backup,
                output_dir=self._output_dir,
                dry_run=self._dry_run,
                compress=self._compress,
                progress=lambda done, total, name: self.progressed.emit(done, total, name),
                should_cancel=lambda: self._cancelled,
            )
            if self._cache is not None and not self._dry_run:
                record_run(self._cache, self._plans, report, "gui")
            if self._learn and self._cache is not None:
                self.learned = learn_from_run(self._cache, self._learn, report, samples)
        except Exception as exc:
            log.exception("removal worker crashed")
            _record(exc, "RemovalWorker")
            self.failed.emit(str(exc))
            return
        self.finished_removal.emit(report)
