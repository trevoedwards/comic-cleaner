"""End-to-end tests for scan -> group -> remove."""

from __future__ import annotations

import zipfile
from pathlib import Path

from comiccleaner.core import grouping
from comiccleaner.core.archive import ComicArchive, is_page_name, natural_key
from comiccleaner.core.cache import HashCache
from comiccleaner.core.comicinfo import update_comicinfo
from comiccleaner.core.grouping import GroupingOptions, build_groups
from comiccleaner.core.hashing import digest_image, hamming
from comiccleaner.core.model import Decision, MatchKind
from comiccleaner.core.remover import (
    BackupPolicy,
    RemovalPlan,
    apply_plan,
    apply_removals,
    build_plans,
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

    assert len(build_groups(archives, GroupingOptions())) == 1
    assert build_groups(archives, GroupingOptions(skip_first_page=True)) == []


def test_ignored_groups_are_hidden(library: Path) -> None:
    archives = scan_archives(find_archives([library]))
    groups = build_groups(archives, GroupingOptions(threshold=8))
    gid = groups[0].gid

    assert build_groups(archives, GroupingOptions(threshold=8), ignored={gid}) == []


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
    plan = RemovalPlan(
        archive=book, remove_names={"page001.jpg", "page002.jpg"}, original_pages=2
    )

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
    plan = RemovalPlan(archive=book, remove_names={"page002.jpg"}, original_pages=5)

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
