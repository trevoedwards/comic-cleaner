"""Applying removals: rebuild each archive without the marked pages."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .archive import ArchiveError, ComicArchive, detect_kind, is_page_name, write_cbz
from .comicinfo import find_comicinfo, update_comicinfo
from .model import ArchiveKind, DuplicateGroup

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]


class RemovalError(RuntimeError):
    pass


@dataclass(slots=True)
class BackupPolicy:
    """Where the original file goes before it is replaced."""

    enabled: bool = True
    # None means alongside the original, as "<name>.bak"; otherwise a mirror folder.
    directory: Path | None = None
    suffix: str = ".bak"


@dataclass(slots=True)
class RemovalPlan:
    """What will happen to one archive."""

    archive: Path
    remove_names: set[str]
    original_pages: int

    @property
    def remaining_pages(self) -> int:
        return self.original_pages - len(self.remove_names)


@dataclass(slots=True)
class RemovalResult:
    archive: Path
    output: Path | None = None
    backup: Path | None = None
    removed: int = 0
    bytes_freed: int = 0
    converted: bool = False
    skipped: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped


@dataclass(slots=True)
class RemovalReport:
    results: list[RemovalResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[RemovalResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[RemovalResult]:
        return [r for r in self.results if r.error]

    @property
    def total_removed(self) -> int:
        return sum(r.removed for r in self.succeeded)

    @property
    def total_freed(self) -> int:
        return sum(r.bytes_freed for r in self.succeeded)


def build_plans(
    groups: Iterable[DuplicateGroup], archive_page_counts: dict[Path, int]
) -> list[RemovalPlan]:
    """Collapse group decisions into one plan per affected archive."""
    per_archive: dict[Path, set[str]] = defaultdict(set)
    for group in groups:
        for page in group.pages_to_remove():
            per_archive[page.archive].add(page.name)

    plans = [
        RemovalPlan(
            archive=archive,
            remove_names=names,
            original_pages=archive_page_counts.get(archive, 0),
        )
        for archive, names in per_archive.items()
    ]
    plans.sort(key=lambda p: str(p.archive).lower())
    return plans


def _backup_path(archive: Path, policy: BackupPolicy) -> Path:
    if policy.directory is not None:
        policy.directory.mkdir(parents=True, exist_ok=True)
        target = policy.directory / (archive.name + policy.suffix)
    else:
        target = archive.with_name(archive.name + policy.suffix)
    candidate = target
    counter = 1
    while candidate.exists():  # never clobber an existing backup
        candidate = target.with_name(f"{target.name}.{counter}")
        counter += 1
    return candidate


def _move_aside(src: Path, dst: Path) -> None:
    """Move `src` to `dst`, preferring an atomic rename.

    shutil.move silently degrades to copy-then-delete when a rename fails, which
    on Windows means a locked file leaves a half-made backup behind and *then*
    errors. os.replace either succeeds or raises, so only a genuine cross-volume
    move takes the copying path.
    """
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        cross_volume = exc.errno == errno.EXDEV or getattr(exc, "winerror", 0) == 17
        if not cross_volume:
            raise
    shutil.copy2(src, dst)
    try:
        src.unlink()
    except OSError:
        dst.unlink(missing_ok=True)  # do not leave an orphaned partial backup
        raise


def _explain(exc: OSError, path: Path) -> str:
    """Turn the common Windows sharing violation into something actionable."""
    if getattr(exc, "winerror", 0) == 32 or exc.errno == errno.EACCES:
        return (
            f"{path.name} is open in another program (or still being read). "
            "Close it and run the removal again."
        )
    return str(exc)


def _verify_cbz(path: Path, expected_pages: int) -> None:
    """Open the rebuilt file and confirm it is sound before touching the original."""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RemovalError(f"rebuilt archive has a corrupt entry: {bad}")
            actual = sum(1 for n in zf.namelist() if is_page_name(n))
    except zipfile.BadZipFile as exc:
        raise RemovalError(f"rebuilt archive is not a valid zip: {exc}") from exc
    if actual != expected_pages:
        raise RemovalError(f"rebuilt archive has {actual} pages, expected {expected_pages}")


def _collect_entries(
    plan: RemovalPlan,
) -> tuple[list[tuple[str, bytes]], set[str], int, int]:
    """Read the archive and return the entries to keep, plus removal stats."""
    with ComicArchive(plan.archive) as arc:
        all_names = arc.entry_names()
        page_names = arc.page_names()
        removing = {n for n in page_names if n in plan.remove_names}

        missing = plan.remove_names - set(page_names)
        if missing:
            raise RemovalError(
                f"archive changed since scan; {len(missing)} marked page(s) not found"
            )
        if not removing:
            return [], set(), 0, len(page_names)
        if len(removing) >= len(page_names):
            raise RemovalError(
                "refusing to remove every page - this would empty the archive"
            )

        removed_indices = {i for i, n in enumerate(page_names) if n in removing}
        freed = sum(arc.stored_size(n) for n in removing)
        comicinfo_name = find_comicinfo(all_names)
        remaining = len(page_names) - len(removing)

        entries: list[tuple[str, bytes]] = []
        for name in all_names:
            if name in removing:
                continue
            data = arc.read(name)
            if comicinfo_name is not None and name == comicinfo_name:
                data = update_comicinfo(data, removed_indices, remaining)
            entries.append((name, data))

    return entries, removing, freed, len(page_names)


def apply_plan(
    plan: RemovalPlan,
    *,
    backup: BackupPolicy | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
    compress: bool = False,
) -> RemovalResult:
    """Rebuild one archive without its marked pages.

    The new file is written to a temp path and verified before the original is
    moved aside, so an interrupted or failed run never destroys the source.
    """
    policy = backup or BackupPolicy()
    result = RemovalResult(archive=plan.archive)

    if not plan.remove_names:
        result.skipped = True
        return result

    try:
        entries, removing, freed, total_pages = _collect_entries(plan)
    except (ArchiveError, RemovalError) as exc:
        result.error = str(exc)
        return result
    except OSError as exc:
        result.error = f"read failed: {exc}"
        return result

    if not removing:
        result.skipped = True
        return result

    result.removed = len(removing)
    result.bytes_freed = freed
    expected_pages = total_pages - len(removing)

    # cbr/cb7 cannot be written back; they are rebuilt as a sibling .cbz.
    result.converted = detect_kind(plan.archive) in (
        ArchiveKind.RAR,
        ArchiveKind.SEVENZIP,
    )
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / plan.archive.name
    else:
        destination = plan.archive
    if result.converted:
        destination = destination.with_suffix(".cbz")
    result.output = destination

    if dry_run:
        return result

    replacing_in_place = output_dir is None
    if not replacing_in_place and destination.exists():
        result.error = f"refusing to overwrite existing file: {destination.name}"
        result.removed = 0
        result.bytes_freed = 0
        return result

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(destination.parent), prefix=".comicdedupe-", suffix=".cbz"
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        write_cbz(tmp_path, entries, compress=compress)
        _verify_cbz(tmp_path, expected_pages)

        if replacing_in_place and plan.archive.exists():
            if policy.enabled:
                backup_target = _backup_path(plan.archive, policy)
                _move_aside(plan.archive, backup_target)
                result.backup = backup_target
            elif result.converted:
                # Converting cbr to cbz with no backup: the source must still go.
                plan.archive.unlink()

        os.replace(tmp_path, destination)
    except (RemovalError, OSError, zipfile.BadZipFile) as exc:
        tmp_path.unlink(missing_ok=True)
        # Restore the original if it was already moved aside.
        if result.backup is not None and not plan.archive.exists():
            _move_aside(result.backup, plan.archive)
            result.backup = None
        result.error = (
            _explain(exc, plan.archive) if isinstance(exc, OSError) else str(exc)
        )
        result.removed = 0
        result.bytes_freed = 0
    return result


def apply_removals(
    plans: Iterable[RemovalPlan],
    *,
    backup: BackupPolicy | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
    compress: bool = False,
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> RemovalReport:
    """Apply every plan in turn. Disk-bound, so there is no thread pool here."""
    todo = list(plans)
    report = RemovalReport()
    for done, plan in enumerate(todo, start=1):
        if should_cancel is not None and should_cancel():
            break
        report.results.append(
            apply_plan(
                plan,
                backup=backup,
                output_dir=output_dir,
                dry_run=dry_run,
                compress=compress,
            )
        )
        if progress is not None:
            progress(done, len(todo), plan.archive.name)
    return report
