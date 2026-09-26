"""A record of every removal run, and putting books back from their backups."""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .cache import HashCache
from .remover import RemovalPlan, RemovalReport, _explain, _move_aside

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunItem:
    """What one run did to one book."""

    id: int
    run_id: int
    archive: Path          # the book as it was
    output: Path           # where the cleaned book went (the same path, if in place)
    backup: Path | None    # the untouched original, if one was kept
    removed: int
    pages: list[str]       # entry names removed
    bytes_freed: int
    converted: bool        # a cbr/cb7 rebuilt as cbz
    output_size: int | None
    output_mtime_ns: int | None
    restored_at: float | None

    def status(self) -> tuple[bool, str]:
        """Whether this book can be put back, and in a few words why (not)."""
        if self.restored_at:
            return False, f"restored {when(self.restored_at)}"
        if self.backup is None:
            if self.output != self.archive and self.archive.exists():
                return False, "the original was never changed"
            return False, "no backup was kept"
        if not self.backup.exists():
            return False, "the backup has since been deleted"
        if self.output.exists() and not self._output_unchanged():
            # Put the backup back now and whatever changed it since would be lost.
            return False, "changed since the run"
        if self.converted and self.archive.exists():
            return False, f"{self.archive.name} already exists"
        return True, "can be restored"

    def _output_unchanged(self) -> bool:
        if self.output_size is None or self.output_mtime_ns is None:
            return True
        try:
            stat = self.output.stat()
        except OSError:
            return False
        return stat.st_size == self.output_size and stat.st_mtime_ns == self.output_mtime_ns

    @property
    def restorable(self) -> bool:
        return self.status()[0]


@dataclass(slots=True)
class Run:
    id: int
    started_at: float
    source: str  # "gui" or "cli"
    items: list[RunItem] = field(default_factory=list)
    # Its backups are kept through Clean Up Backups until it is unpinned.
    pinned: bool = False

    @property
    def backups(self) -> list[Path]:
        """The backups this run made that are still on disk."""
        return [i.backup for i in self.items if i.backup is not None and i.backup.exists()]

    @property
    def removed(self) -> int:
        return sum(i.removed for i in self.items)

    @property
    def bytes_freed(self) -> int:
        return sum(i.bytes_freed for i in self.items)


def when(stamp: float) -> str:
    return dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def record_run(
    cache: HashCache, plans: Iterable[RemovalPlan], report: RemovalReport, source: str
) -> int | None:
    """Store what a finished (not dry) run changed. None if it changed nothing."""
    by_archive = {plan.archive: plan for plan in plans}
    items = []
    for result in report.succeeded:
        if not result.removed or result.output is None:
            continue
        try:
            stat = result.output.stat()
            size, mtime = stat.st_size, stat.st_mtime_ns
        except OSError:
            size = mtime = None
        plan = by_archive.get(result.archive)
        names = sorted(plan.remove_names) if plan is not None else []
        items.append((
            str(result.archive), str(result.output),
            str(result.backup) if result.backup else None,
            result.removed, json.dumps(names), result.bytes_freed, int(result.converted),
            size, mtime,
        ))
    if not items:
        return None
    return cache.add_run(source, items)


def load_history(cache: HashCache) -> list[Run]:
    runs = {
        rid: Run(id=rid, started_at=float(at), source=src, pinned=bool(pinned))
        for rid, at, src, pinned in cache.run_rows()
    }
    for row in cache.run_item_rows():
        (item_id, run_id, archive, output, backup, removed, pages, freed,
         converted, size, mtime, restored) = row
        run = runs.get(run_id)
        if run is None:
            continue
        try:
            names = json.loads(pages)
        except ValueError:
            names = []
        run.items.append(RunItem(
            id=item_id, run_id=run_id, archive=Path(archive), output=Path(output),
            backup=Path(backup) if backup else None, removed=removed, pages=names,
            bytes_freed=freed, converted=bool(converted), output_size=size,
            output_mtime_ns=mtime, restored_at=restored,
        ))
    return list(runs.values())


def restore(cache: HashCache, item: RunItem) -> str | None:
    """Put a book back as it was before the run. Returns an error, or None.

    The backup goes back over the cleaned book, which is discarded. For a cbr
    that was rebuilt as a cbz, the cbr returns and the cbz is removed, but only
    once the cbr is safely back.
    """
    ok, reason = item.status()
    if not ok:
        return reason
    assert item.backup is not None
    try:
        _move_aside(item.backup, item.archive)
        if item.converted and item.output != item.archive:
            item.output.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("restore failed for %s: %s", item.archive, exc)
        return _explain(exc, item.archive)
    cache.mark_restored(item.id)
    cache.invalidate(item.archive)
    if item.output != item.archive:
        cache.invalidate(item.output)
    return None


def pinned_backups(runs: Iterable[Run]) -> set[Path]:
    """Backups that belong to a pinned run, which a cleanup must leave alone."""
    return {
        item.backup.resolve()
        for run in runs if run.pinned
        for item in run.items if item.backup is not None
    }


@dataclass(slots=True)
class DeletedBackups:
    deleted: int = 0
    freed: int = 0
    errors: list[str] = field(default_factory=list)


def delete_run_backups(run: Run) -> DeletedBackups:
    """Delete the backups one run made, and nothing else. Refuses a pinned run.

    Its books can no longer be restored afterwards; History says so.
    """
    if run.pinned:
        raise ValueError(f"run {run.id} is pinned; unpin it to delete its backups")
    outcome = DeletedBackups()
    for backup in run.backups:
        try:
            size = backup.stat().st_size
            backup.unlink()
        except OSError as exc:
            log.warning("could not delete backup %s: %s", backup, exc)
            outcome.errors.append(f"{backup.name}: {_explain(exc, backup)}")
            continue
        outcome.deleted += 1
        outcome.freed += size
    return outcome


CSV_COLUMNS = [
    "run", "when", "source", "book", "cleaned_file", "backup", "pages_removed",
    "removed_entries", "bytes_freed", "converted", "status",
]


def export_csv(runs: Iterable[Run], path: Path) -> int:
    """One row per book per run, for a spreadsheet. Returns the rows written."""
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for run in runs:
            for item in run.items:
                writer.writerow([
                    run.id, when(run.started_at), run.source, item.archive, item.output,
                    item.backup or "", item.removed, "; ".join(item.pages),
                    item.bytes_freed, "yes" if item.converted else "no", item.status()[1],
                ])
                rows += 1
    return rows
