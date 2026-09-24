"""Recording removal runs and putting books back from their backups."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from comiccleaner import cli
from comiccleaner.core.archive import is_page_name
from comiccleaner.core.cache import HashCache
from comiccleaner.core.grouping import GroupingOptions, build_groups
from comiccleaner.core.history import (
    CSV_COLUMNS,
    RunItem,
    export_csv,
    load_history,
    record_run,
    restore,
)
from comiccleaner.core.model import Decision
from comiccleaner.core.remover import (
    BackupPolicy,
    RemovalReport,
    apply_removals,
    build_plans,
)
from comiccleaner.core.scanner import find_archives, scan_archives


@pytest.fixture
def cache(tmp_path: Path):
    store = HashCache(tmp_path / "cache.sqlite")
    yield store
    store.close()


def _pages(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(1 for n in zf.namelist() if is_page_name(n))


def _clean(library: Path, cache: HashCache, **kwargs):
    archives = scan_archives(find_archives([library]))
    groups = build_groups(archives, GroupingOptions())  # the two identical copies
    for group in groups:
        group.decision = Decision.DELETE
    plans = build_plans(groups, {a.path: a.page_count for a in archives})
    report = apply_removals(plans, **kwargs)
    return record_run(cache, plans, report, "cli"), report


def _item(cache: HashCache) -> RunItem:
    [run] = load_history(cache)
    return run.items[0]


# -- recording -------------------------------------------------------------


def test_a_run_is_recorded_book_by_book(library, cache):
    run_id, _ = _clean(library, cache)

    [run] = load_history(cache)
    assert run.id == run_id
    assert run.source == "cli"
    assert run.removed == 2
    assert sorted(i.archive.name for i in run.items) == ["Book 01.cbz", "Book 02.cbz"]
    item = run.items[0]
    assert item.pages == ["page002.jpg"]
    assert item.backup is not None and item.backup.name.endswith(".cbz.bak")
    assert item.status() == (True, "can be restored")


def test_a_run_that_changed_nothing_is_not_recorded(cache):
    assert record_run(cache, [], RemovalReport(), "gui") is None
    assert load_history(cache) == []


# -- restoring -------------------------------------------------------------


def test_restoring_puts_the_original_back(library, cache):
    _clean(library, cache)
    item = _item(cache)
    assert _pages(item.archive) == 4

    assert restore(cache, item) is None

    assert _pages(item.archive) == 5
    assert not item.backup.exists()
    ok, reason = _item(cache).status()
    assert not ok and reason.startswith("restored")


def test_a_restored_book_cannot_be_restored_twice(library, cache):
    _clean(library, cache)
    restore(cache, _item(cache))

    assert restore(cache, _item(cache)).startswith("restored")


def test_nothing_to_restore_without_a_backup(library, cache):
    _clean(library, cache, backup=BackupPolicy(enabled=False))

    assert _item(cache).status() == (False, "no backup was kept")


def test_cleaned_copies_elsewhere_leave_nothing_to_restore(library, cache, tmp_path):
    _clean(library, cache, output_dir=tmp_path / "cleaned")

    assert _item(cache).status() == (False, "the original was never changed")


def test_a_deleted_backup_is_reported(library, cache):
    _clean(library, cache)
    item = _item(cache)
    item.backup.unlink()

    assert item.status() == (False, "the backup has since been deleted")


def test_a_book_changed_since_the_run_is_left_alone(library, cache):
    """Restoring over it would throw away whatever changed it."""
    _clean(library, cache)
    item = _item(cache)
    with zipfile.ZipFile(item.archive, "a") as zf:
        zf.writestr("extra.txt", "edited since")
    before = item.archive.read_bytes()

    assert restore(cache, item) == "changed since the run"
    assert item.archive.read_bytes() == before
    assert item.backup.exists()


def _converted(tmp_path: Path, cache: HashCache) -> RunItem:
    """As a cbr rebuilt as a cbz leaves things: the cbr moved to .bak, a new cbz."""
    folder = tmp_path / "conv"
    folder.mkdir()
    backup = folder / "Book.cbr.bak"
    backup.write_bytes(b"Rar!original")
    output = folder / "Book.cbz"
    output.write_bytes(b"PKcleaned")
    stat = output.stat()
    cache.add_run("gui", [(
        str(folder / "Book.cbr"), str(output), str(backup), 1, json.dumps(["ad.jpg"]),
        10, 1, stat.st_size, stat.st_mtime_ns,
    )])
    return _item(cache)


def test_restoring_a_converted_book_brings_back_the_cbr(tmp_path, cache):
    item = _converted(tmp_path, cache)

    assert restore(cache, item) is None

    assert item.archive.read_bytes() == b"Rar!original"
    assert not item.output.exists()
    assert not item.backup.exists()


def test_a_converted_book_is_not_restored_over_a_new_cbr(tmp_path, cache):
    item = _converted(tmp_path, cache)
    item.archive.write_bytes(b"Rar!someone else's")

    assert restore(cache, item) == "Book.cbr already exists"
    assert item.output.exists()


# -- export ----------------------------------------------------------------


def test_history_exports_as_csv(library, cache, tmp_path):
    _clean(library, cache)
    target = tmp_path / "history.csv"

    assert export_csv(load_history(cache), target) == 2

    rows = list(csv.reader(io.StringIO(target.read_text(encoding="utf-8"))))
    assert rows[0] == CSV_COLUMNS
    assert {Path(row[3]).name for row in rows[1:]} == {"Book 01.cbz", "Book 02.cbz"}
    assert all(row[-1] == "can be restored" for row in rows[1:])


# -- the command line ------------------------------------------------------


def _run(*argv: str, stdin: io.StringIO | None = None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), stdout=out, stderr=err, stdin=stdin or io.StringIO())
    return code, out.getvalue(), err.getvalue()


def test_the_cli_lists_and_restores_a_run(library, tmp_path):
    db = str(tmp_path / "c.sqlite")
    _run("clean", str(library), "--cache", db, "--all", "--yes")

    code, out, _ = _run("history", "--cache", db, "list")
    assert code == cli.EXIT_OK
    assert "Run 1" in out and "2 restorable" in out

    code, _, err = _run("history", "--cache", db, "restore", "1")
    assert code == cli.EXIT_REFUSED and "--yes" in err
    assert _pages(library / "Book 01.cbz") == 4

    code, out, _ = _run("history", "--cache", db, "restore", "1", "--yes")
    assert code == cli.EXIT_OK and "Restored 2 of 2" in out
    assert _pages(library / "Book 01.cbz") == 5

    _, listing, _ = _run("history", "--cache", db, "list", "--json")
    books = json.loads(listing)["runs"][0]["books"]
    assert all(not b["restorable"] and b["status"].startswith("restored") for b in books)


def test_a_dry_run_leaves_no_history(library, tmp_path):
    db = str(tmp_path / "c.sqlite")
    _run("clean", str(library), "--cache", db, "--all", "--dry-run")

    _, out, _ = _run("history", "--cache", db, "list")
    assert "No removals yet." in out


def test_restoring_an_unknown_run_is_refused(tmp_path):
    code, _, err = _run("history", "--cache", str(tmp_path / "c.sqlite"), "restore", "9")

    assert code == cli.EXIT_REFUSED
    assert "No run 9" in err
