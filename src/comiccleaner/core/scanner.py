"""Walking archives and producing per-page hashes."""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
from .comicinfo import find_comicinfo, read_metadata
from .hashing import DecodeError, digest_image
from .model import ArchiveInfo, ArchiveKind, PageEntry

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]

# Not a comic archive, and never will be: said so plainly rather than skipped.
PDF_SUFFIX = ".pdf"

# Archive suffixes a folder walk leaves alone (see WALKED_SUFFIXES).
_UNWALKED_SUFFIXES = ARCHIVE_SUFFIXES - WALKED_SUFFIXES


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


@dataclass(slots=True)
class Survey:
    """What expanding a drop found, and what it left out and why."""

    found: list[Path] = field(default_factory=list)
    pdfs: int = 0        # PDF files, which are not supported at all
    unwalked: int = 0    # plain .rar/.7z in a folder; see WALKED_SUFFIXES
    excluded: int = 0    # matched an exclude pattern
    cancelled: bool = False


def is_excluded(path: Path, root: Path | None, patterns: Sequence[str]) -> bool:
    """Whether `path` matches any glob, by name or by its path under `root`.

    Matching follows the platform: case-insensitive on Windows. Relative paths
    use forward slashes, so "Scans/*" means the same on every system.
    """
    if not patterns:
        return False
    candidates = [path.name]
    if root is not None:
        with contextlib.suppress(ValueError):
            candidates.append(path.relative_to(root).as_posix())
    return any(
        fnmatch.fnmatch(candidate, pattern)
        for pattern in patterns
        for candidate in candidates
    )


