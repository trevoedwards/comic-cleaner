"""The headless scan and clean commands."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from comiccleaner import cli
from comiccleaner.core.archive import is_page_name
from comiccleaner.core.cache import HashCache
from comiccleaner.core.grouping import GroupingOptions, build_groups
from comiccleaner.core.scanner import find_archives, scan_archives

from .conftest import make_page, write_archive

ROOT = Path(__file__).resolve().parents[1]


class _Terminal(io.StringIO):
    """stdin as a person at a terminal would supply it."""

    def isatty(self) -> bool:
        return True


def run(*argv: str, stdin: io.StringIO | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), stdout=out, stderr=err, stdin=stdin or io.StringIO())
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def cache_file(tmp_path: Path) -> str:
    return str(tmp_path / "cache.sqlite")


def _pages(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(1 for n in zf.namelist() if is_page_name(n))


def _counts(library: Path) -> dict[str, int]:
    return {p.name: _pages(p) for p in sorted(library.glob("*.cbz"))}


ORIGINAL = {"Book 01.cbz": 5, "Book 02.cbz": 5, "Book 03.cbz": 5}


# -- scan ------------------------------------------------------------------


def test_scan_reports_groups_and_changes_nothing(library, cache_file):
    code, out, _ = run("scan", str(library), "--cache", cache_file, "--threshold", "8")

    assert code == cli.EXIT_OK
    assert "Scanned 3 archive(s), 15 page(s)." in out
    assert "1 duplicate group(s), 3 page(s)" in out
    assert "similar" in out
    assert _counts(library) == ORIGINAL


def test_threshold_is_capped_where_the_gui_caps_it(library, cache_file, capsys):
    from comiccleaner.core.grouping import MAX_THRESHOLD

    assert MAX_THRESHOLD == 16
    code, _, _ = run("scan", str(library), "--cache", cache_file, "--threshold", "16")
    assert code == cli.EXIT_OK
    with pytest.raises(SystemExit) as exited:
        run("scan", str(library), "--cache", cache_file, "--threshold", "17")
    assert exited.value.code == 2
    assert "between 0 and 16" in capsys.readouterr().err


def test_scan_json_matches_the_library_api(library, cache_file):
    code, out, _ = run("scan", str(library), "--cache", cache_file, "--json")

    report = json.loads(out)
    expected = build_groups(scan_archives(find_archives([library])), GroupingOptions())
    assert code == cli.EXIT_OK
    assert report["version"] == cli.JSON_VERSION
    assert report["library"] == {"archives": 3, "pages": 15, "unreadable": []}
    assert [g["id"] for g in report["groups"]] == [g.gid for g in expected]
    group = report["groups"][0]
    assert group["kind"] == "identical"
    assert group["copies"] == 2 and group["books"] == 2
    assert {Path(p["archive"]).name for p in group["pages"]} == {"Book 01.cbz", "Book 02.cbz"}
    assert all(p["page"] == 2 for p in group["pages"])  # 1-based, as a reader counts


def test_scan_lists_every_copy_when_asked(library, cache_file):
    _, out, _ = run("scan", str(library), "--cache", cache_file, "--threshold", "8", "--pages")

    assert out.count("p2  (page002.jpg)") == 3


def test_exact_only_drops_similar_groups(library, cache_file):
    _, out, _ = run(
        "scan", str(library), "--cache", cache_file, "--threshold", "8",
        "--exact-only", "--json",
    )

    assert json.loads(out)["groups"] == []


def test_an_unreadable_archive_is_reported_with_a_failing_exit_code(library, cache_file):
    (library / "Broken.cbz").write_bytes(b"not a zip")

    code, out, _ = run("scan", str(library), "--cache", cache_file)

    assert code == cli.EXIT_FAILURES
    assert "1 could not be read" in out
    assert "Broken.cbz" in out


def test_a_missing_path_is_refused(tmp_path, cache_file):
    code, _, err = run("scan", str(tmp_path / "nope"), "--cache", cache_file)

    assert code == cli.EXIT_REFUSED
    assert "No such file or folder" in err


def test_a_folder_with_no_comics_is_refused(tmp_path, cache_file):
    (tmp_path / "empty").mkdir()

    code, _, err = run("scan", str(tmp_path / "empty"), "--cache", cache_file)

    assert code == cli.EXIT_REFUSED
    assert "No comic archives found" in err


def test_pages_ignored_in_the_gui_are_hidden_unless_asked_for(library, cache_file):
    groups = build_groups(scan_archives(find_archives([library])), GroupingOptions())
    cache = HashCache(Path(cache_file))
    cache.ignore(groups[0].gid, {p.dhash for p in groups[0].pages})
    cache.close()

    _, hidden, _ = run("scan", str(library), "--cache", cache_file, "--json")
    _, shown, _ = run("scan", str(library), "--cache", cache_file, "--json", "--include-ignored")

    assert json.loads(hidden)["groups"] == []
    assert len(json.loads(shown)["groups"]) == 1


def test_the_cache_is_used_across_runs(library, cache_file, monkeypatch):
    run("scan", str(library), "--cache", cache_file)

    from comiccleaner.core import scanner

    def fail(*a, **k):
        raise AssertionError("an unchanged book was hashed again")

    monkeypatch.setattr(scanner, "digest_image", fail)
    code, _, _ = run("scan", str(library), "--cache", cache_file)
    assert code == cli.EXIT_OK


# -- clean: what gets chosen -------------------------------------------------


def test_clean_needs_to_be_told_what_to_remove(library, cache_file):
    with pytest.raises(SystemExit) as exited:
        run("clean", str(library), "--cache", cache_file)
    assert exited.value.code == 2


def test_clean_all_removes_the_advert_and_keeps_backups(library, cache_file):
    code, out, _ = run(
        "clean", str(library), "--cache", cache_file, "--threshold", "8", "--all", "--yes"
    )

    assert code == cli.EXIT_OK
    assert "Removed 3 page(s) from 3 archive(s)" in out
    assert _counts(library) == dict.fromkeys(ORIGINAL, 4)
    assert sorted(p.name for p in library.glob("*.bak")) == [f"{n}.bak" for n in ORIGINAL]


def test_clean_a_group_by_id_prefix(library, cache_file):
    _, listing, _ = run("scan", str(library), "--cache", cache_file, "--json")
    gid = json.loads(listing)["groups"][0]["id"]

    code, _, _ = run("clean", str(library), "--cache", cache_file, "--group", gid[:6], "--yes")

    assert code == cli.EXIT_OK
    # Threshold 0: only the two byte-identical copies go.
    assert _counts(library) == {"Book 01.cbz": 4, "Book 02.cbz": 4, "Book 03.cbz": 5}


def test_an_unknown_group_is_refused_and_nothing_changes(library, cache_file):
    code, _, err = run("clean", str(library), "--cache", cache_file, "--group", "ffff", "--yes")

    assert code == cli.EXIT_REFUSED
    assert "No group 'ffff'" in err
    assert _counts(library) == ORIGINAL


def test_select_groups_rejects_an_ambiguous_prefix(library):
    groups = build_groups(scan_archives(find_archives([library])), GroupingOptions())
    twin = type(groups[0])(gid=groups[0].gid[:4] + "0" * 12, kind=groups[0].kind, pages=[])
    with pytest.raises(cli._Refusal, match="ambiguous"):
        cli.select_groups([groups[0], twin], [groups[0].gid[:4]])


def test_books_that_would_lose_too_much_are_skipped(tmp_path, cache_file):
    """Two copies of one issue match page for page; stripping both would be wrong."""
    issue = [make_page(seed=i) for i in range(4)]
    library = tmp_path / "dupes"
    write_archive(library / "Issue 1.cbz", issue)
    write_archive(library / "Issue 1 (copy).cbz", [*issue, make_page(seed=99)])

    code, out, _ = run(
        "clean", str(library), "--cache", cache_file, "--all", "--include-first-page",
        "--yes",
    )

    assert code == cli.EXIT_OK
    assert "Nothing to remove." in out
    assert "Skipping 2 archive(s)" in out
    assert _counts(library) == {"Issue 1 (copy).cbz": 5, "Issue 1.cbz": 4}


def test_split_by_fraction_uses_the_limit_inclusively():
    from comiccleaner.core.remover import RemovalPlan

    quarter = RemovalPlan(archive=Path("a.cbz"), remove_names={"x"}, original_pages=4)
    half = RemovalPlan(archive=Path("b.cbz"), remove_names={"x", "y"}, original_pages=4)

    within, over = cli.split_by_fraction([quarter, half], 0.25)

    assert within == [quarter]
    assert over == [half]


# -- clean: confirmation ---------------------------------------------------


def test_unattended_clean_without_yes_is_refused(library, cache_file):
    code, _, err = run("clean", str(library), "--cache", cache_file, "--all")

    assert code == cli.EXIT_REFUSED
    assert "--yes" in err
    assert _counts(library) == ORIGINAL


def test_a_person_can_decline_at_the_prompt(library, cache_file):
    code, _, err = run("clean", str(library), "--cache", cache_file, "--all",
                       stdin=_Terminal("n\n"))

    assert code == cli.EXIT_REFUSED
    assert "Remove these pages? [y/N]" in err
    assert _counts(library) == ORIGINAL


def test_a_person_can_confirm_at_the_prompt(library, cache_file):
    code, _, _ = run("clean", str(library), "--cache", cache_file, "--all",
                     stdin=_Terminal("y\n"))

    assert code == cli.EXIT_OK
    assert _counts(library)["Book 01.cbz"] == 4


def test_end_of_input_at_the_prompt_is_treated_as_unattended(library, cache_file):
    """Windows reports NUL as a terminal, so `< NUL` reaches the prompt."""
    code, _, err = run("clean", str(library), "--cache", cache_file, "--all",
                       stdin=_Terminal(""))

    assert code == cli.EXIT_REFUSED
    assert "--yes" in err
    assert _counts(library) == ORIGINAL


# -- clean: where things go ------------------------------------------------


def test_dry_run_changes_nothing_and_says_so(library, cache_file):
    code, out, err = run(
        "clean", str(library), "--cache", cache_file, "--all", "--dry-run", "--json"
    )

    report = json.loads(out)
    assert code == cli.EXIT_OK
    assert report["dry_run"] is True
    assert report["removed"] == 2
    assert all(r["backup"] is None for r in report["results"])
    assert "2 page(s) to remove" in err  # the human summary stays off stdout
    assert _counts(library) == ORIGINAL
    assert not list(library.glob("*.bak"))


def test_delete_backups_sweeps_them_after_a_clean_run(library, cache_file):
    code, out, _ = run(
        "clean", str(library), "--cache", cache_file, "--all", "--yes", "--delete-backups"
    )

    assert code == cli.EXIT_OK
    assert "Deleted 2 backup(s)" in out
    assert not list(library.glob("*.bak"))


def test_backups_can_go_to_their_own_folder(library, cache_file, tmp_path):
    backups = tmp_path / "backups"

    run("clean", str(library), "--cache", cache_file, "--all", "--yes",
        "--backup-dir", str(backups))

    assert sorted(p.name for p in backups.iterdir()) == ["Book 01.cbz.bak", "Book 02.cbz.bak"]
    assert not list(library.glob("*.bak"))


def test_output_folder_leaves_originals_alone(library, cache_file, tmp_path):
    cleaned = tmp_path / "cleaned"

    code, out, _ = run(
        "clean", str(library), "--cache", cache_file, "--all", "--yes", "--json",
        "--output", str(cleaned),
    )

    assert code == cli.EXIT_OK
    assert _counts(library) == ORIGINAL
    assert _counts(cleaned) == {"Book 01.cbz": 4, "Book 02.cbz": 4}
    assert all(Path(r["output"]).parent == cleaned for r in json.loads(out)["results"])


@pytest.mark.parametrize(
    "extra",
    [
        ["--no-backup", "--backup-dir", "x"],
        ["--no-backup", "--delete-backups"],
        ["--output", "x", "--no-backup"],
        ["--max-fraction", "0"],
        ["--max-fraction", "1.5"],
    ],
)
def test_contradictory_options_are_rejected(library, cache_file, extra):
    with pytest.raises(SystemExit) as exited:
        run("clean", str(library), "--cache", cache_file, "--all", "--yes", *extra)
    assert exited.value.code == 2
    assert _counts(library) == ORIGINAL


def test_a_second_clean_finds_nothing_left(library, cache_file):
    run("clean", str(library), "--cache", cache_file, "--all", "--yes")

    code, out, _ = run("clean", str(library), "--cache", cache_file, "--all", "--yes")

    assert code == cli.EXIT_OK
    assert "Nothing to remove." in out


# -- wiring ----------------------------------------------------------------


def test_the_entry_point_routes_commands_to_the_cli(library, cache_file, capsys):
    from comiccleaner.__main__ import main

    code = main(["scan", str(library), "--cache", cache_file, "--json"])

    assert code == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["library"]["archives"] == 3


def test_the_cli_never_imports_qt(library, tmp_path):
    """It has to run on a server where Qt's GUI libraries are not even installed."""
    script = (
        "import sys\n"
        "from comiccleaner.__main__ import main\n"
        f"code = main(['clean', {str(library)!r}, '--all', '--yes', '--quiet',\n"
        f"             '--cache', {str(tmp_path / 'c.sqlite')!r}])\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('PySide6'))\n"
        "assert not loaded, loaded\n"
        "sys.exit(code)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120
    )

    assert result.returncode == 0, result.stderr
    assert _counts(library)["Book 01.cbz"] == 4


