"""Known junk: grouping against it, learning it from removals, and sharing it."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from comiccleaner.core.cache import HashCache
from comiccleaner.core.grouping import GroupingOptions, build_groups
from comiccleaner.core.model import Decision
from comiccleaner.core.remover import RemovalReport, RemovalResult, apply_removals, build_plans
from comiccleaner.core.scanner import find_archives, scan_archives
from comiccleaner.core.signatures import (
    FILE_FORMAT,
    SignatureFileError,
    capture_samples,
    export_known,
    import_known,
    learn_from_run,
    read_known,
)

from .conftest import make_page, write_archive


@pytest.fixture
def cache(tmp_path: Path):
    store = HashCache(tmp_path / "cache.sqlite")
    yield store
    store.close()


def _scan(*folders: Path):
    return scan_archives(find_archives(list(folders)))


def _mark(groups):
    for group in groups:
        group.decision = Decision.DELETE
    return groups


# -- grouping --------------------------------------------------------------


def test_a_known_page_in_a_single_new_book_is_still_found(tmp_path, ad_page):
    """The whole point: after cleaning, the advert repeats against nothing."""
    new = tmp_path / "new"
    write_archive(new / "Book 04.cbz", [make_page(seed=1), ad_page, make_page(seed=2)])
    archives = _scan(new)
    ad_hash = next(p.dhash for p in archives[0].pages if p.index == 1)

    assert build_groups(archives, GroupingOptions(min_archives=2)) == []
    [group] = build_groups(archives, GroupingOptions(min_archives=2), known={ad_hash})

    assert group.known
    assert group.page_count == 1
    assert group.pages[0].index == 1


def test_known_matching_follows_the_threshold(library, ad_page):
    """A remembered page catches its re-encodes once the threshold allows it."""
    archives = _scan(library)
    book3 = next(a for a in archives if a.path.name == "Book 03.cbz")
    recompressed_hash = book3.pages[1].dhash
    exact_hash = next(a for a in archives if a.path.name == "Book 01.cbz").pages[1].dhash
    assert recompressed_hash != exact_hash

    loose = build_groups([book3], GroupingOptions(threshold=8, min_archives=2), known={exact_hash})
    tight = build_groups([book3], GroupingOptions(threshold=0, min_archives=2), known={exact_hash})

    assert len(loose) == 1 and loose[0].known
    assert tight == []


def test_ignoring_wins_over_known(library):
    archives = _scan(library)
    groups = build_groups(archives, GroupingOptions(threshold=8))
    hashes = {p.dhash for g in groups for p in g.pages}

    assert build_groups(
        archives, GroupingOptions(threshold=8), ignored=hashes, known=hashes
    ) == []


def test_covers_stay_protected_from_known_junk(tmp_path):
    cover = make_page(seed=42)
    write_archive(tmp_path / "V1.cbz", [cover, make_page(seed=1)])
    archives = _scan(tmp_path)
    cover_hash = archives[0].pages[0].dhash

    assert build_groups(archives, GroupingOptions(skip_first_page=True), known={cover_hash}) == []


# -- learning --------------------------------------------------------------


def test_a_removal_is_remembered_with_a_thumbnail(library, cache):
    archives = _scan(library)
    groups = _mark(build_groups(archives, GroupingOptions(threshold=8)))
    samples = capture_samples(groups)
    report = apply_removals(build_plans(groups, {a.path: a.page_count for a in archives}))

    assert learn_from_run(cache, groups, report, samples) == 1
    [entry] = cache.known_entries()
    assert entry.sid == groups[0].gid
    assert entry.hashes == {p.dhash for p in groups[0].pages}
    assert entry.note == "3 copies in 3 book(s)"
    assert entry.source == "removed"
    assert entry.thumbnail and entry.thumbnail.startswith(b"\x89PNG")


def test_nothing_is_learned_from_books_that_all_failed(library, cache):
    archives = _scan(library)
    groups = _mark(build_groups(archives, GroupingOptions(threshold=8)))
    failed = RemovalReport(
        results=[RemovalResult(archive=a.path, error="boom") for a in archives]
    )

    assert learn_from_run(cache, groups, failed) == 0
    assert cache.known_hashes() == set()


def test_remembering_again_merges_new_variants(cache):
    assert cache.remember("0a", {0x0A}, note="first") is True
    assert cache.remember("0a", {0x0A, 0x0B}, note="again") is False

    [entry] = cache.known_entries()
    assert entry.hashes == {0x0A, 0x0B}
    assert entry.note == "first"


def test_known_junk_survives_the_full_uint64_range(cache):
    cache.remember("ff00000000000000", {0xFF00000000000000})

    assert cache.known_hashes() == {0xFF00000000000000}


def test_forget_and_clear(cache):
    cache.remember("01", {1})
    cache.remember("02", {2})

    cache.forget("01")
    assert cache.known_hashes() == {2}
    cache.clear_known()
    assert cache.known_entries() == []


def test_a_newer_cache_opens_an_older_database(tmp_path):
    """Databases from before known junk existed gain the tables on open."""
    import sqlite3

    db = tmp_path / "old.sqlite"
    sqlite3.connect(db).executescript(
        "CREATE TABLE ignored (gid TEXT PRIMARY KEY, note TEXT, "
        "created_at REAL NOT NULL DEFAULT (strftime('%s','now')));"
    )
    store = HashCache(db)
    store.remember("01", {1})
    assert store.known_hashes() == {1}
    store.close()


# -- sharing ---------------------------------------------------------------


def test_export_then_import_round_trips(tmp_path, cache):
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    cache.remember("000000000000000a", {0x0A, 0x1B}, note="an advert", thumbnail=png)
    exported = tmp_path / "junk.json"

    assert export_known(cache.known_entries(), exported) == 1

    other = HashCache(tmp_path / "other.sqlite")
    result = import_known(other, exported)
    [entry] = other.known_entries()
    other.close()

    assert (result.added, result.merged) == (1, 0)
    assert entry.hashes == {0x0A, 0x1B}
    assert entry.note == "an advert"
    assert entry.source == "imported"
    assert entry.thumbnail == png


def test_importing_the_same_list_twice_merges(tmp_path, cache):
    exported = tmp_path / "junk.json"
    cache.remember("000000000000000a", {0x0A})
    export_known(cache.known_entries(), exported)

    assert import_known(cache, exported).merged == 1
    assert len(cache.known_entries()) == 1


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "payload",
    [
        {"format": "something-else", "version": 1, "entries": []},
        {"format": FILE_FORMAT, "version": 99, "entries": []},
        {"format": FILE_FORMAT, "version": 1, "entries": "nope"},
        {"format": FILE_FORMAT, "version": 1, "entries": [{"hashes": []}]},
        {"format": FILE_FORMAT, "version": 1, "entries": [{"hashes": ["xyz"]}]},
        {"format": FILE_FORMAT, "version": 1, "entries": [{"hashes": ["1" * 17]}]},
        [1, 2, 3],
    ],
    ids=["format", "version", "entries", "empty", "not-hex", "too-long", "not-object"],
)
def test_a_bad_list_is_rejected_and_nothing_is_stored(tmp_path, cache, payload):
    bad = _write(tmp_path / "bad.json", payload)

    with pytest.raises(SignatureFileError):
        import_known(cache, bad)
    assert cache.known_entries() == []


def test_a_list_that_is_not_json_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")

    with pytest.raises(SignatureFileError, match="not valid JSON"):
        read_known(bad)


def test_imported_ids_are_recomputed_not_trusted(tmp_path):
    listing = _write(tmp_path / "l.json", {
        "format": FILE_FORMAT, "version": 1,
        "entries": [{"id": "../../evil", "hashes": ["00000000000000ff", "000000000000000f"]}],
    })

    [entry] = read_known(listing)

    assert entry.sid == "000000000000000f"


def test_a_thumbnail_that_is_not_a_png_is_dropped_but_the_entry_kept(tmp_path):
    listing = _write(tmp_path / "l.json", {
        "format": FILE_FORMAT, "version": 1,
        "entries": [{
            "hashes": ["000000000000000f"],
            "thumbnail": base64.b64encode(b"GIF89a not a png").decode(),
            "note": "n" * 1000,
        }],
    })

    [entry] = read_known(listing)

    assert entry.thumbnail is None
    assert len(entry.note) == 200
