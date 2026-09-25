"""What gets imported from a folder, disk space, and runs cut off mid-swap."""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from comiccleaner import cli
from comiccleaner.core import remover
from comiccleaner.core.archive import TEMP_PREFIX, ComicArchive
from comiccleaner.core.hashing import content_digest
from comiccleaner.core.remover import (
    MARKER_SUFFIX,
    BackupPolicy,
    RemovalError,
    RemovalPlan,
    apply_plan,
    apply_removals,
    check_free_space,
    on_same_volume,
    recover_interrupted,
)
from comiccleaner.core.scanner import find_archives

from .conftest import make_page, write_archive
from .test_cli import run

_Usage = namedtuple("_Usage", "total used free")


def _plan(book: Path, names: set[str], pages: int) -> RemovalPlan:
    with ComicArchive(book) as arc:
        shas = {n: content_digest(arc.read(n)) for n in names}
    return RemovalPlan(
        archive=book, remove_names=set(names), original_pages=pages, expected_sha=shas
    )


def _book(path: Path, pages: int = 4) -> Path:
    return write_archive(path, [make_page(seed=len(path.name) * 10 + i) for i in range(pages)])


def _free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    monkeypatch.setattr(remover.shutil, "disk_usage", lambda _p: _Usage(10**12, 0, free))


def _markers(folder: Path) -> list[Path]:
    return list(folder.rglob(f"{TEMP_PREFIX}*{MARKER_SUFFIX}"))


# -- what a folder walk picks up --------------------------------------------


def test_folder_walk_skips_plain_rar_and_7z(tmp_path: Path) -> None:
    for name in ("a.cbz", "b.zip", "c.cbr", "d.cb7", "e.rar", "f.7z"):
        (tmp_path / "sub" / name).parent.mkdir(exist_ok=True)
        (tmp_path / "sub" / name).write_bytes(b"x")

    found = {p.name for p in find_archives([tmp_path])}

    assert found == {"a.cbz", "b.zip", "c.cbr", "d.cb7"}


def test_a_rar_or_7z_named_directly_is_still_imported(tmp_path: Path) -> None:
    rar = tmp_path / "e.rar"
    seven = tmp_path / "f.7z"
    rar.write_bytes(b"x")
    seven.write_bytes(b"x")

    assert find_archives([rar, seven]) == sorted([rar.resolve(), seven.resolve()])


# -- a backup copied to another volume ---------------------------------------