def test_the_cli_shares_the_guis_data_folder():
    pytest.importorskip("PySide6")
    from comiccleaner import paths
    from comiccleaner.gui.settings import data_dir

    assert paths.data_dir() == data_dir()


# -- known junk ------------------------------------------------------------


def _incoming(tmp_path: Path, ad_page: bytes) -> Path:
    folder = tmp_path / "incoming"
    # Five pages, so losing the advert stays inside the default --max-fraction.
    story = [make_page(seed=400 + i) for i in range(4)]
    write_archive(folder / "Book 04.cbz", [story[0], ad_page, *story[1:]])
    return folder


def test_a_clean_is_remembered_and_catches_the_advert_in_a_new_book(
    library, cache_file, tmp_path, ad_page
):
    code, out, _ = run("clean", str(library), "--cache", cache_file, "--all", "--yes")
    assert code == cli.EXIT_OK
    assert "Remembered 1 new page(s) as known junk." in out

    incoming = _incoming(tmp_path, ad_page)
    _, listing, _ = run("scan", str(incoming), "--cache", cache_file, "--json")
    [group] = json.loads(listing)["groups"]
    assert group["known"] is True
    assert group["copies"] == 1  # one book, yet found

    code, _, _ = run("clean", str(incoming), "--cache", cache_file, "--known", "--yes")
    assert code == cli.EXIT_OK
    assert _pages(incoming / "Book 04.cbz") == 4


