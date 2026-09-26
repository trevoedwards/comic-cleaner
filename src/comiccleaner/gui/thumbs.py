"""Lazy, cached thumbnail loading for the review grid."""

from __future__ import annotations

import contextlib
import io
import logging
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QIcon, QPixmap

from ..core.archive import ArchiveError, ComicArchive
from ..core.hashing import make_thumbnail
from ..core.model import PageEntry

log = logging.getLogger(__name__)

THUMB_SIZE = 180

# The sizes Settings offers, as (label, pixels); Medium is THUMB_SIZE.
THUMB_SIZES = (("Small", 120), ("Medium", THUMB_SIZE), ("Large", 240))

# Opening a cbr extracts the whole archive to a temp folder, so keeping a few
# handles alive makes browsing a group of pages from one book far cheaper.
_MAX_OPEN_ARCHIVES = 4


class ArchivePool:
    """Small LRU of open archives, shared by the thumbnail workers."""

    def __init__(self, limit: int = _MAX_OPEN_ARCHIVES) -> None:
        self._open: OrderedDict[Path, ComicArchive] = OrderedDict()
        self._limit = limit

    def read(self, archive: Path, name: str) -> bytes:
        handle = self._open.get(archive)
        if handle is None:
            handle = ComicArchive(archive)
            handle.open()
            self._open[archive] = handle
            while len(self._open) > self._limit:
                _, evicted = self._open.popitem(last=False)
                evicted.close()
        else:
            self._open.move_to_end(archive)
        return handle.read(name)

    def close_all(self) -> None:
        for handle in self._open.values():
            handle.close()
        self._open.clear()


class _ThumbSignals(QObject):
    # PNG bytes, not a QPixmap: pixmaps may only be made on the UI thread. The
    # size it was made at comes along, so one made before a resize is dropped.
    ready = Signal(str, bytes, int)
    failed = Signal(str, str)


class _ThumbJob(QRunnable):
    def __init__(
        self, pool: ArchivePool, key: str, page: PageEntry, parent: QObject, size: int
    ) -> None:
        super().__init__()
        # Owned by the cache, which disposes of it once the answer is in: a runnable
        # cannot own a QObject, and an unparented one would outlive every job.
        self.signals = _ThumbSignals(parent)
        self._pool = pool
        self._key = key
        self._page = page
        self._size = size

    def run(self) -> None:  # executed on a pool thread
        try:
            data = self._pool.read(self._page.archive, self._page.name)
            png = make_thumbnail(data, self._size)
        except (ArchiveError, OSError, ValueError) as exc:
            self.signals.failed.emit(self._key, str(exc))
            return
        except Exception as exc:  # Pillow can raise almost anything on bad scans
            self.signals.failed.emit(self._key, str(exc))
            return
        self.signals.ready.emit(self._key, png, self._size)


class _PageSignals(QObject):
    # key, the decoded RGB image (or None), and an error message when it failed.
    done = Signal(str, object, str)


class _PageJob(QRunnable):
    """Decodes one page at full size for the preview window."""

    def __init__(self, pool: ArchivePool, key: str, page: PageEntry, parent: QObject) -> None:
        super().__init__()
        self.signals = _PageSignals(parent)
        self._pool = pool
        self._key = key
        self._page = page

    def run(self) -> None:  # executed on a pool thread
        try:
            data = self._pool.read(self._page.archive, self._page.name)
            image = Image.open(io.BytesIO(data))
            image.load()
            # A PIL image rather than a QPixmap: pixmaps belong to the UI thread.
            self.signals.done.emit(self._key, image.convert("RGB"), "")
        except Exception as exc:  # Pillow can raise almost anything on bad scans
            self.signals.done.emit(self._key, None, str(exc))


class _TaskSignals(QObject):
    # key, whatever the task returned (or None), and an error message when it failed.
    done = Signal(str, object, str)


class _TaskJob(QRunnable):
    """Runs a pure-Python function (no Qt objects) on the thumbnail thread."""

    def __init__(self, key: str, fn: Callable[[], object], parent: QObject) -> None:
        super().__init__()
        self.signals = _TaskSignals(parent)
        self._key = key
        self._fn = fn

    def run(self) -> None:  # executed on a pool thread
        try:
            result = self._fn()
        except Exception as exc:
            self.signals.done.emit(self._key, None, str(exc))
            return
        self.signals.done.emit(self._key, result, "")


def _dispose(signals: QObject | None) -> None:
    """Cut a finished job's signals loose and let Qt delete them."""
    if signals is None:
        return
    for name in ("ready", "failed", "done"):
        signal = getattr(signals, name, None)
        if signal is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect()
    signals.deleteLater()


