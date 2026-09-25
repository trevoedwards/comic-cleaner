"""Walking archives and producing per-page hashes."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .archive import (
    ARCHIVE_SUFFIXES,
    TEMP_PREFIX,
    WALKED_SUFFIXES,
    ArchiveError,
    ComicArchive,
    detect_kind,
)
from .cache import HashCache
from .hashing import DecodeError, digest_image
from .model import ArchiveInfo, ArchiveKind, PageEntry

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]


# Files that carry an archive extension without being a comic: macOS writes a "._"
# AppleDouble stub beside every file on exFAT/SMB volumes, and an interrupted
# removal can strand its ".comiccleaner-" temp file. Importing either one shows up
# as an unreadable book, or worse, as a second copy of a real book's pages.
_NOT_ARCHIVE_PREFIXES = ("._", TEMP_PREFIX)


def _is_archive_file(path: Path, suffixes: set[str] = ARCHIVE_SUFFIXES) -> bool:
    return (
        path.suffix.lower() in suffixes
        and not path.name.startswith(_NOT_ARCHIVE_PREFIXES)
        and path.is_file()
    )


def find_archives(paths: Iterable[Path], *, recursive: bool = True) -> list[Path]:
    """Expand a mix of dropped files and folders into a sorted archive list.

    Folders yield only comic suffixes (see WALKED_SUFFIXES); a file named
    directly is taken with any archive suffix.
    """
    found: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            walker = path.rglob("*") if recursive else path.glob("*")
            for child in walker:
                if _is_archive_file(child, WALKED_SUFFIXES):
                    found.add(child.resolve())
        elif _is_archive_file(path):
            found.add(path.resolve())
    return sorted(found)


def scan_archive(path: Path, cache: HashCache | None = None) -> ArchiveInfo:
    """Hash every page of one archive, using the cache when it is still valid."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError as exc:
        return ArchiveInfo(
            path=path, kind=ArchiveKind.UNKNOWN, size=0, mtime_ns=0, error=str(exc)
        )

    kind = detect_kind(path)
    info = ArchiveInfo(path=path, kind=kind, size=stat.st_size, mtime_ns=stat.st_mtime_ns)

    if cache is not None:
        cached = cache.get(path, stat.st_size, stat.st_mtime_ns)
        if cached is not None:
            if any(p.error for p in cached):
                cached = _retry_stale_errors(path, cached, cache, stat)
            info.pages = cached
            info.page_count = len(cached)
            return info

    pages: list[PageEntry] = []
    try:
        with ComicArchive(path) as arc:
            for index, name in enumerate(arc.page_names()):
                pages.append(_hash_page(arc, path, name, index))
    except ArchiveError as exc:
        info.error = str(exc)
        return info

    info.pages = pages
    info.page_count = len(pages)
    if cache is not None:
        cache.put(path, stat.st_size, stat.st_mtime_ns, pages)
    return info


def _hash_page(arc: ComicArchive, path: Path, name: str, index: int) -> PageEntry:
    """One page's digest, or a page carrying the reason it could not be read."""
    try:
        data = arc.read(name)
    except (ArchiveError, OSError) as exc:
        return _error_page(path, name, index, str(exc))
    stored = arc.stored_size(name)
    try:
        digest = digest_image(data)
    except DecodeError as exc:
        return _error_page(path, name, index, str(exc), size=stored)
    return PageEntry(
        archive=path,
        name=name,
        index=index,
        size=stored,
        width=digest.width,
        height=digest.height,
        content_sha=digest.content_sha,
        dhash=digest.dhash,
        flat=digest.flat,
    )


def _retry_stale_errors(
    path: Path, cached: list[PageEntry], cache: HashCache, stat: os.stat_result
) -> list[PageEntry]:
    """Decode again only the cached failures a newer Pillow might now read.

    The archive is unchanged (the cache checked its size and time), so every
    other page keeps its cached hash and page positions still line up.
    """
    stale = cache.stale_errors(path)
    if not stale:
        return cached
    try:
        with ComicArchive(path) as arc:
            pages = [
                _hash_page(arc, path, p.name, p.index) if p.name in stale else p
                for p in cached
            ]
    except ArchiveError as exc:
        # Cannot even open it right now; keep what we had and try again next time.
        log.debug("could not retry failed pages of %s: %s", path, exc)
        return cached
    cache.put(path, stat.st_size, stat.st_mtime_ns, pages)
    return pages


def _error_page(
    archive: Path, name: str, index: int, error: str, size: int = 0
) -> PageEntry:
    return PageEntry(
        archive=archive, name=name, index=index, size=size, width=0, height=0,
        content_sha="", dhash=0, flat=False, error=error,
    )


def default_workers() -> int:
    """Image decoding releases the GIL in Pillow, so threads do scale here."""
    return max(2, min(8, (os.cpu_count() or 4)))


def scan_archives(
    paths: Iterable[Path],
    *,
    cache: HashCache | None = None,
    workers: int | None = None,
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[ArchiveInfo]:
    """Scan many archives concurrently, reporting progress as each finishes."""
    targets = list(paths)
    total = len(targets)
    results: list[ArchiveInfo] = []
    if not total:
        return results

    n_workers = workers or default_workers()
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(scan_archive, p, cache): p for p in targets}
        for done, future in enumerate(_as_completed(futures), start=1):
            path = futures[future]
            if should_cancel is not None and should_cancel():
                for pending in futures:
                    pending.cancel()
                break
            try:
                results.append(future.result())
            except Exception as exc:  # a worker blowing up must not kill the scan
                log.exception("scan failed for %s", path)
                results.append(
                    ArchiveInfo(
                        path=path, kind=ArchiveKind.UNKNOWN, size=0, mtime_ns=0,
                        error=f"unexpected error: {exc}",
                    )
                )
            if progress is not None:
                progress(done, total, path.name)
    results.sort(key=lambda a: str(a.path).lower())
    return results


def _as_completed(futures: dict):
    from concurrent.futures import as_completed

    return as_completed(futures)