def test_known_groups_are_starred_in_the_table(library, cache_file, tmp_path, ad_page):
    run("clean", str(library), "--cache", cache_file, "--all", "--yes")

    _, out, _ = run("scan", str(_incoming(tmp_path, ad_page)), "--cache", cache_file)

    assert "identical*" in out
    assert "* known junk" in out


def test_clean_known_leaves_new_repeats_alone(library, cache_file):
    """--known only acts on what was removed before, never on a fresh match."""
    code, out, _ = run("clean", str(library), "--cache", cache_file, "--known", "--yes")

    assert code == cli.EXIT_OK
    assert "Nothing to remove." in out
    assert _counts(library) == ORIGINAL


def test_no_remember_learns_nothing(library, cache_file):
    run("clean", str(library), "--cache", cache_file, "--all", "--yes", "--no-remember")

    code, out, _ = run("known", "--cache", cache_file, "list")
    assert code == cli.EXIT_OK
    assert "Nothing is remembered yet" in out


def test_a_dry_run_learns_nothing(library, cache_file):
    run("clean", str(library), "--cache", cache_file, "--all", "--dry-run")

    _, out, _ = run("known", "--cache", cache_file, "list", "--json")
    assert json.loads(out)["known"] == []


def test_no_known_leaves_the_list_out(library, cache_file, tmp_path, ad_page):
    run("clean", str(library), "--cache", cache_file, "--all", "--yes")

    _, out, _ = run(
        "scan", str(_incoming(tmp_path, ad_page)), "--cache", cache_file, "--json", "--no-known"
    )

    assert json.loads(out)["groups"] == []


