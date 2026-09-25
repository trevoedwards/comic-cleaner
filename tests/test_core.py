"""End-to-end tests for scan -> group -> remove."""

from __future__ import annotations

import errno
import os
import stat
import sys
import warnings
import zipfile
from pathlib import Path

import numpy as np
import pytest

from comiccleaner.core import extern, grouping, remover
from comiccleaner.core.archive import (
    TEMP_PREFIX,
    ArchiveError,
    ComicArchive,
    is_page_name,
    natural_key,
)
from comiccleaner.core.cache import HashCache
from comiccleaner.core.comicinfo import update_comicinfo
from comiccleaner.core.grouping import GroupingOptions, build_groups
from comiccleaner.core.hashing import content_digest, digest_image, hamming
from comiccleaner.core.model import ArchiveKind, Decision, MatchKind, PageEntry
from comiccleaner.core.remover import (
    DEFAULT_MAX_FRACTION,
    BackupPolicy,
    RemovalPlan,
    apply_plan,
    apply_removals,
    build_plans,
    is_backup_name,
    split_by_fraction,
)
from comiccleaner.core.scanner import find_archives, scan_archive, scan_archives

from .conftest import make_flat_page, make_page, write_archive

# -- scanning --------------------------------------------------------------


def test_find_archives_walks_folders(library: Path) -> None:
    found = find_archives([library])
    assert len(found) == 3
    assert all(p.suffix == ".cbz" for p in found)


def test_scan_reads_every_page(library: Path) -> None:
    info = scan_archive(library / "Book 01.cbz")
    assert info.error is None
    assert info.page_count == 5
    assert all(p.error is None for p in info.pages)
    assert all(p.width == 400 and p.height == 600 for p in info.pages)
    # ComicInfo.xml must not be counted as a page.
    assert not any(p.name.endswith(".xml") for p in info.pages)


def test_scan_uses_cache_on_second_pass(library: Path, tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "cache.sqlite")
    book = library / "Book 01.cbz"

    first = scan_archive(book, cache)
    second = scan_archive(book, cache)

    assert [p.content_sha for p in first.pages] == [p.content_sha for p in second.pages]
    assert [p.dhash for p in first.pages] == [p.dhash for p in second.pages]
    cache.close()


def test_cache_invalidates_when_file_changes(library: Path, tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "cache.sqlite")
    book = library / "Book 01.cbz"
    scan_archive(book, cache)

    write_archive(book, [make_page(seed=1), make_page(seed=2)])
    refreshed = scan_archive(book, cache)

    assert refreshed.page_count == 2
    cache.close()


# -- hashing ---------------------------------------------------------------


def test_identical_bytes_share_both_hashes(ad_page: bytes) -> None:
    a, b = digest_image(ad_page), digest_image(ad_page)
    assert a.content_sha == b.content_sha
    assert a.dhash == b.dhash


def test_recompression_changes_bytes_but_not_perceptual_hash(ad_page: bytes) -> None:
    from .conftest import _recompress

    original = digest_image(ad_page)
    degraded = digest_image(_recompress(ad_page, quality=40))

    assert original.content_sha != degraded.content_sha
    assert hamming(original.dhash, degraded.dhash) <= 4


def test_rescaling_preserves_perceptual_hash(ad_page: bytes) -> None:
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(ad_page)).resize((200, 300))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)

    assert hamming(digest_image(ad_page).dhash, digest_image(buf.getvalue()).dhash) <= 6


def test_blank_pages_are_flagged_flat() -> None:
    assert digest_image(make_flat_page(255)).flat is True
    assert digest_image(make_page(seed=5)).flat is False


# -- grouping --------------------------------------------------------------


