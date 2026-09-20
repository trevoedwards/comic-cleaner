"""Lazy, cached thumbnail loading for the review grid."""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QIcon, QPixmap

from ..core.archive import ArchiveError, ComicArchive
from ..core.hashing import make_thumbnail
from ..core.model import PageEntry

log = logging.getLogger(__name__)

THUMB_SIZE = 180

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
    ready = Signal(str, QPixmap)
    failed = Signal(str, str)


class _ThumbJob(QRunnable):
    def __init__(self, pool: ArchivePool, key: str, page: PageEntry) -> None:
        super().__init__()
        self.signals = _ThumbSignals()
        self._pool = pool
        self._key = key
        self._page = page

    def run(self) -> None:  # executed on a pool thread
        try:
            data = self._pool.read(self._page.archive, self._page.name)
            png = make_thumbnail(data, THUMB_SIZE)
        except (ArchiveError, OSError, ValueError) as exc:
            self.signals.failed.emit(self._key, str(exc))
            return
        except Exception as exc:  # Pillow can raise almost anything on bad scans
            self.signals.failed.emit(self._key, str(exc))
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(png, "PNG"):
            self.signals.failed.emit(self._key, "could not decode thumbnail")
            return
        self.signals.ready.emit(self._key, pixmap)


class ThumbnailCache(QObject):
    """Requests thumbnails off the UI thread and caches the results."""

    ready = Signal(str, QPixmap)

    def __init__(self, parent: QObject | None = None, capacity: int = 600) -> None:
        super().__init__(parent)
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._pending: set[str] = set()
        self._capacity = capacity
        self._pool = ArchivePool()
        # Archive reads are I/O bound and the pool is not reentrant-safe, so a
        # single worker thread keeps ordering predictable and memory flat.
        self._threads = QThreadPool(self)
        self._threads.setMaxThreadCount(1)
        self._placeholder = _make_placeholder()

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
            job = _ThumbJob(self._pool, key, page)
            job.signals.ready.connect(self._on_ready)
            job.signals.failed.connect(self._on_failed)
            self._threads.start(job)
        return self._placeholder

    def icon(self, page: PageEntry) -> QIcon:
        return QIcon(self.get(page))

    def _on_ready(self, key: str, pixmap: QPixmap) -> None:
        self._pending.discard(key)
        self._cache[key] = pixmap
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        self.ready.emit(key, pixmap)

    def _on_failed(self, key: str, message: str) -> None:
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
        self._pool.close_all()

    def invalidate(self, archive: Path) -> None:
        """Forget thumbnails for an archive whose contents just changed."""
        prefix = f"{archive}|"
        for key in [k for k in self._cache if k.startswith(prefix)]:
            del self._cache[key]

    def shutdown(self) -> None:
        self.release_archives()


def _make_placeholder() -> QPixmap:
    pixmap = QPixmap(THUMB_SIZE, THUMB_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    return pixmap