def test_known_list_export_import_and_forget(library, cache_file, tmp_path):
    run("clean", str(library), "--cache", cache_file, "--all", "--yes")
    exported = tmp_path / "junk.json"
    other = str(tmp_path / "other.sqlite")

    _, listing, _ = run("known", "--cache", cache_file, "list", "--json")
    [entry] = json.loads(listing)["known"]
    assert entry["source"] == "removed"

    code, out, _ = run("known", "--cache", cache_file, "export", str(exported))
    assert code == cli.EXIT_OK and "Wrote 1" in out

    code, out, _ = run("known", "--cache", other, "import", str(exported))
    assert code == cli.EXIT_OK and "Added 1" in out
    code, out, _ = run("known", "--cache", other, "import", str(exported))
    assert "1 were already known" in out

    code, out, _ = run("known", "--cache", other, "forget", entry["id"][:6])
    assert code == cli.EXIT_OK and f"Forgot {entry['id']}" in out
    _, listing, _ = run("known", "--cache", other, "list", "--json")
    assert json.loads(listing)["known"] == []


def test_a_bad_known_list_is_refused(cache_file, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "nope"}', encoding="utf-8")

    code, _, err = run("known", "--cache", cache_file, "import", str(bad))

    assert code == cli.EXIT_REFUSED
    assert "Nothing was imported" in err