class ThumbnailCache(QObject):
    """Requests thumbnails off the UI thread and caches the results."""

    ready = Signal(str, QPixmap)
    page_loaded = Signal(str, object, str)
    task_done = Signal(str, object, str)

    def __init__(
        self, parent: QObject | None = None, capacity: int = 600, size: int = THUMB_SIZE
    ) -> None:
        super().__init__(parent)
        self.size = size
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._pending: set[str] = set()
        # The signals object of every job still queued or running, by key, so it
        # can be disposed of when its answer arrives or the queue is dropped.
        self._jobs: dict[str, QObject] = {}
        self._page_jobs: dict[str, QObject] = {}
        self._task_jobs: dict[str, QObject] = {}
        self._capacity = capacity
        self._pool = ArchivePool()
        # Archive reads are I/O bound and the pool is not reentrant-safe, so a
        # single worker thread keeps ordering predictable and memory flat.
        self._threads = QThreadPool(self)
        self._threads.setMaxThreadCount(1)
        self._placeholder = _make_placeholder(size)

    def set_size(self, size: int) -> None:
        """Make thumbnails at a new size from now on, forgetting every old one."""
        if size == self.size:
            return
        self.release_archives()  # drops queued jobs, which would answer at the old size
        self.size = size
        self._cache.clear()
        self._placeholder = _make_placeholder(size)

    @staticmethod
    def key_for(page: PageEntry) -> str:
        return f"{page.archive}|{page.name}"

    def get(self, page: PageEntry) -> QPixmap:
        """Return a cached thumbnail, or a placeholder while one is generated."""
        key = self.key_for(page)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if key not in self._pending:
            self._pending.add(key)
            job = _ThumbJob(self._pool, key, page, self, self.size)
            job.signals.ready.connect(self._on_ready)
            job.signals.failed.connect(self._on_failed)
            self._jobs[key] = job.signals
            self._threads.start(job)
        return self._placeholder

    def icon(self, page: PageEntry) -> QIcon:
        return QIcon(self.get(page))

    def request_page(self, page: PageEntry) -> None:
        """Decode a page at full size; the answer arrives through page_loaded.

        Shares the thumbnail thread and its open archives, so a cbr that was just
        extracted for the grid is not extracted all over again. A page already
        queued is not queued twice; its one answer reaches every listener.
        """
        key = self.key_for(page)
        if key in self._page_jobs:
            return
        job = _PageJob(self._pool, key, page, self)
        job.signals.done.connect(self._on_page_loaded)
        self._page_jobs[key] = job.signals
        self._threads.start(job)

    def run_task(self, key: str, fn: Callable[[], object]) -> None:
        """Run `fn` on the thumbnail thread; its result arrives through task_done.

        `fn` must not touch Qt objects. Queued behind the page loads, so work that
        needs a page (a difference image, say) runs after it has been decoded.
        """
        if key in self._task_jobs:
            return
        job = _TaskJob(key, fn, self)
        job.signals.done.connect(self._on_task_done)
        self._task_jobs[key] = job.signals
        self._threads.start(job)

    def _on_task_done(self, key: str, result: object, error: str) -> None:
        _dispose(self._task_jobs.pop(key, None))
        if error:
            log.debug("background task failed for %s: %s", key, error)
        self.task_done.emit(key, result, error)

    def _on_page_loaded(self, key: str, image: object, error: str) -> None:
        _dispose(self._page_jobs.pop(key, None))
        if error:
            log.debug("page load failed for %s: %s", key, error)
        self.page_loaded.emit(key, image, error)

    def _on_ready(self, key: str, png: bytes, size: int) -> None:
        if size != self.size:
            return  # queued before set_size; the job at the new size answers instead
        # Runs on the UI thread, the only place a QPixmap may be created.
        pixmap = QPixmap()
        if not pixmap.loadFromData(png):  # Qt recognises the PNG by itself
            self._on_failed(key, "could not decode thumbnail")
            return
        _dispose(self._jobs.pop(key, None))
        self._pending.discard(key)
        self._cache[key] = pixmap
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        self.ready.emit(key, pixmap)

    def _on_failed(self, key: str, message: str) -> None:
        _dispose(self._jobs.pop(key, None))
        self._pending.discard(key)
        log.debug("thumbnail failed for %s: %s", key, message)
        self._cache[key] = self._placeholder
        self.ready.emit(key, self._placeholder)

    def release_archives(self) -> None:
        """Drop every open archive handle, keeping the thumbnails already made.

        Windows refuses to rename or delete a file that is still open, so this
        must run before any archive is rewritten.
        """
        self._threads.clear()
        self._threads.waitForDone(5000)
        self._pending.clear()
        # Jobs dropped from the queue never answer, so their signals go now. An
        # answer already emitted is still delivered, and handled as usual.
        for jobs in (self._jobs, self._page_jobs, self._task_jobs):
            for signals in jobs.values():
                _dispose(signals)
            jobs.clear()
        self._pool.close_all()

    def invalidate(self, archive: Path) -> None:
        """Forget thumbnails for an archive whose contents just changed."""
        prefix = f"{archive}|"
        for key in [k for k in self._cache if k.startswith(prefix)]:
            del self._cache[key]

    def shutdown(self) -> None:
        self.release_archives()


def _make_placeholder(size: int = THUMB_SIZE) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    return pixmap
