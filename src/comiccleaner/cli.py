"""Headless scan and clean, for scripts, schedulers and library post-processing.

Nothing here imports Qt, so it runs on a server with no display or GUI libraries.
It shares the GUI's hash cache and ignore list, but not its settings: every
matching option is a flag with the GUI's default, so a script means the same
thing no matter what someone last chose in the Settings dialog.

    comiccleaner scan  PATH... [matching options] [--json]
    comiccleaner clean PATH... (--all | --group ID...) [--dry-run] [--yes] ...

Exit codes: 0 success, 1 some archives could not be read or cleaned, 2 bad
usage or a refusal (nothing was changed), 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .core.cache import HashCache
from .core.grouping import GroupingOptions, build_groups
from .core.model import ArchiveInfo, Decision, DuplicateGroup, MatchKind
from .core.remover import BackupPolicy, RemovalPlan, RemovalReport, apply_removals, build_plans
from .core.scanner import find_archives, scan_archives
from .paths import cache_path
from .units import human_bytes

log = logging.getLogger(__name__)

COMMANDS = ("scan", "clean")

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_REFUSED = 2
EXIT_INTERRUPTED = 130

# Adverts and credits are a page or three. A plan that would take a large share
# of a book is far more likely to be two copies of the same issue matching each
# other page for page, so it is skipped unless the user raises the limit.
DEFAULT_MAX_FRACTION = 0.25

JSON_VERSION = 1


class _Refusal(Exception):
    """Stop without changing anything, with a message for the user."""


class _Interrupt:
    """First Ctrl+C asks to stop between books; a second one stops at once.

    A removal swaps each book in whole or not at all, so finishing the current
    one is always safe and leaves nothing half-written.
    """

    def __init__(self, err: TextIO) -> None:
        self.requested = False
        self._err = err
        self._previous: Any = None

    def __enter__(self) -> _Interrupt:
        try:
            self._previous = signal.signal(signal.SIGINT, self._handle)
        except ValueError:  # not the main thread (tests); Ctrl+C just raises
            self._previous = None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)

    def _handle(self, signum: int, frame: object) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        print(
            "\nStopping after the current book (Ctrl+C again to stop now)...",
            file=self._err,
            flush=True,
        )


# -- argument parsing ------------------------------------------------------


def _matching_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("matching (defaults match the GUI's)")
    group.add_argument(
        "--threshold", type=int, default=0, metavar="BITS",
        help="Hamming distance for 'similar' (0 = identical images only, the default; "
        "2-6 catches re-encodes).",
    )
    group.add_argument(
        "--min-copies", type=int, default=2, metavar="N",
        help="Occurrences before a group counts (default 2).",
    )
    group.add_argument(
        "--min-books", type=int, default=2, metavar="N",
        help="Books a group must span (default 2; 1 also finds repeats within one book).",
    )
    group.add_argument(
        "--include-first-page", action="store_true",
        help="Also match each book's first page. Off by default: covers repeat legitimately.",
    )
    group.add_argument(
        "--skip-last-page", action="store_true", help="Never match each book's last page."
    )
    group.add_argument(
        "--include-blank", action="store_true",
        help="Include blank and solid-colour pages, which look alike to any perceptual hash.",
    )
    group.add_argument(
        "--exact-only", action="store_true",
        help="Only byte-identical groups, even when --threshold also finds similar ones.",
    )
    group.add_argument(
        "--include-ignored", action="store_true",
        help="Also report pages ignored in the GUI.",
    )


def _common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH",
                        help="Archives or folders (searched recursively).")
    parser.add_argument("--json", action="store_true",
                        help="Write a machine-readable report to stdout.")
    parser.add_argument("-q", "--quiet", action="store_true", help="No progress output.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    parser.add_argument(
        "--cache", type=Path, metavar="FILE",
        help="Hash cache to use (default: the one the GUI uses).",
    )
    parser.add_argument("--no-cache", action="store_true",
                        help="Hash every page afresh and store nothing.")
    parser.add_argument("--workers", type=int, metavar="N",
                        help="Parallel scan threads (default: one per core, up to 8).")
    _matching_options(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comiccleaner",
        description="Find and remove pages that repeat across comic archives, "
        "without the GUI.",
        epilog="Run 'comiccleaner COMMAND --help' for a command's options. "
        "With no command, comiccleaner opens the GUI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    scan = commands.add_parser(
        "scan", help="Report duplicate groups. Changes nothing.",
        description="Hash every page and report the pages that repeat across books. "
        "Nothing on disk is changed.",
    )
    _common_options(scan)
    scan.add_argument("--pages", action="store_true",
                      help="List every copy under each group, not just a sample.")

    clean = commands.add_parser(
        "clean", help="Remove duplicate pages.",
        description="Remove the pages in the chosen groups from every book they appear in. "
        "Each book is rebuilt, verified and only then swapped in, with a backup kept "
        "beside it unless told otherwise.",
    )
    _common_options(clean)
    which = clean.add_mutually_exclusive_group(required=True)
    which.add_argument("--all", action="store_true",
                       help="Remove every group the matching options find.")
    which.add_argument(
        "--group", action="append", default=[], metavar="ID", dest="groups",
        help="Remove this group (an ID from 'scan', or a unique prefix of one). Repeatable.",
    )
    safety = clean.add_argument_group("safety")
    safety.add_argument("-n", "--dry-run", action="store_true",
                        help="Report what would happen and change nothing.")
    safety.add_argument("-y", "--yes", action="store_true",
                        help="Do not ask for confirmation. Required when not run from a terminal.")
    safety.add_argument(
        "--max-fraction", type=float, default=DEFAULT_MAX_FRACTION, metavar="F",
        help="Skip any book that would lose more than this share of its pages "
        f"(default {DEFAULT_MAX_FRACTION}; 1 disables the check).",
    )
    output = clean.add_argument_group("output")
    output.add_argument("--output", type=Path, metavar="DIR",
                        help="Write cleaned copies here and leave the originals untouched.")
    output.add_argument("--backup-dir", type=Path, metavar="DIR",
                        help="Keep backups here instead of beside each original.")
    output.add_argument("--no-backup", action="store_true",
                        help="Replace originals without keeping a backup.")
    output.add_argument(
        "--delete-backups", action="store_true",
        help="Delete the backups once every book in the run has succeeded.",
    )
    output.add_argument("--compress", action="store_true",
                        help="Deflate images when rebuilding (slower, rarely smaller).")
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not 0 <= args.threshold <= 32:
        parser.error("--threshold must be between 0 and 32")
    if args.min_copies < 2:
        parser.error("--min-copies must be at least 2")
    if args.min_books < 1:
        parser.error("--min-books must be at least 1")
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.command == "clean":
        if not 0 < args.max_fraction <= 1:
            parser.error("--max-fraction must be above 0 and at most 1")
        if args.no_backup and (args.backup_dir or args.delete_backups):
            parser.error("--no-backup cannot be combined with --backup-dir or --delete-backups")
        if args.output and (args.backup_dir or args.no_backup or args.delete_backups):
            parser.error("--output leaves the originals alone, so backup options do not apply")


def grouping_options(args: argparse.Namespace) -> GroupingOptions:
    return GroupingOptions(
        threshold=args.threshold,
        min_pages=args.min_copies,
        min_archives=args.min_books,
        include_flat=args.include_blank,
        skip_first_page=not args.include_first_page,
        skip_last_page=args.skip_last_page,
    )


# -- the pipeline ----------------------------------------------------------


class _Progress:
    """A single overwritten status line on a terminal; nothing otherwise."""

    def __init__(self, stream: TextIO, enabled: bool) -> None:
        self._stream = stream
        self._enabled = enabled and stream.isatty()
        self._width = 0

    def __call__(self, done: int, total: int, name: str, verb: str = "Scanning") -> None:
        if not self._enabled:
            return
        line = f"{verb} {done}/{total}: {name}"[:100]
        self._stream.write("\r" + line.ljust(self._width))
        self._stream.flush()
        self._width = len(line)

    def finish(self) -> None:
        if self._enabled and self._width:
            self._stream.write("\r" + " " * self._width + "\r")
            self._stream.flush()
            self._width = 0


def _scan(
    args: argparse.Namespace,
    cache: HashCache | None,
    progress: _Progress,
    should_cancel: Callable[[], bool],
) -> list[ArchiveInfo]:
    missing = [p for p in args.paths if not p.exists()]
    if missing:
        raise _Refusal("No such file or folder: " + ", ".join(str(p) for p in missing))
    paths = find_archives(args.paths)
    if not paths:
        raise _Refusal("No comic archives found in " + ", ".join(str(p) for p in args.paths))
    try:
        return scan_archives(
            paths, cache=cache, workers=args.workers,
            progress=progress, should_cancel=should_cancel,
        )
    finally:
        progress.finish()


def _groups(
    args: argparse.Namespace, archives: list[ArchiveInfo], cache: HashCache | None
) -> list[DuplicateGroup]:
    ignored = set() if (args.include_ignored or cache is None) else cache.ignored_hashes()
    groups = build_groups(archives, grouping_options(args), ignored=ignored)
    if args.exact_only:
        groups = [g for g in groups if g.kind is MatchKind.EXACT]
    return groups


def select_groups(groups: list[DuplicateGroup], wanted: Sequence[str]) -> list[DuplicateGroup]:
    """Resolve IDs (or unique prefixes of them) to groups, refusing any mismatch."""
    chosen: dict[str, DuplicateGroup] = {}
    for raw in wanted:
        prefix = raw.strip().lower()
        matches = [g for g in groups if g.gid.startswith(prefix)] if prefix else []
        if not matches:
            raise _Refusal(
                f"No group {raw!r} with these matching options. Run 'scan' with the same "
                "options to list the current IDs."
            )
        if len(matches) > 1:
            ids = ", ".join(g.gid for g in matches[:5])
            raise _Refusal(f"Group prefix {raw!r} is ambiguous: {ids}")
        chosen[matches[0].gid] = matches[0]
    return list(chosen.values())


def split_by_fraction(
    plans: list[RemovalPlan], max_fraction: float
) -> tuple[list[RemovalPlan], list[RemovalPlan]]:
    """Plans within the limit, and those that would take too much of a book."""
    within, over = [], []
    for plan in plans:
        share = len(plan.remove_names) / plan.original_pages if plan.original_pages else 1.0
        (over if share > max_fraction else within).append(plan)
    return within, over


def _delete_backups(report: RemovalReport) -> tuple[int, int]:
    removed = freed = 0
    for result in report.succeeded:
        if result.backup is None or not result.backup.exists():
            continue
        try:
            size = result.backup.stat().st_size
            result.backup.unlink()
        except OSError as exc:
            log.warning("could not delete backup %s: %s", result.backup, exc)
            continue
        result.backup = None
        removed += 1
        freed += size
    return removed, freed


_UNATTENDED = (
    "Refusing to change files without confirmation. Pass --yes to proceed "
    "unattended, or --dry-run to preview."
)


def _confirm(prompt: str, out: TextIO, stdin: TextIO, err: TextIO) -> bool:
    out.flush()  # the plan being confirmed must be on screen before the question
    err.write(f"{prompt} [y/N] ")
    err.flush()
    answer = stdin.readline()
    if not answer:
        # End of input: nobody is there to answer. Windows reports NUL as a
        # terminal, so this is what `< NUL` or a closed stdin looks like.
        err.write("\n")
        raise _Refusal(_UNATTENDED)
    return answer.strip().lower() in ("y", "yes")


def _display_root(archives: list[ArchiveInfo]) -> Path | None:
    """The folder paths are shown relative to, so plans stay readable."""
    try:
        return Path(os.path.commonpath([a.path.parent for a in archives]))
    except ValueError:  # none, or on different drives
        return None


def _display(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return str(path.relative_to(root))
        except ValueError:
            pass
    return str(path)


# -- reporting -------------------------------------------------------------


def _kind(group: DuplicateGroup) -> str:
    return "identical" if group.kind is MatchKind.EXACT else "similar"


def _library_json(archives: list[ArchiveInfo]) -> dict[str, Any]:
    return {
        "archives": len(archives),
        "pages": sum(a.page_count for a in archives),
        "unreadable": [
            {"path": str(a.path), "error": a.error} for a in archives if a.error
        ],
    }


def _group_json(group: DuplicateGroup) -> dict[str, Any]:
    rep = group.representative
    return {
        "id": group.gid,
        "kind": _kind(group),
        "copies": group.page_count,
        "books": group.archive_count,
        "bytes": group.recoverable_bytes,
        "width": rep.width,
        "height": rep.height,
        "pages": [
            {"archive": str(p.archive), "name": p.name, "page": p.index + 1, "bytes": p.size}
            for p in group.pages
        ],
    }


def _print_library(archives: list[ArchiveInfo], out: TextIO) -> None:
    root = _display_root(archives)
    unreadable = [a for a in archives if a.error]
    pages = sum(a.page_count for a in archives)
    line = f"Scanned {len(archives)} archive(s), {pages} page(s)."
    if unreadable:
        line += f" {len(unreadable)} could not be read:"
    print(line, file=out)
    for archive in unreadable:
        print(f"  {_display(archive.path, root)}: {archive.error}", file=out)


def _print_groups(
    groups: list[DuplicateGroup], out: TextIO, root: Path | None, *, every_page: bool
) -> None:
    if not groups:
        print("No duplicate pages found with these options.", file=out)
        return
    total = sum(g.recoverable_bytes for g in groups)
    print(
        f"{len(groups)} duplicate group(s), {sum(g.page_count for g in groups)} page(s), "
        f"{human_bytes(total)} recoverable:",
        file=out,
    )
    print(file=out)
    print(f"  {'ID':<16}  {'KIND':<9}  {'COPIES':>6}  {'BOOKS':>5}  {'SIZE':>9}  SAMPLE", file=out)
    for group in groups:
        rep = group.representative
        print(
            f"  {group.gid:<16}  {_kind(group):<9}  {group.page_count:>6}  "
            f"{group.archive_count:>5}  {human_bytes(group.recoverable_bytes):>9}  "
            f"{rep.archive.name} p{rep.index + 1}",
            file=out,
        )
        if every_page:
            for page in group.pages:
                print(
                    f"{'':20}{_display(page.archive, root)}  p{page.index + 1}  ({page.name})",
                    file=out,
                )


def _result_json(report: RemovalReport, skipped: list[RemovalPlan]) -> dict[str, Any]:
    return {
        "removed": report.total_removed,
        "bytes_freed": report.total_freed,
        "failed": len(report.failed),
        "cancelled": report.cancelled,
        "results": [
            {
                "archive": str(r.archive),
                "output": str(r.output) if r.output else None,
                "backup": str(r.backup) if r.backup else None,
                "removed": r.removed,
                "bytes_freed": r.bytes_freed,
                "converted": r.converted,
                "error": r.error,
            }
            for r in report.results
        ],
        "skipped_over_limit": [
            {"archive": str(p.archive), "would_remove": len(p.remove_names),
             "pages": p.original_pages}
            for p in skipped
        ],
    }


def _dump_clean(
    out: TextIO,
    args: argparse.Namespace,
    archives: list[ArchiveInfo],
    report: RemovalReport,
    skipped: list[RemovalPlan],
    deleted: tuple[int, int] | None = None,
) -> None:
    payload = {
        "version": JSON_VERSION,
        "dry_run": args.dry_run,
        "library": _library_json(archives),
        **_result_json(report, skipped),
        "backups_deleted": deleted[0] if deleted else 0,
    }
    json.dump(payload, out, indent=2)
    out.write("\n")


def _print_plans(plans: list[RemovalPlan], skipped: list[RemovalPlan],
                 max_fraction: float, out: TextIO, root: Path | None) -> None:
    if plans:
        pages = sum(len(p.remove_names) for p in plans)
        print(f"{pages} page(s) to remove from {len(plans)} archive(s):", file=out)
    for plan in plans:
        print(
            f"  {_display(plan.archive, root)}: removing {len(plan.remove_names)}, "
            f"{plan.remaining_pages} left",
            file=out,
        )
    if skipped:
        if plans:
            print(file=out)
        print(
            f"Skipping {len(skipped)} archive(s) that would lose more than "
            f"{max_fraction:.0%} of their pages (raise --max-fraction to include them):",
            file=out,
        )
        for plan in skipped:
            print(
                f"  {_display(plan.archive, root)}: would remove {len(plan.remove_names)} of "
                f"{plan.original_pages}",
                file=out,
            )


def _print_report(
    report: RemovalReport, dry_run: bool, out: TextIO, root: Path | None
) -> None:
    verb = "Would remove" if dry_run else "Removed"
    freed = "would free" if dry_run else "freed"
    print(
        f"{verb} {report.total_removed} page(s) from {len(report.succeeded)} archive(s), "
        f"{freed} {human_bytes(report.total_freed)}.",
        file=out,
    )
    converted = [r for r in report.succeeded if r.converted]
    if converted:
        tense = "would be" if dry_run else "were"
        print(f"{len(converted)} .cbr/.cb7 file(s) {tense} rebuilt as .cbz.", file=out)
    if report.failed:
        print(f"{len(report.failed)} archive(s) failed and were left untouched:", file=out)
        for result in report.failed:
            print(f"  {_display(result.archive, root)}: {result.error}", file=out)
    if report.cancelled:
        print("Stopped early; the remaining archives were not touched.", file=out)
    if dry_run:
        print("Dry run: nothing was changed.", file=out)


# -- commands --------------------------------------------------------------


def _run_scan(args: argparse.Namespace, archives: list[ArchiveInfo],
              cache: HashCache | None, out: TextIO) -> int:
    groups = _groups(args, archives, cache)
    if args.json:
        payload = {"version": JSON_VERSION, "library": _library_json(archives),
                   "groups": [_group_json(g) for g in groups]}
        json.dump(payload, out, indent=2)
        out.write("\n")
    else:
        _print_library(archives, out)
        print(file=out)
        _print_groups(groups, out, _display_root(archives), every_page=args.pages)
    return EXIT_FAILURES if any(a.error for a in archives) else EXIT_OK


def _run_clean(
    args: argparse.Namespace,
    archives: list[ArchiveInfo],
    cache: HashCache | None,
    out: TextIO,
    err: TextIO,
    stdin: TextIO,
    progress: _Progress,
    interrupt: _Interrupt,
) -> int:
    groups = _groups(args, archives, cache)
    chosen = groups if args.all else select_groups(groups, args.groups)
    for group in chosen:
        group.decision = Decision.DELETE

    counts = {a.path: a.page_count for a in archives}
    plans, skipped = split_by_fraction(build_plans(chosen, counts), args.max_fraction)

    human = err if args.json else out  # keep stdout pure JSON
    root = _display_root(archives)
    if not (args.json and args.quiet):
        _print_library(archives, human)
        print(file=human)
    if not plans:
        if args.json:
            _dump_clean(out, args, archives, RemovalReport(), skipped)
        else:
            print("Nothing to remove.", file=out)
            if skipped:
                _print_plans(plans, skipped, args.max_fraction, out, root)
        return EXIT_FAILURES if any(a.error for a in archives) else EXIT_OK

    _print_plans(plans, skipped, args.max_fraction, human, root)
    print(file=human)

    if not args.dry_run and not args.yes:
        if not stdin.isatty():
            raise _Refusal(_UNATTENDED)
        if not _confirm("Remove these pages?", out, stdin, err):
            raise _Refusal("Cancelled. Nothing was changed.")

    if args.no_backup:
        backup = BackupPolicy(enabled=False)
    else:
        backup = BackupPolicy(enabled=True, directory=args.backup_dir)
    try:
        report = apply_removals(
            plans,
            backup=backup,
            output_dir=args.output,
            dry_run=args.dry_run,
            compress=args.compress,
            progress=lambda d, t, n: progress(d, t, n, "Rewriting"),
            should_cancel=lambda: interrupt.requested,
        )
    finally:
        progress.finish()

    # The rewritten books no longer match what the cache holds for them.
    if cache is not None and not args.dry_run:
        for result in report.succeeded:
            cache.invalidate(result.archive)

    deleted: tuple[int, int] | None = None
    if args.delete_backups and not args.dry_run and not report.failed and not report.cancelled:
        deleted = _delete_backups(report)

    if args.json:
        _dump_clean(out, args, archives, report, skipped, deleted)
    else:
        _print_report(report, args.dry_run, out, root)
        if deleted is not None:
            print(f"Deleted {deleted[0]} backup(s), reclaiming {human_bytes(deleted[1])}.",
                  file=out)

    if report.cancelled:
        return EXIT_INTERRUPTED
    failed = report.failed or any(a.error for a in archives)
    return EXIT_FAILURES if failed else EXIT_OK


def main(
    argv: Sequence[str],
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    inp = stdin or sys.stdin

    parser = build_parser()
    args = parser.parse_args(list(argv))
    _validate(parser, args)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=err,
    )

    cache: HashCache | None = None
    if not args.no_cache:
        try:
            cache = HashCache(args.cache or cache_path())
        except Exception as exc:  # a broken cache slows things down; it is not fatal
            print(f"warning: hash cache unavailable ({exc}); hashing everything",
                  file=err)

    progress = _Progress(err, enabled=not args.quiet)
    try:
        with _Interrupt(err) as interrupt:
            archives = _scan(args, cache, progress, lambda: interrupt.requested)
            if interrupt.requested:
                print("Interrupted during the scan; nothing was changed.", file=err)
                return EXIT_INTERRUPTED
            if args.command == "scan":
                return _run_scan(args, archives, cache, out)
            return _run_clean(args, archives, cache, out, err, inp, progress, interrupt)
    except _Refusal as refusal:
        print(f"comiccleaner: {refusal}", file=err)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("\nInterrupted.", file=err)
        return EXIT_INTERRUPTED
    finally:
        if cache is not None:
            cache.close()
