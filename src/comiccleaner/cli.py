"""Headless scan and clean, for scripts, schedulers and library post-processing.

Nothing here imports Qt, so it runs on a server with no display or GUI libraries.
It shares the GUI's hash cache and ignore list, but not its settings: every
matching option is a flag with the GUI's default, so a script means the same
thing no matter what someone last chose in the Settings dialog.

    comiccleaner scan  PATH... [matching options] [--json]
    comiccleaner clean PATH... (--all | --known | --group ID...) [--dry-run] [--yes] ...
    comiccleaner known (list | export FILE | import FILE | forget ID...)
    comiccleaner history (list | restore RUN)
    comiccleaner pack (export FILE | import FILE)

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
from .core.duplicates import DuplicateBooks, find_duplicate_books
from .core.grouping import (
    EDGE_PAGES,
    MAX_EDGE_PAGES,
    MAX_THRESHOLD,
    GroupingOptions,
    build_groups,
    near_edge,
    review_warnings,
)
from .core.history import load_history, record_run, restore, when
from .core.model import ArchiveInfo, Decision, DuplicateGroup, MatchKind
from .core.pack import export_pack, import_pack, read_pack
from .core.planfile import (
    SKIPPED_OVER_LIMIT,
    describe_plan,
    plan_records,
    write_plan_json,
)
from .core.remover import (
    DEFAULT_MAX_FRACTION,
    BackupPolicy,
    RemovalPlan,
    RemovalReport,
    apply_removals,
    build_plans,
    recover_interrupted,
    split_by_fraction,
)
from .core.scanner import scan_archives, survey
from .core.signatures import (
    SignatureFileError,
    capture_samples,
    export_known,
    import_known,
    learn_from_run,
)
from .paths import cache_path
from .units import human_bytes

log = logging.getLogger(__name__)

COMMANDS = ("scan", "clean", "known", "history", "pack")

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_REFUSED = 2
EXIT_INTERRUPTED = 130

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
        f"2-6 catches re-encodes; at most {MAX_THRESHOLD}).",
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
    group.add_argument(
        "--no-known", action="store_true",
        help="Leave out the known-junk list (pages removed before, which are otherwise "
        "found even in a single book).",
    )
    group.add_argument(
        "--edge-window", type=int, default=EDGE_PAGES, metavar="N",
        help=f"Pages from either end of a book that count as 'near the edge' when "
        f"ranking groups and warning about mid-book matches (default {EDGE_PAGES}, "
        f"at most {MAX_EDGE_PAGES}). Does not limit what clean removes; see --edges.",
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
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help="Leave out files whose name, or path relative to a PATH folder, matches "
        "this glob, such as '*sample*' or 'Scans/*'. Repeatable.",
    )
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
    which.add_argument(
        "--known", action="store_true", dest="known_only",
        help="Remove only known junk: pages removed from the library before. The "
        "safest thing to run unattended on new books.",
    )
    safety = clean.add_argument_group("safety")
    safety.add_argument("-n", "--dry-run", action="store_true",
                        help="Report what would happen and change nothing.")
    safety.add_argument("-y", "--yes", action="store_true",
                        help="Do not ask for confirmation. Required when not run from a terminal.")
    safety.add_argument(
        "--edges", type=int, metavar="N",
        help="Only remove copies within N pages of the start or end of a book, where "
        "adverts and credits sit. Copies further in are left alone.",
    )
    safety.add_argument(
        "--max-fraction", type=float, default=DEFAULT_MAX_FRACTION, metavar="F",
        help="Skip any book that would lose more than this share of its pages "
        f"(default {DEFAULT_MAX_FRACTION}; 1 disables the check).",
    )
    safety.add_argument(
        "--plan", type=Path, metavar="FILE",
        help="Write the plan (every book to clean or skip, its pages and share) to this "
        "JSON file before anything else happens. With --dry-run, nothing else is written.",
    )
    safety.add_argument(
        "--quarantine", type=Path, metavar="DIR",
        help="Copy every removed page into DIR, under the book's name, before the book "
        "is replaced. A book whose pages cannot be copied is left untouched.",
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
    output.add_argument(
        "--no-remember", action="store_true",
        help="Do not add what this run removes to the known-junk list.",
    )

    known = commands.add_parser(
        "known", help="List, export, import or forget known junk.",
        description="Known junk is every page removed from the library before. It is "
        "found and removed wherever it turns up again, even in a single book. The list "
        "is shared with the GUI.",
    )
    known.add_argument("--cache", type=Path, metavar="FILE",
                       help="Hash cache to use (default: the one the GUI uses).")
    known.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    actions = known.add_subparsers(dest="action", required=True, metavar="ACTION")
    listing = actions.add_parser("list", help="Show what is remembered.")
    listing.add_argument("--json", action="store_true", help="Machine-readable output.")
    export = actions.add_parser("export", help="Write the list to a file to share.")
    export.add_argument("file", type=Path, metavar="FILE")
    load = actions.add_parser("import", help="Merge in a list someone else exported.")
    load.add_argument("file", type=Path, metavar="FILE")
    forget = actions.add_parser("forget", help="Stop treating these pages as junk.")
    forget.add_argument("ids", nargs="+", metavar="ID", help="IDs from 'known list'.")

    history = commands.add_parser(
        "history", help="List past removals, or restore one from its backups.",
        description="Every removal run, from the GUI or here, is recorded. A run's books "
        "can be put back exactly as they were while their backups still exist.",
    )
    history.add_argument("--cache", type=Path, metavar="FILE",
                         help="Hash cache to use (default: the one the GUI uses).")
    history.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    steps = history.add_subparsers(dest="action", required=True, metavar="ACTION")
    past = steps.add_parser("list", help="Show past runs.")
    past.add_argument("--json", action="store_true", help="Machine-readable output.")
    undo = steps.add_parser("restore", help="Put a run's books back from their backups.")
    undo.add_argument("run", type=int, metavar="RUN", help="A run number from 'history list'.")
    undo.add_argument("-y", "--yes", action="store_true",
                      help="Do not ask for confirmation. Required when not run from a terminal.")

    pack = commands.add_parser(
        "pack", help="Export or import a review pack (known junk and ignored pages).",
        description="A review pack holds the known-junk list and the ignore list in one "
        "file, to move to another machine or share. Packs written by the GUI also carry "
        "its matching settings; those are only applied from the GUI.",
    )
    pack.add_argument("--cache", type=Path, metavar="FILE",
                      help="Hash cache to use (default: the one the GUI uses).")
    pack.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    pack_steps = pack.add_subparsers(dest="action", required=True, metavar="ACTION")
    pack_out = pack_steps.add_parser("export", help="Write a review pack.")
    pack_out.add_argument("file", type=Path, metavar="FILE")
    pack_in = pack_steps.add_parser("import", help="Merge in a review pack.")
    pack_in.add_argument("file", type=Path, metavar="FILE")
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command in ("known", "history", "pack"):
        return
    if not 0 <= args.threshold <= MAX_THRESHOLD:
        parser.error(f"--threshold must be between 0 and {MAX_THRESHOLD}")
    if args.min_copies < 2:
        parser.error("--min-copies must be at least 2")
    if args.min_books < 1:
        parser.error("--min-books must be at least 1")
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be at least 1")
    if not 1 <= args.edge_window <= MAX_EDGE_PAGES:
        parser.error(f"--edge-window must be between 1 and {MAX_EDGE_PAGES}")
    if args.command == "clean":
        if not 0 < args.max_fraction <= 1:
            parser.error("--max-fraction must be above 0 and at most 1")
        if args.edges is not None and args.edges < 1:
            parser.error("--edges must be at least 1")
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
        edge_pages=args.edge_window,
    )


# -- the pipeline ----------------------------------------------------------


class _Progress:
    """A single overwritten status line on a terminal; nothing otherwise."""

    def __init__(self, stream: TextIO, enabled: bool) -> None:
        self.stream = stream
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


def _recover(args: argparse.Namespace, err: TextIO) -> None:
    """Put back books an interrupted run left only as a backup, before reading."""
    for item in recover_interrupted(args.paths, dry_run=args.dry_run):
        if item.restored:
            verb = "Would put back" if args.dry_run else "Put back"
            print(
                f"{verb} {item.original} from its backup: an earlier removal was "
                "interrupted before the cleaned copy was swapped in.",
                file=err,
            )


def _scan(
    args: argparse.Namespace,
    cache: HashCache | None,
    progress: _Progress,
    should_cancel: Callable[[], bool],
) -> list[ArchiveInfo]:
    missing = [p for p in args.paths if not p.exists()]
    if missing:
        raise _Refusal("No such file or folder: " + ", ".join(str(p) for p in missing))
    found = survey(args.paths, exclude=args.exclude)
    if found.pdfs and not args.quiet:
        print(f"Skipping {found.pdfs} PDF file(s): PDF is not supported.", file=progress.stream)
    paths = found.found
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
    known = set() if (args.no_known or cache is None) else cache.known_hashes()
    groups = build_groups(archives, grouping_options(args), ignored=ignored, known=known)
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
        "known": group.known,
        "edge_share": round(group.edge_share, 3),
        "warnings": review_warnings(group),
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


def _duplicates_json(pairs: list[DuplicateBooks]) -> list[dict[str, Any]]:
    return [
        {"smaller": str(p.smaller), "larger": str(p.larger), "shared_pages": p.shared,
         "smaller_pages": p.smaller_pages, "overlap": round(p.overlap, 3)}
        for p in pairs
    ]


def _print_duplicate_books(pairs: list[DuplicateBooks], out: TextIO, root: Path | None) -> None:
    if not pairs:
        return
    print(file=out)
    print(
        f"{len(pairs)} pair(s) of books look like the same issue twice. Their shared "
        "pages are not filler; nothing is removed because of this:",
        file=out,
    )
    for pair in pairs:
        print(
            f"  {_display(pair.smaller, root)}  ~  {_display(pair.larger, root)}  "
            f"({pair.shared} of {pair.smaller_pages} pages shared, {pair.overlap:.0%})",
            file=out,
        )


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
    print(f"  {'ID':<16}  {'KIND':<10}  {'COPIES':>6}  {'BOOKS':>5}  {'SIZE':>9}  SAMPLE", file=out)
    for group in groups:
        rep = group.representative
        print(
            f"  {group.gid:<16}  {_kind(group) + ('*' if group.known else ''):<10}  "
            f"{group.page_count:>6}  "
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
    if any(g.known for g in groups):
        print(file=out)
        print(
            "  * known junk: removed from this library before, so it is found even in "
            "a single book. 'clean --known' removes only these.",
            file=out,
        )


def _result_json(report: RemovalReport, skipped: list[RemovalPlan]) -> dict[str, Any]:
    return {
        "removed": report.total_removed,
        "bytes_freed": report.total_freed,
        "failed": len(report.failed),
        "cancelled": report.cancelled,
        "space_shortfall": [
            {"folder": str(s.folder), "needed": s.needed, "free": s.free}
            for s in report.space_shortfall
        ],
        "results": [
            {
                "archive": str(r.archive),
                "output": str(r.output) if r.output else None,
                "backup": str(r.backup) if r.backup else None,
                "removed": r.removed,
                "bytes_freed": r.bytes_freed,
                "converted": r.converted,
                "quarantined": [str(q) for q in r.quarantined],
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
    remembered: int = 0,
    spared: int = 0,
) -> None:
    payload = {
        "version": JSON_VERSION,
        "dry_run": args.dry_run,
        "library": _library_json(archives),
        **_result_json(report, skipped),
        "backups_deleted": deleted[0] if deleted else 0,
        "remembered": remembered,
        "spared_mid_book": spared,
    }
    json.dump(payload, out, indent=2)
    out.write("\n")


def _print_plans(plans: list[RemovalPlan], skipped: list[RemovalPlan],
                 max_fraction: float, out: TextIO, root: Path | None) -> None:
    if plans:
        pages = sum(len(p.remove_names) for p in plans)
        print(f"{pages} page(s) to remove from {len(plans)} archive(s):", file=out)
    for plan in plans:
        print(f"  {_display(plan.archive, root)}: {describe_plan(plan)}", file=out)
    edges = [p for p in plans if p.takes_cover or p.takes_last_page]
    if edges:
        print(file=out)
        print(
            f"{len(edges)} book(s) would lose their first or last page, which is often "
            "the cover or the back cover:",
            file=out,
        )
        for plan in edges:
            which = " and ".join(
                w for w, hit in (("first", plan.takes_cover), ("last", plan.takes_last_page))
                if hit
            )
            print(f"  {_display(plan.archive, root)}: its {which} page", file=out)
    converting = [p for p in plans if p.converts]
    if converting:
        print(file=out)
        print(
            f"{len(converting)} .cbr/.cb7 book(s) cannot be written in that format; each is "
            "written as a new .cbz beside the original, which is kept as the backup.",
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
    if report.blocked:
        for shortfall in report.space_shortfall:
            print(shortfall.describe(), file=out)
        return
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
    pairs = find_duplicate_books(archives)
    if args.json:
        payload = {"version": JSON_VERSION, "library": _library_json(archives),
                   "groups": [_group_json(g) for g in groups],
                   "duplicate_books": _duplicates_json(pairs)}
        json.dump(payload, out, indent=2)
        out.write("\n")
    else:
        _print_library(archives, out)
        print(file=out)
        root = _display_root(archives)
        _print_groups(groups, out, root, every_page=args.pages)
        _print_duplicate_books(pairs, out, root)
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
    if args.all:
        chosen = groups
    elif args.known_only:
        chosen = [g for g in groups if g.known]
    else:
        chosen = select_groups(groups, args.groups)
    for group in chosen:
        group.decision = Decision.DELETE

    counts = {a.path: a.page_count for a in archives}
    spared = 0
    if args.edges is not None:
        for group in chosen:
            for page in group.pages_to_remove():
                if not near_edge(page, counts.get(page.archive, 0), args.edges):
                    group.kept.add(page.key)
                    spared += 1
    plans, skipped = split_by_fraction(build_plans(chosen, counts), args.max_fraction)

    human = err if args.json else out  # keep stdout pure JSON
    root = _display_root(archives)
    if args.plan is not None:
        records = plan_records(
            plans, [(p, SKIPPED_OVER_LIMIT) for p in skipped], dry_run=args.dry_run
        )
        try:
            write_plan_json(args.plan, records, dry_run=args.dry_run)
        except OSError as exc:
            raise _Refusal(f"Could not write the plan to {args.plan}: {exc}") from exc
        if not (args.json and args.quiet):
            print(f"Wrote the plan for {len(records)} book(s) to {args.plan}.", file=human)
    if not (args.json and args.quiet):
        _print_library(archives, human)
        print(file=human)
        if spared:
            print(
                f"Leaving {spared} cop{'y' if spared == 1 else 'ies'} more than {args.edges} "
                "page(s) from either end of its book alone (--edges).",
                file=human,
            )
            print(file=human)
    if not plans:
        if args.json:
            _dump_clean(out, args, archives, RemovalReport(), skipped, spared=spared)
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

    learn = cache is not None and not args.dry_run and not args.no_remember
    # Thumbnails first: the pages will not exist once the run is over.
    samples = capture_samples(chosen) if learn else {}

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
            quarantine=args.quarantine,
            progress=lambda d, t, n: progress(d, t, n, "Rewriting"),
            should_cancel=lambda: interrupt.requested,
        )
    finally:
        progress.finish()

    # The rewritten books no longer match what the cache holds for them.
    if cache is not None and not args.dry_run:
        for result in report.succeeded:
            cache.invalidate(result.archive)
        record_run(cache, plans, report, "cli")
    remembered = learn_from_run(cache, chosen, report, samples) if learn and cache else 0

    deleted: tuple[int, int] | None = None
    if args.delete_backups and not args.dry_run and not report.failed and not report.cancelled:
        deleted = _delete_backups(report)

    if args.json:
        _dump_clean(out, args, archives, report, skipped, deleted, remembered, spared)
    else:
        _print_report(report, args.dry_run, out, root)
        if deleted is not None:
            print(f"Deleted {deleted[0]} backup(s), reclaiming {human_bytes(deleted[1])}.",
                  file=out)
        if remembered:
            print(f"Remembered {remembered} new page(s) as known junk.", file=out)

    if report.cancelled:
        return EXIT_INTERRUPTED
    failed = report.failed or report.blocked or any(a.error for a in archives)
    return EXIT_FAILURES if failed else EXIT_OK


def _run_known(args: argparse.Namespace, cache: HashCache, out: TextIO) -> int:
    """List, share and prune the known-junk list."""
    if args.action == "list":
        entries = cache.known_entries()
        if args.json:
            json.dump(
                {
                    "version": JSON_VERSION,
                    "known": [
                        {"id": e.sid, "note": e.note, "source": e.source,
                         "hashes": sorted(f"{h:016x}" for h in e.hashes)}
                        for e in entries
                    ],
                },
                out, indent=2,
            )
            out.write("\n")
        elif not entries:
            print("Nothing is remembered yet. Pages are added when they are removed.", file=out)
        else:
            print(f"{len(entries)} remembered page(s):", file=out)
            for entry in entries:
                print(f"  {entry.sid}  {entry.source:<8}  {entry.note}", file=out)
        return EXIT_OK
    if args.action == "export":
        count = export_known(cache.known_entries(), args.file)
        print(f"Wrote {count} remembered page(s) to {args.file}.", file=out)
        return EXIT_OK
    if args.action == "import":
        try:
            result = import_known(cache, args.file)
        except SignatureFileError as exc:
            raise _Refusal(f"{exc}. Nothing was imported.") from exc
        print(f"Added {result.added}; {result.merged} were already known.", file=out)
        return EXIT_OK
    # forget
    known = {e.sid for e in cache.known_entries()}
    for raw in args.ids:
        matches = [sid for sid in known if sid.startswith(raw.lower())]
        if len(matches) != 1:
            raise _Refusal(
                f"No remembered page {raw!r}" if not matches
                else f"Prefix {raw!r} is ambiguous: {', '.join(sorted(matches)[:5])}"
            )
        cache.forget(matches[0])
        print(f"Forgot {matches[0]}.", file=out)
    return EXIT_OK


def _run_history(
    args: argparse.Namespace, cache: HashCache, out: TextIO, err: TextIO, stdin: TextIO
) -> int:
    """List past removal runs, or put a run's books back from their backups."""
    runs = load_history(cache)
    if args.action == "list":
        if args.json:
            json.dump(
                {
                    "version": JSON_VERSION,
                    "runs": [
                        {
                            "id": run.id, "when": when(run.started_at), "source": run.source,
                            "books": [
                                {"archive": str(i.archive), "output": str(i.output),
                                 "backup": str(i.backup) if i.backup else None,
                                 "removed": i.removed, "pages": i.pages,
                                 "restorable": i.restorable, "status": i.status()[1]}
                                for i in run.items
                            ],
                        }
                        for run in runs
                    ],
                },
                out, indent=2,
            )
            out.write("\n")
        elif not runs:
            print("No removals yet.", file=out)
        else:
            for run in runs:
                restorable = sum(1 for i in run.items if i.restorable)
                print(
                    f"Run {run.id}  {when(run.started_at)}  {run.source}: {run.removed} page(s) "
                    f"from {len(run.items)} book(s), {restorable} restorable",
                    file=out,
                )
        return EXIT_OK

    chosen = next((r for r in runs if r.id == args.run), None)
    if chosen is None:
        raise _Refusal(f"No run {args.run}. 'history list' shows the runs.")
    run = chosen
    items = [i for i in run.items if i.restorable]
    for item in run.items:
        if not item.restorable:
            print(f"  skipping {item.archive.name}: {item.status()[1]}", file=out)
    if not items:
        raise _Refusal(f"Nothing in run {run.id} can be restored.")
    for item in items:
        print(f"  {item.archive}: put back, {item.removed} page(s) return", file=out)
    if not args.yes:
        if not stdin.isatty():
            raise _Refusal(_UNATTENDED)
        if not _confirm(f"Restore {len(items)} book(s)?", out, stdin, err):
            raise _Refusal("Cancelled. Nothing was changed.")
    failed = 0
    for item in items:
        error = restore(cache, item)
        if error is not None:
            failed += 1
            print(f"  {item.archive.name}: not restored: {error}", file=out)
    print(f"Restored {len(items) - failed} of {len(items)} book(s).", file=out)
    return EXIT_FAILURES if failed else EXIT_OK


