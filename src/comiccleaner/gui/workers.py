"""Background threads for scanning and for applying removals."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core.cache import HashCache
from ..core.model import ArchiveInfo
from ..core.remover import (
    BackupPolicy,
    RemovalPlan,
    RemovalReport,
    apply_removals,
)
from ..core.scanner import scan_archives
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
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._plans = plans
        self._backup = backup
        self._output_dir = output_dir
        self._dry_run = dry_run
        self._compress = compress
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            report: RemovalReport = apply_removals(
                self._plans,
                backup=self._backup,
                output_dir=self._output_dir,
                dry_run=self._dry_run,
                compress=self._compress,
                progress=lambda done, total, name: self.progressed.emit(done, total, name),
                should_cancel=lambda: self._cancelled,
            )
        except Exception as exc:
            log.exception("removal worker crashed")
            _record(exc, "RemovalWorker")
            self.failed.emit(str(exc))
            return
        self.finished_removal.emit(report)
