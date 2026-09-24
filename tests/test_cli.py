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