def _run_pack(args: argparse.Namespace, cache: HashCache, out: TextIO) -> int:
    """Write or merge a review pack. Settings stay with the GUI."""
    if args.action == "export":
        pack = export_pack(cache, args.file, None)
        print(
            f"Wrote {len(pack.known)} remembered page(s) and {len(pack.ignored)} ignored "
            f"page(s) to {args.file}. Matching settings are only included when the GUI "
            "exports a pack.",
            file=out,
        )
        return EXIT_OK
    try:
        pack = read_pack(args.file)
    except SignatureFileError as exc:
        raise _Refusal(f"{exc}. Nothing was imported.") from exc
    result = import_pack(cache, pack)
    print(
        f"Known junk: added {result.known.added}, {result.known.merged} already known. "
        f"Ignored: added {result.ignored_added}, {result.ignored_merged} already there.",
        file=out,
    )
    if pack.settings:
        print(
            "The pack also has matching settings. They apply to the GUI only: import the "
            "pack there (File > Import Review Pack) to use them.",
            file=out,
        )
    return EXIT_OK


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
    if not getattr(args, "no_cache", False):
        try:
            cache = HashCache(args.cache or cache_path())
        except Exception as exc:  # a broken cache slows things down; it is not fatal
            print(f"warning: hash cache unavailable ({exc}); hashing everything",
                  file=err)

    if args.command in ("known", "history", "pack"):
        if cache is None:
            print(f"comiccleaner: {args.command} lives in the hash cache, which is "
                  "unavailable", file=err)
            return EXIT_REFUSED
        try:
            if args.command == "history":
                return _run_history(args, cache, out, err, inp)
            if args.command == "pack":
                return _run_pack(args, cache, out)
            return _run_known(args, cache, out)
        except _Refusal as refusal:
            print(f"comiccleaner: {refusal}", file=err)
            return EXIT_REFUSED
        except OSError as exc:
            print(f"comiccleaner: {exc}", file=err)
            return EXIT_REFUSED
        finally:
            cache.close()

    progress = _Progress(err, enabled=not args.quiet)
    try:
        with _Interrupt(err) as interrupt:
            if args.command == "clean":
                _recover(args, err)
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