def test_forgetting_an_unknown_id_is_refused(cache_file):
    code, _, err = run("known", "--cache", cache_file, "forget", "abc")

    assert code == cli.EXIT_REFUSED
    assert "No remembered page 'abc'" in err


def test_the_entry_point_routes_known_to_the_cli(cache_file, capsys):
    from comiccleaner.__main__ import main

    assert main(["known", "--cache", cache_file, "list"]) == cli.EXIT_OK
    assert "Nothing is remembered yet" in capsys.readouterr().out


# -- position --------------------------------------------------------------


def _ad_mid_and_at_end(root: Path) -> Path:
    """One book has the advert at the back, the other buries it mid-book."""
    ad = make_page(seed=7777)
    story = [make_page(seed=500 + i) for i in range(9)]
    write_archive(root / "Back.cbz", [*story, ad])
    other = [make_page(seed=600 + i) for i in range(9)]
    write_archive(root / "Middle.cbz", [*other[:5], ad, *other[5:]])
    return root


def test_edges_leaves_mid_book_copies_alone(tmp_path, cache_file):
    library = _ad_mid_and_at_end(tmp_path / "lib")

    code, out, err = run(
        "clean", str(library), "--cache", cache_file, "--all", "--yes", "--edges", "3", "--json"
    )

    assert code == cli.EXIT_OK
    assert json.loads(out)["spared_mid_book"] == 1
    assert "Leaving 1 copy" in err
    assert _counts(library) == {"Back.cbz": 9, "Middle.cbz": 10}


def test_scan_json_reports_position_and_warnings(tmp_path, cache_file):
    library = _ad_mid_and_at_end(tmp_path / "lib")

    _, out, _ = run("scan", str(library), "--cache", cache_file, "--json")
    [group] = json.loads(out)["groups"]

    assert group["edge_share"] == 0.5
    assert group["warnings"] == []  # one copy sits where junk sits, so no mid-book flag


def test_edges_must_be_positive(library, cache_file):
    with pytest.raises(SystemExit) as exited:
        run("clean", str(library), "--cache", cache_file, "--all", "--edges", "0")
    assert exited.value.code == 2


# -- plans, quarantine, excludes and the edge window -----------------------