def _cross_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(src: object, dst: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(remover.os, "replace", refuse)


def test_cross_volume_backup_is_refused_before_copying_when_space_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "book.cbz"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "elsewhere" / "book.cbz.bak"
    dst.parent.mkdir()
    _cross_volume(monkeypatch)
    _free(monkeypatch, 999)
    copied = []
    monkeypatch.setattr(remover.shutil, "copy2", lambda *a: copied.append(a))

    with pytest.raises(RemovalError, match="not enough free space"):
        remover._move_aside(src, dst)

    assert copied == []
    assert src.read_bytes() == b"x" * 1000
    assert not dst.exists()


def test_cross_volume_backup_copies_when_there_is_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "book.cbz"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "elsewhere" / "book.cbz.bak"
    dst.parent.mkdir()
    _cross_volume(monkeypatch)
    _free(monkeypatch, 1000)

    remover._move_aside(src, dst)

    assert not src.exists()
    assert dst.read_bytes() == b"x" * 1000


def test_a_folder_not_yet_made_is_on_its_parents_volume(tmp_path: Path) -> None:
    assert on_same_volume(tmp_path / "book.cbz", tmp_path / "not" / "made" / "yet")


# -- a run killed between the backup and the swap ----------------------------


class _Killed(BaseException):
    """Stands in for the process dying: no except clause in the remover sees it."""


def test_a_run_killed_mid_swap_leaves_a_note_and_recovery_puts_the_book_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _book(tmp_path / "book.cbz")
    before = book.read_bytes()

    def die(*_a: object) -> None:
        raise _Killed

    monkeypatch.setattr(remover, "_apply_mode", die)  # runs after the move aside
    with pytest.raises(_Killed):
        apply_plan(_plan(book, {"page002.jpg"}, 4))
    monkeypatch.undo()

    assert not book.exists()
    assert (tmp_path / "book.cbz.bak").read_bytes() == before
    assert len(_markers(tmp_path)) == 1

    monkeypatch.setattr(remover, "_process_alive", lambda _pid: False)
    recovered = recover_interrupted([tmp_path])

    assert [(r.original, r.restored) for r in recovered] == [(book, True)]
    assert book.read_bytes() == before
    assert not (tmp_path / "book.cbz.bak").exists()
    assert _markers(tmp_path) == []
    assert not list(tmp_path.glob(f"{TEMP_PREFIX}*.cbz"))  # the unswapped temp is gone


def _leftover(folder: Path, *, book_exists: bool) -> tuple[Path, Path, Path]:
    """The files a killed run leaves: backup, temp and note, with or without the book."""
    book = folder / "book.cbz"
    backup = folder / "book.cbz.bak"
    temp = folder / f"{TEMP_PREFIX}abc.cbz"
    backup.write_bytes(b"original")
    temp.write_bytes(b"half written")
    if book_exists:
        book.write_bytes(b"cleaned")
        temp.unlink()  # a finished swap renamed it into place
    remover._write_marker(book, book, backup, temp)
    return book, backup, temp


def test_missing_book_is_restored_from_its_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remover, "_process_alive", lambda _pid: False)
    book, backup, temp = _leftover(tmp_path, book_exists=False)

    # A book path, as a saved session lists it, finds the note in its folder.
    recovered = recover_interrupted([book])

    assert [r.restored for r in recovered] == [True]
    assert book.read_bytes() == b"original"
    assert not backup.exists()
    assert not temp.exists()
    assert _markers(tmp_path) == []


def test_a_finished_swap_only_clears_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remover, "_process_alive", lambda _pid: False)
    book, backup, _ = _leftover(tmp_path, book_exists=True)

    recovered = recover_interrupted([tmp_path])

    assert [r.restored for r in recovered] == [False]
    assert book.read_bytes() == b"cleaned"
    assert backup.read_bytes() == b"original"
    assert _markers(tmp_path) == []


def test_a_book_deleted_on_purpose_is_not_brought_back(tmp_path: Path) -> None:
    """No note, no recovery: a lone .bak says nothing about why the book is gone."""
    backup = tmp_path / "book.cbz.bak"
    backup.write_bytes(b"original")

    assert recover_interrupted([tmp_path, tmp_path / "book.cbz"]) == []
    assert backup.exists()
    assert not (tmp_path / "book.cbz").exists()


def test_a_run_still_going_in_another_process_is_left_alone(tmp_path: Path) -> None:
    # The note carries this process's id, which is certainly still running.
    book, backup, temp = _leftover(tmp_path, book_exists=False)

    assert recover_interrupted([tmp_path]) == []
    assert not book.exists()
    assert backup.exists() and temp.exists()
    assert len(_markers(tmp_path)) == 1


def test_recovery_dry_run_reports_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remover, "_process_alive", lambda _pid: False)
    book, backup, temp = _leftover(tmp_path, book_exists=False)

    recovered = recover_interrupted([tmp_path], dry_run=True)

    assert [r.restored for r in recovered] == [True]
    assert not book.exists()
    assert backup.exists() and temp.exists()
    assert len(_markers(tmp_path)) == 1


def test_an_unreadable_note_is_left_alone(tmp_path: Path) -> None:
    note = tmp_path / f"{TEMP_PREFIX}junk{MARKER_SUFFIX}"
    note.write_text("{not json", encoding="utf-8")

    assert recover_interrupted([tmp_path]) == []
    assert note.exists()


def test_process_alive_tells_a_running_process_from_a_finished_one() -> None:
    assert remover._process_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not remover._process_alive(child.pid)


