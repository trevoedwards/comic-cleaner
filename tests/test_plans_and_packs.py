"""Removal plans on paper, protected folders, quarantine, review packs and pinned runs."""

from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import pytest

from comiccleaner.core.archive import is_page_name
from comiccleaner.core.cache import HashCache
from comiccleaner.core.grouping import GroupingOptions, build_groups
from comiccleaner.core.history import (
    delete_run_backups,
    load_history,
    pinned_backups,
    record_run,
)
from comiccleaner.core.model import Decision, DuplicateGroup
from comiccleaner.core.pack import (
    export_pack,
    import_pack,
    read_pack,
    setting_changes,
)
from comiccleaner.core.planfile import (
    SKIPPED_OVER_LIMIT,
    describe_plan,
    plan_records,
    write_plan_csv,
    write_plan_json,
)
from comiccleaner.core.remover import (
    BackupPolicy,
    RemovalPlan,
    apply_plan,
    apply_removals,
    build_plans,
    find_backups,
    split_protected,
    total_size,
)
from comiccleaner.core.scanner import find_archives, scan_archives
from comiccleaner.core.signatures import (
    SignatureFileError,
    export_known,
    import_known,
    imported_only,
    read_known,
)

from .conftest import make_page, write_archive


def _pages(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return sorted(n for n in zf.namelist() if is_page_name(n))


def _marked(library: Path, **options) -> tuple[list[DuplicateGroup], dict[Path, int]]:
    archives = scan_archives(find_archives([library]))
    groups = build_groups(archives, GroupingOptions(**options))
    for group in groups:
        group.decision = Decision.DELETE
    return groups, {a.path: a.page_count for a in archives}


# -- describing a plan -----------------------------------------------------


def test_a_plan_names_its_share_and_first_pages_in_reading_order():
    plan = RemovalPlan(
        archive=Path("Book.cbz"),
        remove_names={"p10.jpg", "p2.jpg", "p9.jpg", "p11.jpg", "p12.jpg"},
        original_pages=20,
        indices={"p2.jpg": 1, "p9.jpg": 8, "p10.jpg": 9, "p11.jpg": 10, "p12.jpg": 11},
    )
    assert describe_plan(plan) == (
        "removing 5 of 20 (25%), 15 left: p2.jpg, p9.jpg, p10.jpg and 2 more"
    )
    short = RemovalPlan(Path("B.cbz"), {"a.jpg"}, 4, indices={"a.jpg": 2})
    assert describe_plan(short) == "removing 1 of 4 (25%), 3 left: a.jpg"


def test_plans_know_when_they_take_a_cover_or_a_last_page(library):
    groups, counts = _marked(library, threshold=8, skip_first_page=False)
    [plan, *_] = build_plans(groups, counts)
    assert plan.indices  # every marked page's position comes along
    assert not plan.takes_cover and not plan.takes_last_page

    cover = RemovalPlan(Path("x.cbz"), {"a.jpg"}, 5, indices={"a.jpg": 0})
    last = RemovalPlan(Path("x.cbz"), {"e.jpg"}, 5, indices={"e.jpg": 4})
    assert cover.takes_cover and not cover.takes_last_page
    assert last.takes_last_page and not last.takes_cover


def test_a_cbr_plan_says_it_will_become_a_cbz(tmp_path):
    rar = tmp_path / "Book.cbr"
    rar.write_bytes(b"Rar!\x1a\x07\x00" + b"\0" * 32)
    zipped = write_archive(tmp_path / "Other.cbr", [make_page(1)])  # a zip in disguise
    assert RemovalPlan(rar, {"a.jpg"}, 3).converts
    assert not RemovalPlan(zipped, {"a.jpg"}, 3).converts


def test_the_plan_file_lists_every_book_and_changes_nothing(library, tmp_path):
    groups, counts = _marked(library, threshold=8)
    plans = build_plans(groups, counts)
    before = {p.name: p.read_bytes() for p in library.iterdir()}
    records = plan_records(plans[1:], [(plans[0], SKIPPED_OVER_LIMIT)], dry_run=True)

    write_plan_json(tmp_path / "plan.json", records, dry_run=True)
    write_plan_csv(tmp_path / "plan.csv", records)

    payload = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert payload["format"] == "comiccleaner-plan" and payload["dry_run"] is True
    books = payload["books"]
    assert [b["status"] for b in books] == ["planned", "planned", "skipped"]
    assert books[2]["reason"] == SKIPPED_OVER_LIMIT
    assert all(b["pages_removed"] == 1 and b["percent"] == 20.0 for b in books)
    assert all(b["removed_entries"] == ["page002.jpg"] for b in books)
    with (tmp_path / "plan.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3 and rows[0]["dry_run"] == "yes" and rows[0]["percent"] == "20.0"
    assert {p.name: p.read_bytes() for p in library.iterdir()} == before


# -- protected folders -----------------------------------------------------


def test_protected_folders_keep_their_books_out_of_the_plan(tmp_path):
    keep = tmp_path / "Originals"
    plans = [
        RemovalPlan(keep / "a.cbz", {"x"}, 5),
        RemovalPlan(keep / "deep" / "b.cbz", {"x"}, 5),
        RemovalPlan(tmp_path / "Originals copy" / "c.cbz", {"x"}, 5),  # a prefix, not inside
        RemovalPlan(tmp_path / "d.cbz", {"x"}, 5),
    ]
    allowed, protected = split_protected(plans, [str(keep), "  "])
    assert [p.archive.name for p in protected] == ["a.cbz", "b.cbz"]
    assert [p.archive.name for p in allowed] == ["c.cbz", "d.cbz"]
    assert split_protected(plans, []) == (plans, [])


# -- quarantine ------------------------------------------------------------


def test_removed_pages_are_copied_to_the_quarantine_first(library, tmp_path):
    groups, counts = _marked(library)  # identical: books 1 and 2
    quarantine = tmp_path / "quarantine"
    report = apply_removals(build_plans(groups, counts), quarantine=quarantine)

    assert len(report.succeeded) == 2
    for result in report.succeeded:
        [copy] = result.quarantined
        assert copy.parent == quarantine / result.archive.stem
        assert copy.name == "page002.jpg"
        with zipfile.ZipFile(result.backup) as zf:
            assert copy.read_bytes() == zf.read("page002.jpg")
        assert "page002.jpg" not in _pages(result.archive)


def test_quarantine_never_overwrites_an_earlier_copy(library, tmp_path):
    groups, counts = _marked(library)
    quarantine = tmp_path / "q"
    (quarantine / "Book 01").mkdir(parents=True)
    (quarantine / "Book 01" / "page002.jpg").write_bytes(b"older")
    plan = next(p for p in build_plans(groups, counts) if p.archive.name == "Book 01.cbz")

    result = apply_plan(plan, quarantine=quarantine)

    assert result.ok
    assert (quarantine / "Book 01" / "page002.jpg").read_bytes() == b"older"
    assert result.quarantined == [quarantine / "Book 01" / "page002-1.jpg"]


def test_a_quarantine_that_cannot_be_written_fails_the_book_untouched(library, tmp_path):
    groups, counts = _marked(library)
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the quarantine folder should be")
    plan = next(p for p in build_plans(groups, counts) if p.archive.name == "Book 01.cbz")
    before = plan.archive.read_bytes()

    result = apply_plan(plan, quarantine=blocker)

    assert result.error and "could not copy the removed pages" in result.error
    assert result.quarantined == [] and result.removed == 0
    assert plan.archive.read_bytes() == before
    assert not list(library.glob("*.bak"))
    assert not list(library.glob(".comiccleaner-*"))


def test_a_failed_book_takes_back_its_quarantined_copies(library, tmp_path, monkeypatch):
    from comiccleaner.core import remover

    groups, counts = _marked(library)
    plan = next(p for p in build_plans(groups, counts) if p.archive.name == "Book 01.cbz")

    def broken(path, expected):
        raise remover.RemovalError("rebuilt archive is broken")

    monkeypatch.setattr(remover, "_verify_cbz", broken)
    result = apply_plan(plan, quarantine=tmp_path / "q")

    assert result.error == "rebuilt archive is broken"
    assert result.quarantined == []
    assert list((tmp_path / "q").rglob("*.jpg")) == []


def test_a_dry_run_writes_no_quarantine(library, tmp_path):
    groups, counts = _marked(library)
    report = apply_removals(build_plans(groups, counts), dry_run=True, quarantine=tmp_path / "q")
    assert report.succeeded and not (tmp_path / "q").exists()


# -- backups ---------------------------------------------------------------


def test_backups_are_found_and_measured(tmp_path):
    (tmp_path / "Book.cbz.bak").write_bytes(b"x" * 10)
    (tmp_path / "Book.cbz.bak.1").write_bytes(b"x" * 5)
    (tmp_path / "Notes.bak").write_bytes(b"not ours")
    (tmp_path / "Book.cbz").write_bytes(b"live")
    found = find_backups([tmp_path, tmp_path / "missing"])
    assert [p.name for p in found] == ["Book.cbz.bak", "Book.cbz.bak.1"]
    assert total_size(found + [tmp_path / "gone.bak"]) == 15


def test_pinned_runs_keep_their_backups(library, tmp_path):
    cache = HashCache(tmp_path / "c.sqlite")
    try:
        groups, counts = _marked(library)
        plans = build_plans(groups, counts)
        report = apply_removals(plans, backup=BackupPolicy())
        record_run(cache, plans, report, "cli")
        [run] = load_history(cache)
        assert not run.pinned and len(run.backups) == 2

        cache.set_run_pinned(run.id, True)
        [run] = load_history(cache)
        assert run.pinned
        assert pinned_backups([run]) == {b.resolve() for b in run.backups}
        with pytest.raises(ValueError, match="pinned"):
            delete_run_backups(run)
        assert all(b.exists() for b in run.backups)

        cache.set_run_pinned(run.id, False)
        [run] = load_history(cache)
        backups = run.backups
        outcome = delete_run_backups(run)
        assert outcome.deleted == 2 and outcome.freed > 0 and not outcome.errors
        assert not any(b.exists() for b in backups)
        [run] = load_history(cache)
        assert all(i.status()[1] == "the backup has since been deleted" for i in run.items)
    finally:
        cache.close()


# -- tags on known junk ----------------------------------------------------


def test_tags_survive_an_export_and_import(tmp_path):
    source = HashCache(tmp_path / "a.sqlite")
    target = HashCache(tmp_path / "b.sqlite")
    try:
        source.remember("00000000000000aa", {0xAA}, note="credits page")
        source.describe_known("00000000000000aa", note="Group X credits", tags="credits, x")
        export_known(source.known_entries(), tmp_path / "list.json")
        import_known(target, tmp_path / "list.json")
        [entry] = target.known_entries()
        assert entry.note == "Group X credits" and entry.tags == "credits, x"
    finally:
        source.close()
        target.close()


def test_lists_written_before_tags_still_import(tmp_path):
    old = {
        "format": "comiccleaner-known-junk", "version": 1,
        "entries": [{"id": "x", "note": "old", "hashes": ["00000000000000ff"]}],
    }
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    [entry] = read_known(path)
    assert entry.tags == "" and entry.hashes == {0xFF}


def test_only_groups_vouched_for_by_an_imported_list_are_singled_out(library, tmp_path):
    archives = scan_archives(find_archives([library]))
    [group] = build_groups(archives, GroupingOptions(threshold=8))
    hashes = {p.dhash for p in group.pages}
    group.known = True
    group.decision = Decision.DELETE

    assert imported_only([group], set(), 8) == [group]
    assert imported_only([group], hashes, 8) == []  # removed here before: seen already
    group.decision = Decision.KEEP
    assert imported_only([group], set(), 8) == []


# -- review packs ----------------------------------------------------------


def test_a_review_pack_carries_known_junk_ignores_and_settings(tmp_path):
    source = HashCache(tmp_path / "a.sqlite")
    target = HashCache(tmp_path / "b.sqlite")
    try:
        source.remember("0000000000000001", {1, 3}, note="ad", tags="ads")
        source.ignore("0000000000000010", {0x10, 0x11}, note="a recap page")
        pack_path = tmp_path / "pack.json"
        export_pack(source, pack_path, {"threshold": 6, "min_archives": 1, "theme": "dark"})

        pack = read_pack(pack_path)
        assert pack.settings == {"threshold": 6, "min_archives": 1}  # nothing else travels
        result = import_pack(target, pack)
        assert result.known.added == 1 and result.ignored_added == 1

        [known] = target.known_entries()
        assert known.hashes == {1, 3} and known.tags == "ads" and known.source == "imported"
        assert target.ignored_hash_map() == {"0000000000000010": {0x10, 0x11}}
        assert [e.note for e in target.ignored_entries()] == ["a recap page"]

        again = import_pack(target, pack)
        assert again.known.merged == 1 and again.ignored_merged == 1
        assert again.known.added == again.ignored_added == 0
    finally:
        source.close()
        target.close()


def test_a_pack_with_bad_settings_is_refused_whole(tmp_path):
    path = tmp_path / "pack.json"
    for settings in ({"threshold": 99}, {"skip_first_page": "yes"}, ["threshold"]):
        path.write_text(json.dumps({
            "format": "comiccleaner-review-pack", "version": 1, "settings": settings,
            "known": [], "ignored": [],
        }), encoding="utf-8")
        with pytest.raises(SignatureFileError):
            read_pack(path)
    path.write_text(json.dumps({"format": "something-else"}), encoding="utf-8")
    with pytest.raises(SignatureFileError, match="not a Comic Cleaner review pack"):
        read_pack(path)


def test_only_settings_that_differ_are_offered():
    current = {"threshold": 0, "min_archives": 2, "skip_first_page": True}
    proposed = {"threshold": 6, "min_archives": 2}
    assert setting_changes(current, proposed) == [("threshold", 0, 6)]
    assert setting_changes(current, None) == []
