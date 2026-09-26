"""Finding books, what they say about themselves, and telling issues from filler."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from comiccleaner.core.cache import HashCache
from comiccleaner.core.comicinfo import read_metadata
from comiccleaner.core.duplicates import find_duplicate_books
from comiccleaner.core.grouping import (
    WARN_MID_BOOK,
    GroupingOptions,
    build_groups,
    matches_any,
    review_warnings,
    sort_groups,
    spread,
)
from comiccleaner.core.model import ArchiveInfo, ArchiveKind, Decision
from comiccleaner.core.remover import apply_removals, build_plans
from comiccleaner.core.scanner import (
    ScanStats,
    find_archives,
    find_relocated,
    scan_archive,
    scan_archives,
    survey,
)

from .conftest import make_page, write_archive

# -- walking folders -------------------------------------------------------


def test_a_folder_walk_counts_what_it_leaves_out(tmp_path):
    write_archive(tmp_path / "Book.cbz", [make_page(1)])
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.7")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Scan.PDF").write_bytes(b"%PDF-1.7")
    (tmp_path / "installer.rar").write_bytes(b"Rar!")
    (tmp_path / "backup.7z").write_bytes(b"7z")

    found = survey([tmp_path])

    assert [p.name for p in found.found] == ["Book.cbz"]
    assert found.pdfs == 2 and found.unwalked == 2 and found.excluded == 0
    assert not found.cancelled


def test_a_pdf_named_directly_is_counted_and_nothing_else_is_taken(tmp_path):
    pdf = tmp_path / "comic.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    found = survey([pdf])
    assert found.found == [] and found.pdfs == 1


def test_exclude_globs_match_names_and_paths_below_the_root(tmp_path):
    for name in ("Keep.cbz", "Keep sample.cbz", "Scans/One.cbz", "Other/Two.cbz"):
        write_archive(tmp_path / name, [make_page(1)])

    found = survey([tmp_path], exclude=["*sample*", "Scans/*", "  "])

    assert sorted(p.name for p in found.found) == ["Keep.cbz", "Two.cbz"]
    assert found.excluded == 2
    # A file named directly is matched on its name.
    assert find_archives([tmp_path / "Keep sample.cbz"], exclude=["*sample*"]) == []
    # Backups were never walked, and an exclude does not change that.
    (tmp_path / "Keep.cbz.bak").write_bytes(b"x")
    assert "Keep.cbz.bak" not in {p.name for p in find_archives([tmp_path])}


def test_a_walk_reports_its_folders_and_can_be_stopped(tmp_path):
    for index in range(3):
        write_archive(tmp_path / f"d{index}" / "Book.cbz", [make_page(index)])
    seen: list[Path] = []

    found = survey([tmp_path], progress=seen.append, should_cancel=lambda: len(seen) >= 2)

    assert found.cancelled
    assert len(seen) == 2 and seen[0] == tmp_path
    assert len(found.found) <= 1


# -- scan statistics -------------------------------------------------------


def _info(pages: int, cached: bool) -> ArchiveInfo:
    info = ArchiveInfo(path=Path("x.cbz"), kind=ArchiveKind.ZIP, size=0, mtime_ns=0,
                       page_count=pages, cached=cached)
    return info


def test_scan_stats_count_hashed_and_cached_pages_and_estimate_from_uncached_books():
    now = [100.0]
    stats = ScanStats(total=10, clock=lambda: now[0])
    stats.record(_info(20, cached=True))
    assert stats.eta() is None  # a cache hit says nothing about how long hashing takes
    now[0] = 104.0
    stats.record(_info(30, cached=False))
    stats.record(_info(10, cached=True))
    # 4 s per uncached book; 7 books left, a third of those seen so far needed hashing.
    assert stats.pages_hashed == 30 and stats.pages_cached == 30
    assert abs(stats.eta() - 4.0 * 7 / 3) < 1e-9
    assert stats.describe() == "30 pages hashed, 30 from cache, about 9 s left"


def test_scanned_books_say_whether_they_came_from_the_cache(library, tmp_path):
    cache = HashCache(tmp_path / "c.sqlite")
    try:
        seen: list[ArchiveInfo] = []
        first = scan_archives(find_archives([library]), cache=cache, on_archive=seen.append)
        assert len(seen) == 3 and not any(a.cached for a in first)
        again = scan_archives(find_archives([library]), cache=cache)
        assert all(a.cached for a in again)
    finally:
        cache.close()


# -- ComicInfo -------------------------------------------------------------


def test_metadata_is_read_by_local_name_and_broken_xml_gives_nothing():
    plain = b"<ComicInfo><Series> Saga </Series><Number>12</Number><Title>X</Title></ComicInfo>"
    assert read_metadata(plain) == {"series": "Saga", "number": "12", "title": "X"}
    spaced = (
        b'<?xml version="1.0"?><ci:ComicInfo xmlns:ci="urn:x">'
        b"<ci:Series>Saga</ci:Series><ci:Number>3</ci:Number></ci:ComicInfo>"
    )
    assert read_metadata(spaced) == {"series": "Saga", "number": "3"}
    assert read_metadata(b"<ComicInfo><Series>unclosed") == {}
    assert read_metadata(b"<ComicInfo/>") == {}


def test_a_scan_picks_up_series_and_number_and_the_cache_keeps_them(tmp_path):
    book = write_archive(tmp_path / "Book 07.cbz", [make_page(1), make_page(2)])
    bare = write_archive(tmp_path / "Bare.cbz", [make_page(3)], comicinfo=False)
    cache = HashCache(tmp_path / "c.sqlite")
    try:
        info = scan_archive(book, cache)
        assert (info.series, info.number) == ("Test Series", "Book 07")
        assert info.display_name == "Test Series #Book 07"
        assert scan_archive(bare, cache).display_name == "Bare.cbz"

        again = scan_archive(book, cache)
        assert again.cached and again.display_name == "Test Series #Book 07"
        assert cache.metadata(bare) == {}
    finally:
        cache.close()


def test_a_broken_comicinfo_does_not_fail_the_scan(tmp_path):
    book = tmp_path / "Broken.cbz"
    with zipfile.ZipFile(book, "w") as zf:
        zf.writestr("001.jpg", make_page(1))
        zf.writestr("ComicInfo.xml", "<ComicInfo><Series>oops")
    info = scan_archive(book)
    assert info.error is None and info.page_count == 1 and info.display_name == "Broken.cbz"


def test_books_hashed_before_metadata_was_kept_get_it_on_the_next_scan(tmp_path):
    book = write_archive(tmp_path / "Book 01.cbz", [make_page(1)])
    cache = HashCache(tmp_path / "c.sqlite")
    try:
        scan_archive(book, cache)
        cache.conn.execute("UPDATE archives SET comicinfo = NULL")  # as an older version left it
        assert cache.metadata(book) is None
        info = scan_archive(book, cache)
        assert info.cached and info.series == "Test Series"
        assert cache.metadata(book) == {"series": "Test Series", "number": "Book 01"}
    finally:
        cache.close()


# -- relocating missing books ----------------------------------------------


def test_missing_books_are_found_by_name_size_and_time(tmp_path):
    old = tmp_path / "old"
    moved = write_archive(tmp_path / "new" / "deep" / "A.cbz", [make_page(1)])
    other = write_archive(tmp_path / "new" / "B.cbz", [make_page(2)])
    twin_one = write_archive(tmp_path / "new" / "x" / "C.cbz", [make_page(3)])
    twin_two = tmp_path / "new" / "y" / "C.cbz"
    twin_two.parent.mkdir(parents=True)
    twin_two.write_bytes(twin_one.read_bytes())
    stamp = twin_one.stat().st_mtime_ns
    os.utime(twin_two, ns=(stamp, stamp))

    def identity(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    identities = {
        old / "A.cbz": identity(moved),
        old / "B.cbz": (identity(other)[0] + 1, identity(other)[1]),  # changed since
        old / "C.cbz": identity(twin_one),
    }
    result = find_relocated([*identities, old / "Unknown.cbz"], tmp_path / "new", identities)

    assert result.found == {old / "A.cbz": moved.resolve()}
    assert result.ambiguous == [old / "C.cbz"]
    assert sorted(p.name for p in result.not_found) == ["B.cbz", "Unknown.cbz"]


def test_the_cache_knows_a_books_size_and_time(library, tmp_path):
    cache = HashCache(tmp_path / "c.sqlite")
    try:
        book = library / "Book 01.cbz"
        assert cache.identity(book) is None
        scan_archive(book, cache)
        stat = book.stat()
        assert cache.identity(book) == (stat.st_size, stat.st_mtime_ns)
    finally:
        cache.close()


# -- whole-book duplicates -------------------------------------------------


def test_two_copies_of_one_issue_are_reported_not_grouped_as_filler(tmp_path):
    story = [make_page(seed=500 + i) for i in range(8)]
    write_archive(tmp_path / "Issue 1.cbz", story)
    write_archive(tmp_path / "Issue 1 (copy).cbz", story[:7])  # one page short
    write_archive(tmp_path / "Other.cbz", [story[0], *[make_page(900 + i) for i in range(7)]])
    archives = scan_archives(find_archives([tmp_path]))

    [pair] = find_duplicate_books(archives)

    assert pair.smaller.name == "Issue 1 (copy).cbz" and pair.larger.name == "Issue 1.cbz"
    assert pair.shared == 7 and pair.smaller_pages == 7 and pair.overlap == 1.0
    assert "share 100%" in pair.describe()
    assert all(g.decision is Decision.UNDECIDED for g in build_groups(archives))


def test_shared_filler_does_not_make_books_duplicates(tmp_path):
    ad = make_page(seed=42)
    for index in range(8):
        pages = [make_page(seed=index * 10 + i) for i in range(3)]
        write_archive(tmp_path / f"Book {index}.cbz", [*pages, ad])
    archives = scan_archives(find_archives([tmp_path]))
    assert find_duplicate_books(archives) == []


# -- zip dates -------------------------------------------------------------


def test_kept_entries_keep_their_zip_dates(tmp_path):
    book = tmp_path / "Dated.cbz"
    ad = make_page(seed=77)
    with zipfile.ZipFile(book, "w") as zf:
        for index, (data, when) in enumerate([
            (make_page(1), (2001, 2, 3, 4, 5, 6)),
            (ad, (2002, 1, 1, 0, 0, 0)),
            (make_page(2), (2003, 7, 8, 9, 10, 12)),
        ]):
            zf.writestr(zipfile.ZipInfo(f"{index:03d}.jpg", date_time=when), data)
    write_archive(tmp_path / "Other.cbz", [make_page(3), ad, make_page(4)])
    archives = scan_archives(find_archives([tmp_path]))
    [group] = build_groups(archives, GroupingOptions())
    group.decision = Decision.DELETE

    report = apply_removals(build_plans([group], {a.path: a.page_count for a in archives}))

    assert len(report.succeeded) == 2
    with zipfile.ZipFile(book) as zf:
        dates = {i.filename: i.date_time for i in zf.infolist()}
    assert dates == {"000.jpg": (2001, 2, 3, 4, 5, 6), "002.jpg": (2003, 7, 8, 9, 10, 12)}


# -- grouping options ------------------------------------------------------


def _books_with_ad_at(root: Path, index: int, books: int = 3, pages: int = 12) -> Path:
    ad = make_page(seed=4242)
    for number in range(books):
        story = [make_page(seed=number * 100 + i) for i in range(pages - 1)]
        story.insert(index, ad)
        write_archive(root / f"B{number}.cbz", story)
    return root


def test_the_edge_window_decides_what_counts_as_mid_book(tmp_path):
    archives = scan_archives(find_archives([_books_with_ad_at(tmp_path, 4)]))
    [narrow] = build_groups(archives, GroupingOptions(edge_pages=3))
    [wide] = build_groups(archives, GroupingOptions(edge_pages=5))
    assert narrow.edge_share == 0 and WARN_MID_BOOK in review_warnings(narrow)
    assert wide.edge_share == 1 and WARN_MID_BOOK not in review_warnings(wide)


def test_the_new_sort_keys(library, tmp_path):
    archives = scan_archives(find_archives([library, _books_with_ad_at(tmp_path / "mid", 5)]))
    groups = build_groups(archives, GroupingOptions(threshold=8))
    similar = next(g for g in groups if spread(g) > 0)
    mid = next(g for g in groups if g.edge_share == 0)

    assert sort_groups(groups, "warning")[0] is mid
    assert sort_groups(groups, "edge")[-1] is mid
    assert sort_groups(groups, "distance")[0] is similar
    assert spread(mid) == 0  # identical copies


def test_matches_any_uses_the_threshold():
    assert matches_any([0b1011], {0b1000}, 2)
    assert not matches_any([0b1011], {0b1000}, 1)
    assert not matches_any([], {1}, 5) and not matches_any([1], set(), 5)