def test_exact_grouping_finds_the_byte_identical_advert(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = build_groups(archives, GroupingOptions(threshold=0))

    assert len(groups) == 1
    group = groups[0]
    assert group.kind is MatchKind.EXACT
    assert group.page_count == 2  # books 1 and 2 only
    assert group.archive_count == 2


def test_perceptual_grouping_also_catches_the_recompressed_copy(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = build_groups(archives, GroupingOptions(threshold=8))

    assert len(groups) == 1
    group = groups[0]
    assert group.kind is MatchKind.SIMILAR  # mixed content hashes
    assert group.page_count == 3
    assert group.archive_count == 3


def test_flat_pages_are_excluded_by_default(tmp_path: Path) -> None:
    books = tmp_path / "flat"
    for number in (1, 2):
        write_archive(
            books / f"Blank {number}.cbz",
            [make_page(seed=number), make_flat_page(255), make_page(seed=number + 50)],
        )
    archives = scan_archives(find_archives([books]))

    assert build_groups(archives, GroupingOptions(threshold=0)) == []
    included = build_groups(archives, GroupingOptions(threshold=0, include_flat=True))
    assert len(included) == 1


def test_min_archives_filters_single_book_repeats(tmp_path: Path) -> None:
    books = tmp_path / "solo"
    repeated = make_page(seed=77)
    write_archive(books / "Solo.cbz", [make_page(seed=1), repeated, repeated])
    archives = scan_archives(find_archives([books]))

    assert len(build_groups(archives, GroupingOptions(min_archives=1))) == 1
    assert build_groups(archives, GroupingOptions(min_archives=2)) == []


def test_skip_first_page_protects_covers(tmp_path: Path) -> None:
    books = tmp_path / "covers"
    shared_cover = make_page(seed=42)
    for number in (1, 2):
        write_archive(books / f"V{number}.cbz", [shared_cover, make_page(seed=number)])
    archives = scan_archives(find_archives([books]))

    assert len(build_groups(archives, GroupingOptions(skip_first_page=False))) == 1
    assert build_groups(archives, GroupingOptions(skip_first_page=True)) == []
    assert build_groups(archives, GroupingOptions()) == []  # covers are safe by default


def test_ignored_groups_are_hidden(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = build_groups(archives, GroupingOptions(threshold=8))
    hashes = {p.dhash for p in groups[0].pages}

    assert build_groups(archives, GroupingOptions(threshold=8), ignored=hashes) == []


def test_an_ignore_holds_when_the_threshold_changes(library: Path) -> None:
    """Ignoring at one threshold must not let parts of the group back at another."""
    archives = scan_archives(find_archives([library]))
    loose = build_groups(archives, GroupingOptions(threshold=8))[0]
    hashes = {p.dhash for p in loose.pages}

    # Tighter: the cluster splits, and every piece of it is still hidden.
    assert build_groups(archives, GroupingOptions(threshold=0), ignored=hashes) == []

    # Looser, from an ignore made at 0: the re-encoded copy stays hidden too.
    exact = build_groups(archives, GroupingOptions(threshold=0))[0]
    exact_hashes = {p.dhash for p in exact.pages}
    assert build_groups(archives, GroupingOptions(threshold=8), ignored=exact_hashes) == []


def test_ignoring_one_page_leaves_unrelated_groups_alone(tmp_path: Path) -> None:
    ads = [make_page(seed=9000 + i) for i in range(2)]
    for number in range(2):
        write_archive(tmp_path / f"B{number}.cbz", [make_page(seed=number), *ads])
    archives = scan_archives(find_archives([tmp_path]))
    groups = build_groups(archives, GroupingOptions(threshold=4))
    assert len(groups) == 2

    hidden = {p.dhash for p in groups[0].pages}
    remaining = build_groups(archives, GroupingOptions(threshold=4), ignored=hidden)

    assert [g.gid for g in remaining] == [groups[1].gid]


def test_ignore_list_round_trips_through_the_cache(tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "cache.sqlite")
    cache.ignore("00ff", {0xFF, 0x1FF}, note="2 copies", sample=(tmp_path / "a.cbz", "p1.jpg"))
    cache.ignore("f000000000000000", {0xF000000000000000})  # needs the full uint64 range

    assert cache.ignored_hashes() == {0xFF, 0x1FF, 0xF000000000000000}
    entries = {e.gid: e for e in cache.ignored_entries()}
    assert entries["00ff"].note == "2 copies"
    assert entries["00ff"].sample_path == tmp_path / "a.cbz"
    assert entries["00ff"].sample_name == "p1.jpg"

    cache.unignore("00ff")
    assert cache.ignored_hashes() == {0xF000000000000000}
    cache.clear_ignored()
    assert cache.ignored_hashes() == set()
    assert cache.ignored_entries() == []
    cache.close()


def test_ignores_from_an_older_database_still_apply(tmp_path: Path) -> None:
    """Before hashes were stored, an ignore was just its group id."""
    import sqlite3

    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE ignored (gid TEXT PRIMARY KEY, note TEXT, "
        "created_at REAL NOT NULL DEFAULT (strftime('%s','now')));"
        "INSERT INTO ignored(gid, note) VALUES ('00000000000000ab', '');"
    )
    conn.commit()
    conn.close()

    cache = HashCache(db)
    assert cache.ignored_hashes() == {0xAB}
    [entry] = cache.ignored_entries()
    assert entry.sample_path is None
    cache.close()


def test_group_id_is_stable_across_library_size(library: Path, tmp_path: Path) -> None:
    """Adding another book with the same advert must not change the group id."""
    archives = scan_archives(find_archives([library]))
    before = build_groups(archives, GroupingOptions(threshold=8))[0].gid

    extra = scan_archive(
        write_archive(
            tmp_path / "extra" / "Book 04.cbz",
            [make_page(seed=400), make_page(seed=9999), make_page(seed=401)],
        )
    )
    after = build_groups([*archives, extra], GroupingOptions(threshold=8))[0].gid

    assert before == after


# -- removal ---------------------------------------------------------------


def _mark_all_for_deletion(groups: list) -> list:
    for group in groups:
        group.decision = Decision.DELETE
    return groups


def _plan(book: Path, names: set[str], pages: int) -> RemovalPlan:
    """A plan for `names`, carrying the fingerprints a scan would have recorded."""
    with ComicArchive(book) as arc:
        shas = {n: content_digest(arc.read(n)) for n in names}
    return RemovalPlan(
        archive=book, remove_names=set(names), original_pages=pages, expected_sha=shas
    )


def test_removal_strips_the_advert_from_every_book(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    counts = {a.path: a.page_count for a in archives}

    report = apply_removals(build_plans(groups, counts))

    assert not report.failed
    assert report.total_removed == 3
    assert report.total_freed > 0

    for archive in archives:
        rescanned = scan_archive(archive.path)
        assert rescanned.page_count == 4
    # And the advert is gone for good.
    assert build_groups(scan_archives(find_archives([library])), GroupingOptions(threshold=8)) == []


def test_removal_creates_backups(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    counts = {a.path: a.page_count for a in archives}

    report = apply_removals(build_plans(groups, counts))

    for result in report.succeeded:
        assert result.backup is not None
        assert result.backup.exists()
        with zipfile.ZipFile(result.backup) as zf:
            assert sum(1 for n in zf.namelist() if is_page_name(n)) == 5


def test_removal_updates_comicinfo(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    counts = {a.path: a.page_count for a in archives}
    apply_removals(build_plans(groups, counts))

    with zipfile.ZipFile(library / "Book 01.cbz") as zf:
        xml = zf.read("ComicInfo.xml").decode()
    assert "<PageCount>4</PageCount>" in xml
    assert xml.count("<Page ") == 4


def test_output_dir_leaves_originals_untouched(library: Path, tmp_path: Path) -> None:
    out = tmp_path / "cleaned"
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    counts = {a.path: a.page_count for a in archives}

    apply_removals(build_plans(groups, counts), output_dir=out)

    assert scan_archive(library / "Book 01.cbz").page_count == 5
    assert scan_archive(out / "Book 01.cbz").page_count == 4


def test_dry_run_changes_nothing(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    counts = {a.path: a.page_count for a in archives}

    report = apply_removals(build_plans(groups, counts), dry_run=True)

    assert report.total_removed == 3
    assert scan_archive(library / "Book 01.cbz").page_count == 5
    assert not list(library.glob("*.bak"))


def test_kept_pages_are_not_removed(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = build_groups(archives, GroupingOptions(threshold=8))
    group = groups[0]
    group.decision = Decision.DELETE
    spared = next(p for p in group.pages if p.archive.name == "Book 01.cbz")
    group.kept.add(spared.key)

    counts = {a.path: a.page_count for a in archives}
    apply_removals(build_plans(groups, counts))

    assert scan_archive(library / "Book 01.cbz").page_count == 5
    assert scan_archive(library / "Book 02.cbz").page_count == 4


def test_refuses_to_empty_an_archive(tmp_path: Path) -> None:
    book = write_archive(tmp_path / "Tiny.cbz", [make_page(seed=1), make_page(seed=2)])
    plan = _plan(book, {"page001.jpg", "page002.jpg"}, 2)

    result = apply_plan(plan)

    assert result.error is not None
    assert "every page" in result.error
    assert scan_archive(book).page_count == 2


def test_open_archive_fails_cleanly_without_orphan_backup(library: Path) -> None:
    """Windows refuses to rename an open file; that must not corrupt anything.

    shutil.move would copy-then-delete here and strand a half-made .bak, so the
    remover uses os.replace and only falls back for genuine cross-volume moves.
    """
    book = library / "Book 01.cbz"
    plan = _plan(book, {"page002.jpg"}, 5)

    with zipfile.ZipFile(book) as still_open:
        still_open.namelist()  # keep the OS handle alive across the removal
        result = apply_plan(plan)

    if result.error is not None:
        # Windows: rejected, and the library is exactly as it was.
        assert not list(library.glob("*.bak"))
        assert scan_archive(book).page_count == 5
    else:
        # POSIX happily renames open files, so the removal simply succeeds.
        assert result.removed == 1
        assert scan_archive(book).page_count == 4


def test_stale_plan_is_rejected(library: Path) -> None:
    book = library / "Book 01.cbz"
    plan = RemovalPlan(archive=book, remove_names={"page999.jpg"}, original_pages=5)

    result = apply_plan(plan)

    assert result.error is not None
    assert "changed since scan" in result.error


def test_no_backup_policy_leaves_no_bak_files(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    counts = {a.path: a.page_count for a in archives}

    apply_removals(build_plans(groups, counts), backup=BackupPolicy(enabled=False))

    assert not list(library.glob("*.bak"))
    assert scan_archive(library / "Book 01.cbz").page_count == 4


def test_backup_directory_is_used_when_configured(library: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    counts = {a.path: a.page_count for a in archives}

    apply_removals(
        build_plans(groups, counts), backup=BackupPolicy(directory=backups)
    )

    assert len(list(backups.glob("*.bak"))) == 3
    assert not list(library.glob("*.bak"))


# -- misc ------------------------------------------------------------------


def test_natural_ordering_beats_lexicographic() -> None:
    names = ["page10.jpg", "page9.jpg", "page1.jpg"]
    assert sorted(names, key=natural_key) == ["page1.jpg", "page9.jpg", "page10.jpg"]


def test_comicinfo_survives_malformed_xml() -> None:
    broken = b"<ComicInfo><PageCount>3</PageCount"
    assert update_comicinfo(broken, {0}, 2) == broken


def test_summarise_counts_only_marked_groups(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = build_groups(archives, GroupingOptions(threshold=8))

    assert grouping.summarise(groups)["marked_pages"] == 0
    groups[0].decision = Decision.DELETE
    assert grouping.summarise(groups)["marked_pages"] == 3


def test_archive_reports_stored_size(library: Path) -> None:
    with ComicArchive(library / "Book 01.cbz") as arc:
        name = arc.page_names()[0]
        assert arc.stored_size(name) > 0
        assert arc.entry_size(name) > 0


# -- regressions -----------------------------------------------------------


def test_comicinfo_renumbers_a_partial_pages_block() -> None:
    """Many tools list only the special pages, so positions are not indices."""
    xml = (
        b"<ComicInfo><PageCount>10</PageCount><Pages>"
        b'<Page Image="0" Type="FrontCover"/>'
        b'<Page Image="3" Type="Advertisement"/>'
        b'<Page Type="Story"/>'
        b'<Page Image="7" Type="BackCover"/>'
        b"</Pages></ComicInfo>"
    )

    out = update_comicinfo(xml, {3, 4}, 8).decode()

    assert 'Image="0"' in out
    assert 'Image="5"' in out  # 7 slid down past the two removed pages
    assert 'Type="Advertisement"' not in out
    assert 'Type="Story"' in out  # no Image attribute, left alone
    assert "<PageCount>8</PageCount>" in out


def test_backup_names_are_recognised_strictly() -> None:
    assert is_backup_name("Book 01.cbz.bak")
    assert is_backup_name("Book 01.cbz.bak.2")
    assert is_backup_name("Book 01.CBR.BAK")
    # Real books that merely mention "bak" must never be offered for deletion.
    assert not is_backup_name("Bakugan Vol 1.cbz")
    assert not is_backup_name("Comic.bakery.cbz")
    assert not is_backup_name("Book.bak.cbz")
    assert not is_backup_name("notes.bak")
    assert not is_backup_name("Book 01.cbz")


def test_converting_never_overwrites_a_sibling_cbz(
    tmp_path: Path, monkeypatch
) -> None:
    """book.cbr is rebuilt as book.cbz, which may already be a different book."""
    pages = [make_page(seed=i) for i in range(3)]
    source = write_archive(tmp_path / "book.cbr", pages)  # a zip in disguise
    existing = write_archive(tmp_path / "book.cbz", [make_page(seed=50)], comicinfo=False)
    before = existing.read_bytes()
    monkeypatch.setattr(remover, "detect_kind", lambda _p: ArchiveKind.RAR)
    plan = _plan(source, {"page002.jpg"}, 3)

    result = apply_plan(plan)

    assert result.error is not None and "overwrite" in result.error
    assert existing.read_bytes() == before
    assert source.exists()
    assert not list(tmp_path.glob("*.bak"))


def _rar_pretender(monkeypatch) -> None:
    """Make the remover treat a zip named .cbr as a RAR, so it converts."""
    monkeypatch.setattr(remover, "detect_kind", lambda _p: ArchiveKind.RAR)


def test_conversion_without_backup_replaces_the_original(tmp_path: Path, monkeypatch) -> None:
    source = write_archive(tmp_path / "book.cbr", [make_page(seed=i) for i in range(3)])
    _rar_pretender(monkeypatch)
    plan = _plan(source, {"page002.jpg"}, 3)

    result = apply_plan(plan, backup=BackupPolicy(enabled=False))

    assert result.ok and result.converted
    assert not source.exists()
    assert scan_archive(tmp_path / "book.cbz").page_count == 2


def test_failed_conversion_without_backup_keeps_the_original(
    tmp_path: Path, monkeypatch
) -> None:
    """The source used to be deleted *before* the rebuilt file was moved in."""
    source = write_archive(tmp_path / "book.cbr", [make_page(seed=i) for i in range(3)])
    _rar_pretender(monkeypatch)
    real_replace = os.replace

    def deny_cbz(src, dst):
        if str(dst).endswith(".cbz"):
            raise PermissionError(errno.EACCES, "denied")
        return real_replace(src, dst)

    monkeypatch.setattr(remover.os, "replace", deny_cbz)
    plan = _plan(source, {"page002.jpg"}, 3)

    result = apply_plan(plan, backup=BackupPolicy(enabled=False))

    assert result.error is not None
    assert source.exists()
    assert scan_archive(source).page_count == 3


def _corrupt_page(path: Path, page: bytes) -> None:
    """Flip a byte inside a stored page so its CRC no longer matches."""
    raw = bytearray(path.read_bytes())
    raw[raw.find(page) + 20] ^= 0xFF
    path.write_bytes(bytes(raw))


def test_corrupt_entry_is_a_page_error_not_an_archive_error(tmp_path: Path) -> None:
    pages = [make_page(seed=i) for i in range(4)]
    book = write_archive(tmp_path / "book.cbz", pages)
    _corrupt_page(book, pages[1])

    info = scan_archive(book)

    assert info.error is None
    assert info.page_count == 4
    assert [p.error is not None for p in info.pages] == [False, True, False, False]


def test_removal_from_an_archive_with_a_corrupt_entry_fails_cleanly(tmp_path: Path) -> None:
    pages = [make_page(seed=i) for i in range(4)]
    book = write_archive(tmp_path / "book.cbz", pages)
    _corrupt_page(book, pages[1])
    before = book.read_bytes()
    plan = _plan(book, {"page003.jpg"}, 4)

    result = apply_plan(plan)

    assert result.error is not None and "page002.jpg" in result.error
    assert book.read_bytes() == before
    assert not list(tmp_path.glob("*.bak"))


def test_one_unexpected_failure_does_not_lose_the_report(library: Path, monkeypatch) -> None:
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    plans = build_plans(groups, {a.path: a.page_count for a in archives})
    real_apply = remover.apply_plan
    seen: list[Path] = []

    def flaky(plan, **kwargs):
        seen.append(plan.archive)
        if len(seen) == 2:
            raise ValueError("boom")
        return real_apply(plan, **kwargs)

    monkeypatch.setattr(remover, "apply_plan", flaky)

    report = apply_removals(plans)

    assert len(report.results) == 3
    assert len(report.succeeded) == 2
    assert len(report.failed) == 1 and "boom" in report.failed[0].error


def test_external_tools_never_wait_on_stdin() -> None:
    """A password prompt from 7-Zip/UnRAR must hit EOF, not hang the scan.

    The parent's stdin is swapped for an open pipe that never delivers data -
    what a terminal looks like to a child that asks a question.
    """
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        result = extern._run(
            [sys.executable, "-c", "import sys; sys.exit(0 if sys.stdin.read() == '' else 1)"],
            timeout=10,
        )
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)

    assert result.returncode == 0


def test_output_dir_equal_to_the_source_folder_is_refused(tmp_path: Path) -> None:
    """Writing "cleaned copies" over the originals would skip the backup step."""
    book = write_archive(tmp_path / "book.cbz", [make_page(seed=i) for i in range(3)])
    before = book.read_bytes()
    plan = _plan(book, {"page002.jpg"}, 3)

    result = apply_plan(plan, output_dir=tmp_path)

    assert result.error is not None and "overwrite" in result.error
    assert book.read_bytes() == before


def test_find_archives_skips_appledouble_stubs_and_leftover_temp_files(tmp_path: Path) -> None:
    """macOS drops a "._" stub beside every file on exFAT/SMB volumes."""
    real = write_archive(tmp_path / "Book.cbz", [make_page(seed=1), make_page(seed=2)])
    (tmp_path / "._Book.cbz").write_bytes(bytes([0, 5, 22, 7]) + b" appledouble")
    (tmp_path / f"{TEMP_PREFIX}abc123.cbz").write_bytes(b"partial write")

    assert find_archives([tmp_path]) == [real.resolve()]
    assert find_archives([tmp_path / "._Book.cbz"]) == []


def test_rebuilt_archive_keeps_the_original_permissions(tmp_path: Path) -> None:
    """mkstemp makes 0600 files; a media server running as someone else needs more."""
    book = write_archive(tmp_path / "book.cbz", [make_page(seed=i) for i in range(3)])
    os.chmod(book, 0o444)
    expected = stat.S_IMODE(book.stat().st_mode)
    plan = _plan(book, {"page002.jpg"}, 3)

    try:
        result = apply_plan(plan)
        assert result.ok, result.error
        assert stat.S_IMODE(book.stat().st_mode) == expected
    finally:
        for leftover in tmp_path.iterdir():
            os.chmod(leftover, stat.S_IWRITE | stat.S_IREAD)


def _zip_with_duplicate_entry(path: Path) -> Path:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # zipfile warns about the repeat
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("page001.jpg", make_page(seed=1))
            zf.writestr("page002.jpg", make_page(seed=2))
            zf.writestr("page002.jpg", make_page(seed=3))  # same name again
            zf.writestr("page003.jpg", make_page(seed=4))
    return path


def test_zip_with_a_repeated_entry_name_scans_through_the_cache(tmp_path: Path) -> None:
    """The cache keys pages on (archive, name), so a repeat used to fail the whole book."""
    book = _zip_with_duplicate_entry(tmp_path / "dup.cbz")
    cache = HashCache(tmp_path / "cache.sqlite")
    try:
        info = scan_archives([book], cache=cache)[0]
    finally:
        cache.close()

    assert info.error is None
    assert info.page_count == 3


def test_removal_works_on_a_zip_with_a_repeated_entry_name(tmp_path: Path) -> None:
    book = _zip_with_duplicate_entry(tmp_path / "dup.cbz")
    plan = _plan(book, {"page003.jpg"}, 3)

    result = apply_plan(plan)

    assert result.ok, result.error
    assert scan_archive(book).page_count == 2


def test_removal_refuses_when_pages_were_swapped_since_the_scan(library: Path) -> None:
    """Names alone are not identity: a re-pack can leave a story page under an ad's name."""
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    plans = build_plans(groups, {a.path: a.page_count for a in archives})

    # Another tool re-packs Book 01 so page002 is now a real story page.
    book = library / "Book 01.cbz"
    story = [make_page(seed=100 + i) for i in range(4)]
    write_archive(book, [story[0], story[1], make_page(seed=9999), *story[2:]])
    before = book.read_bytes()

    report = apply_removals(plans)

    by_name = {r.archive.name: r for r in report.results}
    assert by_name["Book 01.cbz"].error is not None
    assert "changed since scan" in by_name["Book 01.cbz"].error
    assert book.read_bytes() == before
    assert not (library / "Book 01.cbz.bak").exists()
    # The untouched books were still cleaned.
    assert by_name["Book 02.cbz"].ok and by_name["Book 03.cbz"].ok


def test_output_dir_mirrors_folders_so_same_named_books_do_not_collide(tmp_path: Path) -> None:
    """Two series that both have a "Vol 01.cbz" used to fight over one flat folder."""
    root = tmp_path / "library"
    ad = make_page(seed=9999)
    for s_index, series in enumerate(("Batman", "Superman")):
        for vol in (1, 2):
            story = [make_page(seed=s_index * 1000 + vol * 10 + i) for i in range(4)]
            write_archive(root / series / f"Vol {vol:02d}.cbz", [story[0], ad, *story[1:]])
    archives = scan_archives(find_archives([root]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    out = tmp_path / "cleaned"

    report = apply_removals(
        build_plans(groups, {a.path: a.page_count for a in archives}), output_dir=out
    )

    assert not report.failed, [r.error for r in report.failed]
    produced = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.cbz"))
    assert produced == [
        "Batman/Vol 01.cbz", "Batman/Vol 02.cbz",
        "Superman/Vol 01.cbz", "Superman/Vol 02.cbz",
    ]
    assert all(scan_archive(out / name).page_count == 4 for name in produced)


def test_dry_run_creates_no_output_folders(tmp_path: Path) -> None:
    book = write_archive(tmp_path / "src" / "book.cbz", [make_page(seed=i) for i in range(3)])
    plan = _plan(book, {"page002.jpg"}, 3)
    out = tmp_path / "cleaned" / "deep"

    result = apply_removals([plan], output_dir=out, dry_run=True).results[0]

    assert result.ok
    assert not (tmp_path / "cleaned").exists()


def test_unusable_output_folder_is_reported_plainly(tmp_path: Path) -> None:
    book = write_archive(tmp_path / "book.cbz", [make_page(seed=i) for i in range(3)])
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("in the way")
    plan = _plan(book, {"page002.jpg"}, 3)

    result = apply_removals([plan], output_dir=blocker / "sub").results[0]

    assert result.error is not None and result.error.startswith("cannot write to")
    assert "unexpected" not in result.error
    assert scan_archive(book).page_count == 3


# -- position and review ---------------------------------------------------


def _books_with_ad_at(root: Path, position: int, *, pages: int = 10, books: int = 2) -> Path:
    ad = make_page(seed=7777)
    for number in range(books):
        story = [make_page(seed=number * 1000 + position * 50 + i) for i in range(pages - 1)]
        story.insert(position, ad)
        write_archive(root / f"B{number}.cbz", story)
    return root


def test_near_edge_counts_three_pages_in_from_either_end() -> None:
    from comiccleaner.core.grouping import near_edge

    def page(index: int):
        return PageEntry(
            archive=Path("x.cbz"), name="p", index=index, size=0, width=0, height=0,
            content_sha="", dhash=0,
        )

    assert [near_edge(page(i), 10) for i in range(10)] == [
        True, True, True, False, False, False, False, True, True, True,
    ]
    assert near_edge(page(5), 0)  # unknown length is never called mid-book


def test_a_match_that_only_sits_mid_book_is_flagged_and_not_safe(tmp_path: Path) -> None:
    from comiccleaner.core.grouping import is_safe, review_warnings

    archives = scan_archives(find_archives([_books_with_ad_at(tmp_path / "mid", 5)]))
    [group] = build_groups(archives, GroupingOptions())

    assert group.edge_share == 0
    assert any("mid-book" in w for w in review_warnings(group))
    assert not is_safe(group)


def test_an_identical_page_at_the_back_of_several_books_is_safe(tmp_path: Path) -> None:
    from comiccleaner.core.grouping import is_safe, review_warnings

    archives = scan_archives(find_archives([_books_with_ad_at(tmp_path / "end", 9)]))
    [group] = build_groups(archives, GroupingOptions())

    assert group.edge_share == 1
    assert review_warnings(group) == []
    assert is_safe(group)
    assert not is_safe(group, min_books=3)  # the book minimum still applies


def test_similar_groups_are_never_safe_without_being_known(library: Path) -> None:
    from comiccleaner.core.grouping import is_safe

    [group] = build_groups(scan_archives(find_archives([library])), GroupingOptions(threshold=8))

    assert group.kind is MatchKind.SIMILAR
    assert not is_safe(group)
    group.known = True
    assert is_safe(group)


def test_known_junk_in_one_book_is_not_warned_about_being_in_one_book(tmp_path: Path) -> None:
    from comiccleaner.core.grouping import review_warnings

    archives = scan_archives(find_archives([_books_with_ad_at(tmp_path / "one", 9, books=1)]))
    ad_hash = archives[0].pages[9].dhash
    [group] = build_groups(archives, GroupingOptions(), known={ad_hash})

    assert not any("one book" in w for w in review_warnings(group))


def test_edge_matches_rank_above_mid_book_ones_with_the_same_spread(tmp_path: Path) -> None:
    from comiccleaner.core.grouping import sort_groups

    ad_mid, ad_end = make_page(seed=8001), make_page(seed=8002)
    for number in range(2):
        story = [make_page(seed=number * 100 + i) for i in range(10)]
        story.insert(5, ad_mid)
        story.append(ad_end)
        write_archive(tmp_path / f"B{number}.cbz", story)
    archives = scan_archives(find_archives([tmp_path]))

    groups = build_groups(archives, GroupingOptions())
    assert [g.edge_share for g in groups] == [1.0, 0.0]
    assert [g.edge_share for g in sort_groups(groups, "books")] == [1.0, 0.0]


# -- moved libraries -------------------------------------------------------


def _no_decoding(monkeypatch) -> None:
    from comiccleaner.core import scanner

    def fail(*a, **k):
        raise AssertionError("a book the cache already knew was hashed again")

    monkeypatch.setattr(scanner, "digest_image", fail)


def test_a_moved_library_keeps_its_cached_hashes(
    library: Path, tmp_path: Path, monkeypatch
) -> None:
    import shutil

    cache = HashCache(tmp_path / "cache.sqlite")
    before = scan_archive(library / "Book 01.cbz", cache)
    moved = tmp_path / "elsewhere" / "library"
    shutil.move(str(library), str(moved))

    _no_decoding(monkeypatch)
    after = scan_archive(moved / "Book 01.cbz", cache)

    assert [p.dhash for p in after.pages] == [p.dhash for p in before.pages]
    assert all(p.archive == moved / "Book 01.cbz" for p in after.pages)
    # A move re-keys the entry rather than leaving the old path behind.
    rows = cache.conn.execute("SELECT path FROM archives").fetchall()
    assert [Path(r[0]).parent.name for r in rows] == ["library"]
    assert str(moved.resolve()) in rows[0][0]
    cache.close()


def test_a_copied_book_reuses_the_hashes_and_keeps_the_original(
    library: Path, tmp_path: Path, monkeypatch
) -> None:
    import shutil

    cache = HashCache(tmp_path / "cache.sqlite")
    scan_archive(library / "Book 01.cbz", cache)
    copy = tmp_path / "copy" / "Book 01.cbz"
    copy.parent.mkdir()
    shutil.copy2(library / "Book 01.cbz", copy)  # keeps the modification time

    _no_decoding(monkeypatch)
    scan_archive(copy, cache)
    scan_archive(library / "Book 01.cbz", cache)  # the original is still cached too

    assert cache.conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 2
    cache.close()


def test_a_different_book_with_the_same_size_is_not_mistaken_for_it(
    library: Path, tmp_path: Path
) -> None:
    """Name, size and nanosecond time must all agree; here the name does not."""
    import os
    import shutil

    cache = HashCache(tmp_path / "cache.sqlite")
    scan_archive(library / "Book 01.cbz", cache)
    other = tmp_path / "other" / "Totally Different.cbz"
    other.parent.mkdir()
    shutil.copy2(library / "Book 02.cbz", other)
    source = (library / "Book 01.cbz").stat()
    os.utime(other, ns=(source.st_atime_ns, source.st_mtime_ns))

    assert cache.get(other, other.stat().st_size, other.stat().st_mtime_ns) is None
    cache.close()


# -- multi-index hashing ---------------------------------------------------


def _clustered_hashes(seed: int = 7) -> np.ndarray:
    """Random 64-bit hashes plus near copies of some, a few bits apart.

    Copies are made at every distance up to 8 bits, so a threshold lands right
    on the edge of some pairs, which is where an inexact index would slip.
    """
    rng = np.random.default_rng(seed)
    bases = rng.integers(0, 2**64, size=400, dtype=np.uint64)
    near = []
    for index, base in enumerate(bases[:200]):
        flips = rng.choice(64, size=1 + index % 8, replace=False)
        mask = sum(1 << int(bit) for bit in flips)
        near.append(int(base) ^ mask)
    return np.unique(np.concatenate([bases, np.array(near, dtype=np.uint64)]))


def _as_sets(clusters: list[list[int]]) -> set[frozenset[int]]:
    return {frozenset(c) for c in clusters}


def test_multi_index_hashing_finds_exactly_what_brute_force_does(monkeypatch) -> None:
    hashes = _clustered_hashes()
    for threshold in (1, 2, 3, 4, 6, 8):
        monkeypatch.setattr(grouping, "_BRUTE_FORCE_LIMIT", 10**9)
        brute = grouping._cluster_by_distance(hashes, threshold)
        # Below the set's size, so the banded index is what runs.
        monkeypatch.setattr(grouping, "_BRUTE_FORCE_LIMIT", 10)
        indexed = grouping._cluster_by_distance(hashes, threshold)

        assert _as_sets(indexed) == _as_sets(brute), f"threshold {threshold}"
        assert any(len(c) > 1 for c in brute)  # the comparison had matches to find


# -- archives that point outside themselves --------------------------------


def _fake_rar(path: Path) -> Path:
    """Enough of a RAR header for the archive to be handed to an extractor."""
    path.write_bytes(b"Rar!\x1a\x07\x00" + bytes(32))
    return path


def _extracting(monkeypatch, build) -> None:
    """Stand in for 7-Zip/UnRAR: `build` lays out the extracted tree."""
    from comiccleaner.core import archive as archive_module

    monkeypatch.setattr(
        archive_module, "extract_all", lambda _src, dest, kind: build(Path(dest))
    )


def _outside(tmp_path: Path) -> tuple[Path, bytes]:
    secret = make_page(seed=31337)
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    (folder / "secret.jpg").write_bytes(secret)
    return folder, secret


def _assert_nothing_from_outside(book: Path, secret: bytes, folder: Path) -> None:
    with ComicArchive(book) as arc:
        names = arc.entry_names()
        assert names == ["page001.jpg"]
        assert all(arc.read(n) != secret for n in names)
        for linked in ("page002.jpg", "linked/secret.jpg"):
            with pytest.raises(ArchiveError):
                arc.read(linked)
    info = scan_archive(book)
    assert content_digest(secret) not in {p.content_sha for p in info.pages}
    # Cleaning up the extract must not have followed the link either.
    assert (folder / "secret.jpg").read_bytes() == secret


def test_extracted_symlinks_are_never_followed(tmp_path: Path, monkeypatch) -> None:
    folder, secret = _outside(tmp_path)
    try:
        os.symlink(folder / "secret.jpg", tmp_path / "probe")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this account cannot create symlinks here: {exc}")

    def build(dest: Path) -> None:
        (dest / "page001.jpg").write_bytes(make_page(seed=1))
        os.symlink(folder / "secret.jpg", dest / "page002.jpg")
        os.symlink(folder, dest / "linked", target_is_directory=True)

    _extracting(monkeypatch, build)
    _assert_nothing_from_outside(_fake_rar(tmp_path / "book.cbr"), secret, folder)


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_extracted_junctions_are_never_followed(tmp_path: Path, monkeypatch) -> None:
    """A junction needs no special privilege, unlike a Windows symlink."""
    import _winapi

    folder, secret = _outside(tmp_path)

    def build(dest: Path) -> None:
        (dest / "page001.jpg").write_bytes(make_page(seed=1))
        _winapi.CreateJunction(str(folder), str(dest / "linked"))

    _extracting(monkeypatch, build)
    _assert_nothing_from_outside(_fake_rar(tmp_path / "book.cbr"), secret, folder)


# -- removal safety --------------------------------------------------------


def test_removal_streams_the_kept_pages_rather_than_reading_them_whole(
    library: Path, monkeypatch
) -> None:
    archives = scan_archives(find_archives([library]))
    groups = _mark_all_for_deletion(build_groups(archives, GroupingOptions(threshold=8)))
    plans = build_plans(groups, {a.path: a.page_count for a in archives})
    real_read = ComicArchive.read
    read_whole: list[str] = []

    def tracking(self, name):
        read_whole.append(name)
        return real_read(self, name)

    monkeypatch.setattr(ComicArchive, "read", tracking)

    report = apply_removals(plans)

    assert not report.failed, [r.error for r in report.failed]
    # Only the page being checked before removal, and the small XML being edited.
    assert set(read_whole) == {"page002.jpg", "ComicInfo.xml"}
    assert all(scan_archive(a.path).page_count == 4 for a in archives)


@pytest.mark.parametrize("expected", [{}, {"page003.jpg": ""}])
def test_a_marked_page_with_no_scanned_fingerprint_is_never_removed(
    tmp_path: Path, expected: dict
) -> None:
    """Without its hash, a name is the only identity left, and names are not enough."""
    book = write_archive(tmp_path / "book.cbz", [make_page(seed=i) for i in range(5)])
    before = book.read_bytes()
    with ComicArchive(book) as arc:
        shas = {"page002.jpg": content_digest(arc.read("page002.jpg")), **expected}
    plan = RemovalPlan(
        archive=book, remove_names={"page002.jpg", "page003.jpg"}, original_pages=5,
        expected_sha=shas,
    )

    result = apply_plan(plan)

    assert result.error is not None and "page003.jpg" in result.error
    assert result.removed == 0
    assert book.read_bytes() == before  # not even the page that could be verified
    assert not list(tmp_path.glob("*.bak"))


def test_split_by_fraction_is_inclusive_and_distrusts_unknown_lengths() -> None:
    quarter = RemovalPlan(archive=Path("a.cbz"), remove_names={"x"}, original_pages=4)
    half = RemovalPlan(archive=Path("b.cbz"), remove_names={"x", "y"}, original_pages=4)
    unknown = RemovalPlan(archive=Path("c.cbz"), remove_names={"x"}, original_pages=0)

    assert split_by_fraction([quarter, half, unknown]) == ([quarter], [half, unknown])
    assert split_by_fraction([quarter, half], 0.5) == ([quarter, half], [])
    assert DEFAULT_MAX_FRACTION == 0.25


# -- ComicInfo namespaces --------------------------------------------------

_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_ANANSI = "https://anansi-project.github.io/docs/comicinfo/schemas/v2.1"


def _pages_of(xml: bytes) -> tuple[str | None, list[str]]:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)
    local = {el.tag.rsplit("}", 1)[-1]: el for el in root}
    count = local["PageCount"].text if "PageCount" in local else None
    return count, [p.get("Image") for p in local["Pages"]]


def test_comicinfo_in_a_default_namespace_is_updated_and_keeps_it() -> None:
    import xml.etree.ElementTree as ET

    registry = dict(ET._namespace_map)
    original = (
        f'<?xml version="1.0"?><ComicInfo xmlns="{_ANANSI}" xmlns:xsi="{_XSI}">'
        '<Series xsi:nil="false">S</Series><PageCount>4</PageCount><Pages>'
        '<Page Image="0" Type="FrontCover"/><Page Image="1" Type="Advertisement"/>'
        '<Page Image="3"/></Pages></ComicInfo>'
    ).encode()

    updated = update_comicinfo(original, {1}, 3)

    assert _pages_of(updated) == ("3", ["0", "2"])
    text = updated.decode()
    assert f'<ComicInfo xmlns="{_ANANSI}"' in text
    assert f'xmlns:xsi="{_XSI}"' in text and 'xsi:nil="false"' in text
    assert "ns0" not in text
    assert dict(ET._namespace_map) == registry  # nothing leaks to the next book


def test_comicinfo_with_a_prefixed_namespace_gains_page_count_in_it() -> None:
    original = (
        f'<ci:ComicInfo xmlns:ci="{_ANANSI}"><ci:Pages>'
        '<ci:Page Image="0"/><ci:Page Image="2"/></ci:Pages></ci:ComicInfo>'
    ).encode()

    updated = update_comicinfo(original, {1}, 2)

    assert _pages_of(updated) == ("2", ["0", "1"])
    assert "<ci:PageCount>2</ci:PageCount>" in updated.decode()
    assert "ns0" not in updated.decode()


def test_plain_comicinfo_is_written_exactly_as_before() -> None:
    original = (
        b'<?xml version="1.0"?>\n'
        b'<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        b'xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n'
        b"  <Series>S</Series>\n  <PageCount>4</PageCount>\n  <Pages>\n"
        b'    <Page Image="0" Type="FrontCover" />\n'
        b'    <Page Image="2" Type="Advertisement" />\n'
        b'    <Page Image="3" />\n  </Pages>\n</ComicInfo>\n'
    )

    assert update_comicinfo(original, {2}, 3) == (
        b"<?xml version='1.0' encoding='utf-8'?>\n"
        b"<ComicInfo>\n  <Series>S</Series>\n  <PageCount>3</PageCount>\n  <Pages>\n"
        b'    <Page Image="0" Type="FrontCover" />\n    <Page Image="2" />\n'
        b"  </Pages>\n</ComicInfo>"
    )


# -- cached decode failures ------------------------------------------------

_UNREADABLE = b"not an image at all"


def _book_with_a_bad_page(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page001.jpg", make_page(seed=1))
        zf.writestr("page002.jpg", _UNREADABLE)
        zf.writestr("page003.jpg", make_page(seed=3))
    return path


def _count_decodes(monkeypatch, *, now_readable: bool = False) -> list[bytes]:
    """Record every decode; optionally pretend a new codec can read the bad page."""
    from comiccleaner.core import scanner

    seen: list[bytes] = []
    real = scanner.digest_image

    def digest(data: bytes):
        seen.append(data)
        if now_readable and data == _UNREADABLE:
            return real(make_page(seed=2))
        return real(data)

    monkeypatch.setattr(scanner, "digest_image", digest)
    return seen


def test_a_decode_failure_is_retried_only_when_the_codecs_change(
    tmp_path: Path, monkeypatch
) -> None:
    book = _book_with_a_bad_page(tmp_path / "book.cbz")
    db = tmp_path / "cache.sqlite"
    cache = HashCache(db, codecs="pillow-old")
    first = scan_archive(book, cache)
    cache.close()
    assert [p.error is not None for p in first.pages] == [False, True, False]

    # Same Pillow as before: everything, the failure included, comes from the cache.
    decoded = _count_decodes(monkeypatch)
    cache = HashCache(db, codecs="pillow-old")
    assert scan_archive(book, cache).pages[1].error is not None
    cache.close()
    assert decoded == []

    # A different Pillow: only the failed page is decoded again, and now it reads.
    decoded = _count_decodes(monkeypatch, now_readable=True)
    cache = HashCache(db, codecs="pillow-new")
    upgraded = scan_archive(book, cache)
    assert decoded == [_UNREADABLE]
    assert all(p.error is None for p in upgraded.pages)
    assert [p.content_sha for p in upgraded.pages][::2] == [
        p.content_sha for p in first.pages
    ][::2]

    decoded.clear()
    scan_archive(book, cache)  # and the fixed page is now simply cached
    cache.close()
    assert decoded == []


def test_a_failure_that_persists_is_not_retried_again_under_the_same_codecs(
    tmp_path: Path, monkeypatch
) -> None:
    book = _book_with_a_bad_page(tmp_path / "book.cbz")
    db = tmp_path / "cache.sqlite"
    cache = HashCache(db, codecs="a")
    scan_archive(book, cache)
    cache.close()

    decoded = _count_decodes(monkeypatch)
    cache = HashCache(db, codecs="b")
    scan_archive(book, cache)
    scan_archive(book, cache)
    cache.close()

    assert decoded == [_UNREADABLE]  # once for the new codecs, then cached


def test_a_cache_from_before_codec_stamps_opens_and_retries_its_failures(
    tmp_path: Path,
) -> None:
    import sqlite3

    book = _book_with_a_bad_page(tmp_path / "book.cbz")
    stat = book.stat()
    key = str(book.resolve())
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE archives (path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
        "mtime_ns INTEGER NOT NULL, scanned_at REAL NOT NULL DEFAULT 0);"
        "CREATE TABLE pages (path TEXT NOT NULL, name TEXT NOT NULL, idx INTEGER NOT NULL, "
        "size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, "
        "content_sha TEXT NOT NULL, dhash INTEGER NOT NULL, flat INTEGER NOT NULL DEFAULT 0, "
        "error TEXT, PRIMARY KEY (path, name));"
    )
    conn.execute("INSERT INTO archives(path, size, mtime_ns) VALUES (?, ?, ?)",
                 (key, stat.st_size, stat.st_mtime_ns))
    conn.executemany(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (key, "page001.jpg", 0, 10, 400, 600, "a" * 64, 1, 0, None),
            (key, "page002.jpg", 1, 10, 0, 0, "", 0, 0, "could not decode image"),
            (key, "page003.jpg", 2, 10, 400, 600, "c" * 64, 3, 0, None),
        ],
    )
    conn.commit()
    conn.close()

    cache = HashCache(db, codecs="today")
    try:
        cached = cache.get(book, stat.st_size, stat.st_mtime_ns)
        assert cached is not None and len(cached) == 3
        assert cache.stale_errors(book) == {"page002.jpg"}
    finally:
        cache.close()
