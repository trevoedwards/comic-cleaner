"""The real .cbr books in the test comics folder.

CC_TEST_COMICS in .env (PageMaster-Test-Content on the development machine) is
mounted read-only at /comics in the Docker dev service. Its comics are
copyrighted, so they are not in the repo and CI never has them: these tests run
in Docker and skip anywhere else. The five Green Lantern Corps issues are real
RAR books, and the same scan group's logo is the last page of every one.
CC_REAL_COMICS points the tests at another copy of the folder.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from comiccleaner import cli
from comiccleaner.core import extern
from comiccleaner.core.archive import detect_kind, is_page_name
from comiccleaner.core.grouping import GroupingOptions, build_groups
from comiccleaner.core.model import ArchiveKind
from comiccleaner.core.scanner import scan_archives

COMICS = Path(os.environ.get("CC_REAL_COMICS", "/comics"))
CBR = sorted(COMICS.glob("Green Lantern Corps #00? (2006).cbr")) if COMICS.is_dir() else []

# Pages per issue, as scanned.
PAGES = {
    "Green Lantern Corps #001 (2006).cbr": 23,
    "Green Lantern Corps #002 (2006).cbr": 25,
    "Green Lantern Corps #003 (2006).cbr": 24,
    "Green Lantern Corps #004 (2006).cbr": 24,
    "Green Lantern Corps #005 (2006).cbr": 25,
}
LOGO = "zzTLK.jpg"

pytestmark = [
    pytest.mark.skipif(
        [p.name for p in CBR] != sorted(PAGES),
        reason=f"the five Green Lantern Corps .cbr books are not in {COMICS}",
    ),
    pytest.mark.skipif(not extern.can_extract("rar"), reason="no RAR reader installed"),
]


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), stdout=out, stderr=err, stdin=io.StringIO())
    return code, out.getvalue() + err.getvalue()


def _copies(tmp_path: Path, count: int = 3) -> list[Path]:
    """Writable copies of the first `count` issues; /comics is read-only."""
    library = tmp_path / "library"
    library.mkdir()
    return [Path(shutil.copy2(book, library / book.name)) for book in CBR[:count]]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_real_cbr_is_a_rar_and_reads_whole():
    infos = scan_archives(CBR)

    assert all(detect_kind(book) is ArchiveKind.RAR for book in CBR)
    assert {i.path.name: i.error for i in infos if i.error} == {}
    assert {i.path.name: i.page_count for i in infos} == PAGES


def test_the_scan_groups_logo_is_found_in_every_real_cbr():
    groups = build_groups(scan_archives(CBR), GroupingOptions())

    logo = [g for g in groups if {p.name.rsplit("/", 1)[-1] for p in g.pages} == {LOGO}]
    assert len(logo) == 1
    assert {p.archive.name for p in logo[0].pages} == set(PAGES)


def test_cleaning_a_real_cbr_rebuilds_it_as_a_smaller_cbz(tmp_path):
    books = _copies(tmp_path)

    code, _ = _run(
        "clean", str(books[0].parent), "--all", "--yes", "--cache", str(tmp_path / "c.db")
    )

    assert code == cli.EXIT_OK
    for book in books:
        cleaned = book.with_suffix(".cbz")
        assert not book.exists(), "the .cbr stays only as its backup"
        assert book.with_name(book.name + ".bak").exists()
        with zipfile.ZipFile(cleaned) as zf:
            assert zf.testzip() is None
            pages = [n for n in zf.namelist() if is_page_name(n)]
        assert 0 < len(pages) < PAGES[book.name]
        assert not any(n.endswith(LOGO) for n in pages)


def test_leave_cbr_leaves_real_cbr_books_as_they_were(tmp_path):
    books = _copies(tmp_path)
    before = {book.name: _sha(book) for book in books}

    code, out = _run(
        "clean", str(books[0].parent), "--all", "--yes", "--leave-cbr",
        "--cache", str(tmp_path / "c.db"),
    )

    assert code == cli.EXIT_OK
    assert f"Leaving {len(books)} .cbr/.cb7 book(s) unchanged" in out
    assert {p.name for p in books[0].parent.iterdir()} == set(before)
    assert {book.name: _sha(book) for book in books} == before