def test_a_dry_run_can_write_its_plan_and_changes_nothing(library, cache_file, tmp_path):
    plan = tmp_path / "plan.json"
    code, out, _ = run(
        "clean", str(library), "--cache", cache_file, "--all", "--dry-run",
        "--threshold", "8", "--plan", str(plan),
    )

    assert code == cli.EXIT_OK
    assert f"Wrote the plan for 3 book(s) to {plan}" in out
    payload = json.loads(plan.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert sorted(Path(b["archive"]).name for b in payload["books"]) == sorted(ORIGINAL)
    assert all(b["percent"] == 20.0 and b["status"] == "planned" for b in payload["books"])
    assert _counts(library) == ORIGINAL


def test_plan_lines_name_the_share_and_the_pages(library, cache_file):
    _, out, _ = run(
        "clean", str(library), "--cache", cache_file, "--all", "--dry-run", "--threshold", "8"
    )
    assert "Book 01.cbz: removing 1 of 5 (20%), 4 left: page002.jpg" in out


def test_clean_can_quarantine_what_it_removes(library, cache_file, tmp_path):
    quarantine = tmp_path / "removed"
    code, out, _ = run(
        "clean", str(library), "--cache", cache_file, "--all", "--yes", "--json",
        "--quarantine", str(quarantine),
    )

    assert code == cli.EXIT_OK
    results = json.loads(out)["results"]
    assert sorted(Path(r["quarantined"][0]).parent.name for r in results) == [
        "Book 01", "Book 02",
    ]
    assert sorted(p.name for p in quarantine.rglob("*.jpg")) == ["page002.jpg", "page002.jpg"]


def test_exclude_leaves_matching_books_out(library, cache_file):
    code, out, _ = run(
        "scan", str(library), "--cache", cache_file, "--json", "--exclude", "*03*",
    )
    assert code == cli.EXIT_OK
    assert json.loads(out)["library"]["archives"] == 2


def test_the_edge_window_changes_what_counts_as_mid_book(tmp_path, cache_file):
    library = _ad_mid_and_at_end(tmp_path / "lib")
    _, out, _ = run("scan", str(library), "--cache", cache_file, "--json", "--edge-window", "5")
    [group] = json.loads(out)["groups"]
    assert group["edge_share"] == 1.0

    with pytest.raises(SystemExit) as exited:
        run("scan", str(library), "--cache", cache_file, "--edge-window", "11")
    assert exited.value.code == 2


def test_pdfs_are_named_as_unsupported(library, cache_file):
    (library / "Extra.pdf").write_bytes(b"%PDF-1.7")
    code, _, err = run("scan", str(library), "--cache", cache_file)
    assert code == cli.EXIT_OK
    assert "Skipping 1 PDF file(s): PDF is not supported." in err


def _duplicated_issue(root: Path) -> Path:
    story = [make_page(seed=700 + i) for i in range(6)]
    write_archive(root / "Issue 1.cbz", story)
    write_archive(root / "Issue 1 again.cbz", story)
    return root


def test_scan_reports_books_that_are_the_same_issue(tmp_path, cache_file):
    library = _duplicated_issue(tmp_path / "lib")

    _, out, _ = run("scan", str(library), "--cache", cache_file, "--json")
    [pair] = json.loads(out)["duplicate_books"]
    assert {Path(pair["smaller"]).name, Path(pair["larger"]).name} == {
        "Issue 1.cbz", "Issue 1 again.cbz",
    }
    assert pair["overlap"] == 1.0

    _, text, _ = run("scan", str(library), "--cache", cache_file)
    assert "1 pair(s) of books look like the same issue twice" in text
    assert "Issue 1 again.cbz  ~  Issue 1.cbz" in text


def test_clean_says_a_cbr_becomes_a_cbz(tmp_path, cache_file, monkeypatch):
    from comiccleaner.core import remover

    library = tmp_path / "lib"
    ad = make_page(seed=31)
    for number, name in enumerate(("A.cbr", "B.cbz")):
        story = [make_page(seed=number * 10 + i) for i in range(4)]
        write_archive(library / name, [story[0], ad, *story[1:]])
    # A is a zip named .cbr, as plenty are; stand in for a real RAR here.
    monkeypatch.setattr(
        remover.RemovalPlan, "converts", property(lambda self: self.archive.suffix == ".cbr")
    )

    _, out, _ = run("clean", str(library), "--cache", cache_file, "--all", "--dry-run")

    assert "1 .cbr/.cb7 book(s) cannot be written in that format" in out
    assert "the original, which is kept as the backup" in out


# -- review packs ----------------------------------------------------------


def test_pack_export_and_import(tmp_path):
    source = HashCache(tmp_path / "a.sqlite")
    source.remember("00000000000000ab", {0xAB}, note="an advert")
    source.ignore("00000000000000cd", {0xCD}, note="a recap")
    source.close()
    pack = tmp_path / "pack.json"

    code, out, _ = run("pack", "--cache", str(tmp_path / "a.sqlite"), "export", str(pack))
    assert code == cli.EXIT_OK
    assert "1 remembered page(s) and 1 ignored page(s)" in out
    assert json.loads(pack.read_text(encoding="utf-8"))["settings"] is None

    code, out, _ = run("pack", "--cache", str(tmp_path / "b.sqlite"), "import", str(pack))
    assert code == cli.EXIT_OK
    assert "Known junk: added 1" in out and "Ignored: added 1" in out
    target = HashCache(tmp_path / "b.sqlite")
    try:
        assert target.known_hashes() == {0xAB}
        assert target.ignored_hashes() == {0xCD}
    finally:
        target.close()


def test_pack_import_mentions_settings_it_cannot_apply(tmp_path):
    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps({
        "format": "comiccleaner-review-pack", "version": 1,
        "settings": {"threshold": 4}, "known": [], "ignored": [],
    }), encoding="utf-8")
    code, out, _ = run("pack", "--cache", str(tmp_path / "c.sqlite"), "import", str(pack))
    assert code == cli.EXIT_OK
    assert "apply to the GUI only" in out


def test_a_bad_pack_is_refused(tmp_path):
    pack = tmp_path / "pack.json"
    pack.write_text("{}", encoding="utf-8")
    code, _, err = run("pack", "--cache", str(tmp_path / "c.sqlite"), "import", str(pack))
    assert code == cli.EXIT_REFUSED
    assert "Nothing was imported" in err