def survey(
    paths: Iterable[Path],
    *,
    recursive: bool = True,
    exclude: Sequence[str] = (),
    progress: Callable[[Path], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Survey:
    """Expand a mix of dropped files and folders into a sorted archive list.

    Folders yield only comic suffixes (see WALKED_SUFFIXES); a file named
    directly is taken with any archive suffix. `exclude` globs drop a file whose
    name, or path relative to the folder it was found under, matches one.
    `progress` is told each folder as the walk enters it; once `should_cancel`
    says so, the walk stops and whatever was found so far is returned.
    """
    patterns = [p.strip() for p in exclude if p.strip()]
    result = Survey()
    found: set[Path] = set()
    for raw in paths:
        if result.cancelled:
            break
        path = Path(raw)
        if path.is_dir():
            result.cancelled = _walk(
                path, recursive, patterns, found, result, progress, should_cancel
            )
        elif path.suffix.lower() == PDF_SUFFIX:
            result.pdfs += 1
        elif _is_archive_file(path):
            if is_excluded(path, None, patterns):
                result.excluded += 1
            else:
                found.add(path.resolve())
    result.found = sorted(found)
    return result


def _walk(
    root: Path,
    recursive: bool,
    patterns: list[str],
    found: set[Path],
    result: Survey,
    progress: Callable[[Path], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> bool:
    """Add the archives under one folder to `found`. True if cancelled."""
    for folder, dirs, files in os.walk(root, followlinks=False):
        if should_cancel is not None and should_cancel():
            return True
        here = Path(folder)
        if progress is not None:
            progress(here)
        if not recursive:
            dirs[:] = []
        for name in files:
            child = here / name
            suffix = child.suffix.lower()
            if suffix == PDF_SUFFIX:
                result.pdfs += 1
            elif _is_archive_file(child, WALKED_SUFFIXES):
                if is_excluded(child, root, patterns):
                    result.excluded += 1
                else:
                    found.add(child.resolve())
            elif suffix in _UNWALKED_SUFFIXES and child.is_file():
                result.unwalked += 1
    return False


def find_archives(
    paths: Iterable[Path], *, recursive: bool = True, exclude: Sequence[str] = ()
) -> list[Path]:
    """The archives in a mix of files and folders; see survey()."""
    return survey(paths, recursive=recursive, exclude=exclude).found


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
            info.cached = True
            meta = cache.metadata(path)
            if meta is None and kind is ArchiveKind.ZIP:
                # Hashed before metadata was kept. A zip gives it up cheaply; a RAR
                # would mean extracting the whole book, so that waits for a change.
                meta = _read_metadata_only(path)
                cache.set_metadata(path, meta)
            _apply_metadata(info, meta or {})
            return info

    pages: list[PageEntry] = []
    found: dict[str, str] = {}
    try:
        with ComicArchive(path) as arc:
            for index, name in enumerate(arc.page_names()):
                pages.append(_hash_page(arc, path, name, index))
            found = _metadata(arc)
    except ArchiveError as exc:
        info.error = str(exc)
        return info

    info.pages = pages
    info.page_count = len(pages)
    _apply_metadata(info, found)
    if cache is not None:
        cache.put(path, stat.st_size, stat.st_mtime_ns, pages)
        cache.set_metadata(path, found)
    return info


def _metadata(arc: ComicArchive) -> dict[str, str]:
    """Series, number and title from the book's ComicInfo.xml, if it has a sound one."""
    try:
        name = find_comicinfo(arc.entry_names())
        return read_metadata(arc.read(name)) if name is not None else {}
    except (ArchiveError, OSError) as exc:
        log.debug("could not read ComicInfo.xml from %s: %s", arc.path, exc)
        return {}


def _read_metadata_only(path: Path) -> dict[str, str]:
    try:
        with ComicArchive(path) as arc:
            return _metadata(arc)
    except ArchiveError as exc:
        log.debug("could not open %s for its ComicInfo.xml: %s", path, exc)
        return {}


def _apply_metadata(info: ArchiveInfo, meta: Mapping[str, str]) -> None:
    info.series = meta.get("series", "")
    info.number = meta.get("number", "")
    info.title = meta.get("title", "")


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


class ScanStats:
    """Running totals for a scan, and a rough time left.

    Books that come straight from the cache take no time, so the estimate is
    built from the uncached books alone. It goes by bytes where the sizes are
    known: a trade paperback can be ten times an issue, and the small books
    finish first, so counting books would call the end near while the biggest
    ones are still being read. Without sizes it falls back to counting books.
    """

    def __init__(
        self,
        total: int,
        total_bytes: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.total = total
        self.total_bytes = total_bytes
        self.done = 0
        self.bytes_done = 0
        self.pages_hashed = 0
        self.pages_cached = 0
        self.uncached_books = 0
        self.uncached_bytes = 0
        self._clock = clock
        self._started = clock()

    def record(self, info: ArchiveInfo) -> None:
        self.done += 1
        self.bytes_done += info.size
        if info.cached:
            self.pages_cached += info.page_count
        else:
            self.uncached_books += 1
            self.uncached_bytes += info.size
            self.pages_hashed += info.page_count

    def eta(self) -> float | None:
        """Seconds left, or None until there is something to go on."""
        remaining = self.total - self.done
        if remaining <= 0:
            return 0.0
        if not self.uncached_books:
            return None
        elapsed = self._clock() - self._started
        if self.total_bytes and self.uncached_bytes:
            left = max(self.total_bytes - self.bytes_done, 0)
            # Of what remains, the share likely to need hashing, going by so far.
            still_to_hash = left * self.uncached_bytes / max(self.bytes_done, 1)
            return elapsed * still_to_hash / self.uncached_bytes
        per_book = elapsed / self.uncached_books
        return per_book * remaining * self.uncached_books / self.done

    def describe(self) -> str:
        """For example "120 pages hashed, 3,400 from cache, about 2 min left"."""
        parts = [f"{self.pages_hashed:,} pages hashed", f"{self.pages_cached:,} from cache"]
        eta = self.eta()
        if eta:
            parts.append(f"about {_duration(eta)} left")
        return ", ".join(parts)


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{max(1, round(seconds))} s"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def scan_archives(
    paths: Iterable[Path],
    *,
    cache: HashCache | None = None,
    workers: int | None = None,
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_archive: Callable[[ArchiveInfo], None] | None = None,
) -> list[ArchiveInfo]:
    """Scan many archives concurrently, reporting progress as each finishes.

    `on_archive` sees each result as it arrives, just before `progress` does.
    """
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
                info = future.result()
            except Exception as exc:  # a worker blowing up must not kill the scan
                log.exception("scan failed for %s", path)
                info = ArchiveInfo(
                    path=path, kind=ArchiveKind.UNKNOWN, size=0, mtime_ns=0,
                    error=f"unexpected error: {exc}",
                )
            results.append(info)
            if on_archive is not None:
                on_archive(info)
            if progress is not None:
                progress(done, total, path.name)
    results.sort(key=lambda a: str(a.path).lower())
    return results


def _as_completed(futures: dict):
    from concurrent.futures import as_completed

    return as_completed(futures)


@dataclass(slots=True)
class Relocation:
    """Where books missing from their old place turned up in a chosen folder."""

    found: dict[Path, Path] = field(default_factory=dict)
    ambiguous: list[Path] = field(default_factory=list)  # several candidates
    not_found: list[Path] = field(default_factory=list)  # none, or nothing to go on


def find_relocated(
    missing: Iterable[Path],
    folder: Path,
    identities: Mapping[Path, tuple[int, int]],
) -> Relocation:
    """Look under `folder` for each missing book, by name, size and mtime_ns.

    The same rule the hash cache uses to follow a moved book: moves and copies
    keep all three. Nothing is hashed. A book with no known identity, or with
    more than one matching file, is not relocated.
    """
    wanted = {Path(p): identities.get(Path(p)) for p in missing}
    by_name: dict[str, list[Path]] = {}
    for root, _dirs, files in os.walk(folder, followlinks=False):
        for name in files:
            by_name.setdefault(name, []).append(Path(root) / name)

    result = Relocation()
    for old, identity in wanted.items():
        if identity is None:
            result.not_found.append(old)
            continue
        matches = []
        for candidate in by_name.get(old.name, []):
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if (stat.st_size, stat.st_mtime_ns) == identity:
                matches.append(candidate.resolve())
        if len(matches) == 1:
            result.found[old] = matches[0]
        elif matches:
            result.ambiguous.append(old)
        else:
            result.not_found.append(old)
    return result