def test_finished_and_failed_runs_leave_no_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _book(tmp_path / "good.cbz")
    assert apply_plan(_plan(good, {"page002.jpg"}, 4)).ok

    bad = _book(tmp_path / "bad.cbz")
    before = bad.read_bytes()

    def fail(*_a: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(remover, "_apply_mode", fail)
    result = apply_plan(_plan(bad, {"page002.jpg"}, 4))

    assert result.error is not None
    assert bad.read_bytes() == before  # put back by the except handler
    assert _markers(tmp_path) == []


def test_cli_clean_recovers_first_and_a_dry_run_only_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remover, "_process_alive", lambda _pid: False)
    library = tmp_path / "library"
    library.mkdir()
    _book(library / "other.cbz")
    book, backup, _ = _leftover(library, book_exists=False)
    shutil.copyfile(library / "other.cbz", backup)  # a readable original
    cache = str(tmp_path / "cache.sqlite")

    code, _, err = run("clean", str(library), "--cache", cache, "--all", "--dry-run")
    assert code == cli.EXIT_OK
    assert f"Would put back {book}" in err
    assert not book.exists()

    code, _, err = run("clean", str(library), "--cache", cache, "--all", "--yes")
    assert f"Put back {book}" in err
    assert book.exists()
    assert _markers(library) == []


# -- enough room for the whole run -------------------------------------------


def _books(folder: Path) -> list[Path]:
    return [_book(folder / f"b{i}.cbz") for i in range(2)]


def _plans(books: list[Path]) -> list[RemovalPlan]:
    return [_plan(b, {"page002.jpg"}, 4) for b in books]


def _needed(books: list[Path], monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> int:
    _free(monkeypatch, 0)
    (shortfall,) = check_free_space(_plans(books), **kwargs)
    return shortfall.needed


def test_space_needed_follows_where_the_bytes_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    books = _books(tmp_path)
    sizes = [b.stat().st_size for b in books]

    # Backups beside the books keep every original as well as the cleaned copy.
    assert _needed(books, monkeypatch) == sum(sizes)
    # Cleaned copies elsewhere all stay.
    assert _needed(books, monkeypatch, output_dir=tmp_path / "out") == sum(sizes)
    # No backup: each temp replaces its book, so one book's room at a time.
    assert _needed(books, monkeypatch, backup=BackupPolicy(enabled=False)) == max(sizes)


def test_backups_on_another_volume_are_counted_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    books = _books(tmp_path / "books")
    sizes = [b.stat().st_size for b in books]
    backups = tmp_path / "backups"
    monkeypatch.setattr(
        remover, "_volume", lambda p: (2 if Path(p).name == "backups" else 1, Path(p))
    )
    _free(monkeypatch, 0)

    shortfalls = check_free_space(
        _plans(books), backup=BackupPolicy(enabled=True, directory=backups)
    )

    needed = {s.folder.name: s.needed for s in shortfalls}
    assert needed == {"books": max(sizes), "backups": sum(sizes)}


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_a_run_that_cannot_fit_stops_before_the_first_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    books = _books(tmp_path)
    before = [b.read_bytes() for b in books]
    _free(monkeypatch, 10)

    report = apply_removals(_plans(books), dry_run=dry_run)

    assert report.blocked
    assert report.results == []
    (shortfall,) = report.space_shortfall
    assert shortfall.free == 10
    assert "short by" in shortfall.describe()
    assert [b.read_bytes() for b in books] == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b0.cbz", "b1.cbz"]


def test_cli_reports_the_shortfall_and_fails(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {p.name: p.read_bytes() for p in library.iterdir()}
    _free(monkeypatch, 10)
    cache = str(tmp_path / "cache.sqlite")

    code, out, _ = run("clean", str(library), "--cache", cache, "--all", "--yes")
    assert code == cli.EXIT_FAILURES
    assert "Not enough free space" in out

    code, out, _ = run(
        "clean", str(library), "--cache", cache, "--all", "--dry-run", "--json"
    )
    assert code == cli.EXIT_FAILURES
    assert json.loads(out)["space_shortfall"][0]["free"] == 10
    assert {p.name: p.read_bytes() for p in library.iterdir()} == before
