"""Applying removals: rebuild each archive without the marked pages."""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..units import human_bytes
from .archive import (
    ARCHIVE_SUFFIXES,
    SEP_ALT,
    TEMP_PREFIX,
    ArchiveError,
    ComicArchive,
    detect_kind,
    is_page_name,
    natural_key,
    write_cbz,
)
from .comicinfo import find_comicinfo, update_comicinfo
from .hashing import content_digest
from .model import ArchiveKind, DuplicateGroup

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]

# Adverts and credits are a page or three. A plan that would take a large share
# of a book is far more likely to be two copies of the same issue matching each
# other page for page, so it is skipped unless the user raises the limit.
DEFAULT_MAX_FRACTION = 0.25

# Written beside a book for the moment its original has been moved aside and the
# cleaned copy is not yet in its place; see recover_interrupted.
MARKER_SUFFIX = ".pending"

# Characters no file name may hold on Windows, so a quarantined page never
# carries one over from an entry name.
_UNSAFE_CHARS = frozenset('<>:"/|?*' + SEP_ALT)


class RemovalError(RuntimeError):
    pass


@dataclass(slots=True)
class BackupPolicy:
    """Where the original file goes before it is replaced."""

    enabled: bool = True
    # None means alongside the original, as "<name>.bak"; otherwise a mirror folder.
    directory: Path | None = None
    suffix: str = ".bak"


@dataclass(slots=True)
class RemovalPlan:
    """What will happen to one archive."""

    archive: Path
    remove_names: set[str]
    original_pages: int
    # sha256 of each marked page as it was scanned. A page whose bytes no longer
    # match is left alone: another tool may have renumbered or swapped pages
    # since, and a name alone would then delete the wrong image. A marked page
    # with no hash here is never removed, for the same reason.
    expected_sha: dict[str, str] = field(default_factory=dict)
    # Each marked page's 0-based position in the book, as scanned.
    indices: dict[str, int] = field(default_factory=dict)

    @property
    def remaining_pages(self) -> int:
        return self.original_pages - len(self.remove_names)

    @property
    def fraction(self) -> float:
        """Share of the book this plan would remove; unknown length counts as all."""
        if self.original_pages <= 0:
            return 1.0
        return len(self.remove_names) / self.original_pages

    @property
    def ordered_names(self) -> list[str]:
        """The marked pages in reading order."""
        return sorted(self.remove_names, key=lambda n: (self.indices.get(n, 0), natural_key(n)))

    @property
    def takes_cover(self) -> bool:
        """Removes the first page, which is usually the cover."""
        return 0 in self.indices.values()

    @property
    def takes_last_page(self) -> bool:
        return self.original_pages > 0 and self.original_pages - 1 in self.indices.values()

    @property
    def converts(self) -> bool:
        """A .cbr or .cb7, which is written as a new .cbz rather than in place."""
        return detect_kind(self.archive) in (ArchiveKind.RAR, ArchiveKind.SEVENZIP)


@dataclass(slots=True)
class RemovalResult:
    archive: Path
    output: Path | None = None
    backup: Path | None = None
    removed: int = 0
    bytes_freed: int = 0
    converted: bool = False
    skipped: bool = False
    error: str | None = None
    # Copies of the removed pages, when a quarantine folder was given.
    quarantined: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped


@dataclass(slots=True)
class SpaceShortfall:
    """A volume that cannot hold what a run would write to it."""

    folder: Path
    needed: int
    free: int

    def describe(self) -> str:
        return (
            f"Not enough free space on {self.folder}: the run needs "
            f"{human_bytes(self.needed)} and {human_bytes(self.free)} is free "
            f"(short by {human_bytes(self.needed - self.free)}). Nothing was changed."
        )


@dataclass(slots=True)
class RemovalReport:
    results: list[RemovalResult] = field(default_factory=list)
    # Stopped early at the user's request; plans after the last result never ran.
    cancelled: bool = False
    # Set when the run was refused before its first book for want of disk space.
    space_shortfall: list[SpaceShortfall] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.space_shortfall)

    @property
    def succeeded(self) -> list[RemovalResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[RemovalResult]:
        return [r for r in self.results if r.error]

    @property
    def total_removed(self) -> int:
        return sum(r.removed for r in self.succeeded)

    @property
    def total_freed(self) -> int:
        return sum(r.bytes_freed for r in self.succeeded)


def build_plans(
    groups: Iterable[DuplicateGroup], archive_page_counts: dict[Path, int]
) -> list[RemovalPlan]:
    """Collapse group decisions into one plan per affected archive."""
    per_archive: dict[Path, dict[str, str]] = defaultdict(dict)
    positions: dict[Path, dict[str, int]] = defaultdict(dict)
    for group in groups:
        for page in group.pages_to_remove():
            per_archive[page.archive][page.name] = page.content_sha
            positions[page.archive][page.name] = page.index

    plans = [
        RemovalPlan(
            archive=archive,
            remove_names=set(shas),
            original_pages=archive_page_counts.get(archive, 0),
            expected_sha=shas,
            indices=positions[archive],
        )
        for archive, shas in per_archive.items()
    ]
    plans.sort(key=lambda p: str(p.archive).lower())
    return plans


def split_protected(
    plans: Iterable[RemovalPlan], folders: Iterable[Path | str]
) -> tuple[list[RemovalPlan], list[RemovalPlan]]:
    """Plans outside every protected folder, and those inside one.

    A protected folder's books are imported and reviewed like any other, but
    never rewritten. Paths are compared the way the platform compares them, so
    on Windows a folder protects its books whatever the case of either path.
    """
    roots = [
        os.path.normcase(os.path.abspath(str(f).strip()))
        for f in folders
        if str(f).strip()
    ]
    allowed: list[RemovalPlan] = []
    protected: list[RemovalPlan] = []
    for plan in plans:
        where = os.path.normcase(os.path.abspath(plan.archive))
        inside = any(where == root or where.startswith(root.rstrip(os.sep) + os.sep)
                     for root in roots)
        (protected if inside else allowed).append(plan)
    return allowed, protected


def split_by_fraction(
    plans: Iterable[RemovalPlan], max_fraction: float = DEFAULT_MAX_FRACTION
) -> tuple[list[RemovalPlan], list[RemovalPlan]]:
    """Plans within the limit, and those that would take too much of a book.

    The limit is inclusive: exactly `max_fraction` of a book may go. The GUI and
    the command line both apply this before anything is rewritten.
    """
    within: list[RemovalPlan] = []
    over: list[RemovalPlan] = []
    for plan in plans:
        (over if plan.fraction > max_fraction else within).append(plan)
    return within, over


def is_backup_name(name: str, suffix: str = ".bak") -> bool:
    """True if `name` is a backup this app could have made.

    Backups are "<archive name><suffix>", optionally followed by ".<n>" when an
    earlier backup was in the way. Matching on the suffix alone would also catch
    real comics whose titles merely contain ".bak" somewhere.
    """
    lowered = name.lower()
    suffix = suffix.lower()
    stem, dot, tail = lowered.rpartition(".")
    if dot and tail.isdecimal() and stem.endswith(suffix):
        lowered = stem
    if not lowered.endswith(suffix):
        return False
    return Path(lowered[: -len(suffix)]).suffix in ARCHIVE_SUFFIXES


def find_backups(folders: Iterable[Path], suffix: str = ".bak") -> list[Path]:
    """Every backup this app could have made, directly inside any of `folders`.

    Backups sit beside their book, or flat in the backup folder, so one level
    is all there is to look at.
    """
    found: dict[Path, None] = {}
    for folder in folders:
        try:
            children = list(Path(folder).iterdir())
        except OSError:
            continue
        for child in children:
            if is_backup_name(child.name, suffix) and child.is_file():
                found.setdefault(child, None)
    return sorted(found)


def total_size(paths: Iterable[Path]) -> int:
    """Bytes used by the files that still exist."""
    total = 0
    for path in paths:
        with contextlib.suppress(OSError):
            total += path.stat().st_size
    return total


def _backup_path(archive: Path, policy: BackupPolicy) -> Path:
    if policy.directory is not None:
        policy.directory.mkdir(parents=True, exist_ok=True)
        target = policy.directory / (archive.name + policy.suffix)
    else:
        target = archive.with_name(archive.name + policy.suffix)
    candidate = target
    counter = 1
    while candidate.exists():  # never clobber an existing backup
        candidate = target.with_name(f"{target.name}.{counter}")
        counter += 1
    return candidate


def _existing(path: Path) -> Path:
    """`path`, or its nearest ancestor that exists (a folder not yet created)."""
    path = Path(os.path.abspath(path))
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _volume(path: Path) -> tuple[int, Path]:
    """An id for the volume holding `path`, and a folder on it that exists."""
    anchor = _existing(path)
    return anchor.stat().st_dev, anchor


def on_same_volume(a: Path, b: Path) -> bool:
    """True if moving between `a` and `b` is a rename rather than a copy."""
    try:
        return _volume(a)[0] == _volume(b)[0]
    except OSError:
        return True  # cannot tell; assume the cheap case rather than warn wrongly


def _require_free_space(folder: Path, needed: int, name: str) -> None:
    """Refuse to start a copy the destination volume cannot finish."""
    try:
        free = shutil.disk_usage(_existing(folder)).free
    except OSError as exc:
        log.debug("could not read free space on %s: %s", folder, exc)
        return
    if free < needed:
        raise RemovalError(
            f"not enough free space on {folder} to copy {name} there: it needs "
            f"{human_bytes(needed)} and {human_bytes(free)} is free"
        )


def _move_aside(src: Path, dst: Path) -> None:
    """Move `src` to `dst`, preferring an atomic rename.

    shutil.move silently degrades to copy-then-delete when a rename fails, which
    on Windows means a locked file leaves a half-made backup behind and *then*
    errors. os.replace either succeeds or raises, so only a genuine cross-volume
    move takes the copying path.
    """
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        cross_volume = exc.errno == errno.EXDEV or getattr(exc, "winerror", 0) == 17
        if not cross_volume:
            raise
    # A copy is a full second file until the source is deleted, so a volume that
    # cannot hold it is refused here rather than failing half way through.
    _require_free_space(dst.parent, src.stat().st_size, src.name)
    shutil.copy2(src, dst)
    try:
        src.unlink()
    except OSError:
        dst.unlink(missing_ok=True)  # do not leave an orphaned partial backup
        raise


def _write_marker(original: Path, destination: Path, backup: Path, temp: Path) -> Path:
    """Note, beside the book, that its original is about to be moved aside.

    If the process dies before the cleaned copy is swapped in, the book exists
    only as its backup; this note is what lets recover_interrupted put it back.
    """
    marker = destination.parent / f"{TEMP_PREFIX}{uuid.uuid4().hex}{MARKER_SUFFIX}"
    note = {
        "original": str(original),
        "destination": str(destination),
        "backup": str(backup),
        "temp": str(temp),
        "pid": os.getpid(),
    }
    marker.write_text(json.dumps(note), encoding="utf-8")
    return marker


def _drop_marker(marker: Path | None) -> None:
    if marker is None:
        return
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        # Harmless: the next recovery finds the book in place and clears it.
        log.warning("could not remove %s: %s", marker, exc)


def _process_alive(pid: int) -> bool:
    """True if `pid` is a running process, whose removal may still be under way."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0 only asks; never do this on Windows, where it kills
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(slots=True)
class Recovered:
    """A removal that was interrupted between moving the original and the swap."""

    original: Path
    backup: Path
    # True if the book was put back from its backup; False if it was already in
    # place and only the leftover note was cleared.
    restored: bool


def recover_interrupted(paths: Iterable[Path], *, dry_run: bool = False) -> list[Recovered]:
    """Put back books a killed run left only as a backup.

    Folders in `paths` are searched recursively, like find_archives; for any
    other path (a book, even one that is now missing) its folder is looked in.
    Only books with a note from apply_plan are touched, so a book deleted on
    purpose never comes back from its .bak.
    """
    folders: dict[Path, bool] = {}
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            folders[path] = True
        else:
            folders.setdefault(path.parent, False)
    pattern = f"{TEMP_PREFIX}*{MARKER_SUFFIX}"
    markers: set[Path] = set()
    for folder, recursive in folders.items():
        try:
            found = folder.rglob(pattern) if recursive else folder.glob(pattern)
            markers.update(m.resolve() for m in found if m.is_file())
        except OSError as exc:
            log.debug("could not look for removal notes in %s: %s", folder, exc)
    recovered = []
    for marker in sorted(markers):
        outcome = _recover_one(marker, dry_run=dry_run)
        if outcome is not None:
            recovered.append(outcome)
    return recovered


def _recover_one(marker: Path, *, dry_run: bool) -> Recovered | None:
    try:
        note = json.loads(marker.read_text(encoding="utf-8"))
        original = Path(note["original"])
        destination = Path(note["destination"])
        backup = Path(note["backup"])
        temp = Path(note["temp"])
        pid = int(note.get("pid", 0))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring unreadable removal note %s: %s", marker, exc)
        return None
    if _process_alive(pid):
        return None  # a removal still running in another window or shell
    # Only ever delete a temp file this app named, whatever the note says.
    own_temp = temp.name.startswith(TEMP_PREFIX) and temp != destination

    if destination.exists() or original.exists():
        # The swap finished, or the original never left: the book is in place and
        # only the note was missed. Both files stay; an unswapped temp is junk.
        if not dry_run:
            if own_temp:
                _discard(temp)
            _drop_marker(marker)
        return Recovered(original=original, backup=backup, restored=False)

    if not backup.exists():
        log.warning(
            "%s is missing and so is its backup %s; leaving %s for inspection",
            original, backup, marker,
        )
        return None
    if not dry_run:
        try:
            _move_aside(backup, original)
        except (OSError, RemovalError) as exc:
            log.warning("could not put %s back from %s: %s", original, backup, exc)
            return None  # the note stays, so the next start tries again
        if own_temp:
            _discard(temp)
        _drop_marker(marker)
        log.info("restored %s from %s after an interrupted removal", original, backup)
    return Recovered(original=original, backup=backup, restored=True)


def check_free_space(
    plans: Iterable[RemovalPlan],
    *,
    backup: BackupPolicy | None = None,
    output_dir: Path | None = None,
) -> list[SpaceShortfall]:
    """Volumes that cannot hold what these plans would write.

    Each book is counted at its current size, the most its cleaned copy can
    take. What stays on disk adds up: cleaned copies in an output folder, and
    backups (a rename on the book's own volume, but then the original is kept as
    well as the cleaned copy). What does not: with no backup, or a backup on
    another volume, each temp file replaces its book, so that volume only needs
    room for the largest one at a time.
    """
    policy = backup or BackupPolicy()
    anchors: dict[int, Path] = {}
    kept: dict[int, int] = {}
    transient: dict[int, int] = {}

    def volume_of(folder: Path) -> int:
        dev, anchor = _volume(folder)
        anchors.setdefault(dev, anchor)
        return dev

    for plan in plans:
        try:
            size = plan.archive.stat().st_size
            if output_dir is not None:
                out = volume_of(output_dir)
                kept[out] = kept.get(out, 0) + size
                continue
            book = volume_of(plan.archive.parent)
            if policy.enabled:
                target = volume_of(policy.directory or plan.archive.parent)
                kept[target] = kept.get(target, 0) + size
                if target == book:
                    continue
            transient[book] = max(transient.get(book, 0), size)
        except OSError:
            continue  # a missing book fails on its own when its turn comes

    shortfalls = []
    for dev, anchor in anchors.items():
        needed = kept.get(dev, 0) + transient.get(dev, 0)
        try:
            free = shutil.disk_usage(anchor).free
        except OSError as exc:
            log.debug("could not read free space on %s: %s", anchor, exc)
            continue
        if free < needed:
            shortfalls.append(SpaceShortfall(folder=anchor, needed=needed, free=free))
    return shortfalls


def _mode_of(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _apply_mode(path: Path, mode: int | None) -> None:
    """Give the rebuilt file the original's permission bits.

    mkstemp creates files readable by the owner only (0600), so without this a
    cleaned book on Linux or macOS could no longer be read by a media server
    running as another user, and a read-only original would come back writable.
    """
    if mode is None:
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log.debug("could not restore permissions on %s: %s", path, exc)


def _discard(path: Path) -> None:
    """Delete a temp file, even if it inherited a read-only mode."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink(missing_ok=True)


def _explain(exc: OSError, path: Path) -> str:
    """Turn the common Windows sharing violation into something actionable."""
    if getattr(exc, "winerror", 0) == 32 or exc.errno == errno.EACCES:
        return (
            f"{path.name} is open in another program (or still being read). "
            "Close it and run the removal again."
        )
    return str(exc)


def _verify_cbz(path: Path, expected_pages: int) -> None:
    """Open the rebuilt file and confirm it is sound before touching the original."""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RemovalError(f"rebuilt archive has a corrupt entry: {bad}")
            actual = sum(1 for n in zf.namelist() if is_page_name(n))
    except zipfile.BadZipFile as exc:
        raise RemovalError(f"rebuilt archive is not a valid zip: {exc}") from exc
    if actual != expected_pages:
        raise RemovalError(f"rebuilt archive has {actual} pages, expected {expected_pages}")


@dataclass(slots=True)
class _Checked:
    """What a plan will do to an archive, once it has been checked against it."""

    names: list[str]  # every entry, in archive order
    removing: set[str]
    removed_indices: set[int]
    remaining: int
    freed: int
    comicinfo: str | None


def _check_plan(arc: ComicArchive, plan: RemovalPlan) -> _Checked:
    """Confirm the marked pages are still the ones that were scanned.

    Raises RemovalError, removing nothing, if the archive changed, a marked page
    cannot be identified, or every page would go.
    """
    all_names = arc.entry_names()
    page_names = arc.page_names()
    removing = {n for n in page_names if n in plan.remove_names}

    missing = plan.remove_names - set(page_names)
    if missing:
        raise RemovalError(
            f"archive changed since scan; {len(missing)} marked page(s) not found"
        )
    unverifiable = sorted(n for n in removing if not plan.expected_sha.get(n))
    if unverifiable:
        raise RemovalError(
            f"no scanned fingerprint for {unverifiable[0]}, so it cannot be confirmed "
            "as the page that was marked; nothing was removed"
        )
    for name in sorted(removing):
        if content_digest(arc.read(name)) != plan.expected_sha[name]:
            raise RemovalError(
                f"archive changed since scan; {name} no longer matches the page "
                "that was marked, so nothing was removed"
            )
    if removing and len(removing) >= len(page_names):
        raise RemovalError(
            "refusing to remove every page - this would empty the archive"
        )

    return _Checked(
        names=all_names,
        removing=removing,
        removed_indices={i for i, n in enumerate(page_names) if n in removing},
        remaining=len(page_names) - len(removing),
        freed=sum(arc.stored_size(n) for n in removing),
        comicinfo=find_comicinfo(all_names),
    )


def _rebuild(arc: ComicArchive, dest: Path, checked: _Checked, *, compress: bool) -> None:
    """Write the archive minus the removed pages, streaming every other entry."""
    replace: dict[str, bytes] = {}
    if checked.comicinfo is not None:
        replace[checked.comicinfo] = update_comicinfo(
            arc.read(checked.comicinfo), checked.removed_indices, checked.remaining
        )
    keep = [n for n in checked.names if n not in checked.removing]
    write_cbz(dest, arc, keep, replace=replace, compress=compress)


def _failed(result: RemovalResult, error: str) -> RemovalResult:
    result.error = error
    result.removed = 0
    result.bytes_freed = 0
    # The book is untouched, so copies of "removed" pages would only mislead.
    for copy in result.quarantined:
        _discard(copy)
    result.quarantined = []
    return result


def _safe_part(name: str) -> str:
    """One path component made from an entry or book name, never a way out."""
    base = name.replace(SEP_ALT, "/").rsplit("/", 1)[-1]
    cleaned = "".join("_" if ch in _UNSAFE_CHARS or ord(ch) < 32 else ch for ch in base)
    cleaned = cleaned.strip(" .")
    return cleaned or "page"


def _quarantine(
    arc: ComicArchive, names: Iterable[str], folder: Path, result: RemovalResult
) -> None:
    """Copy each page about to be removed into `folder`, under the book's name.

    Every file written is noted in `result` as it is made, so a failure part
    way through can take back what was already copied. Existing files are never
    overwritten: a clash gets a numbered name instead.
    """
    target = folder / _safe_part(arc.path.stem)
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        base = Path(_safe_part(name))
        dest = target / base.name
        counter = 1
        while dest.exists():
            dest = target / f"{base.stem}-{counter}{base.suffix}"
            counter += 1
        with dest.open("xb") as out:
            result.quarantined.append(dest)
            arc.copy_to(name, out)


def apply_plan(
    plan: RemovalPlan,
    *,
    backup: BackupPolicy | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
    compress: bool = False,
    quarantine: Path | None = None,
) -> RemovalResult:
    """Rebuild one archive without its marked pages.

    The new file is streamed to a temp path and verified before the original is
    moved aside, so an interrupted or failed run never destroys the source.

    With `quarantine`, the removed pages are first copied there, under a folder
    named after the book. If that fails the book fails too, before anything is
    swapped, exactly as a failed rebuild would.
    """
    policy = backup or BackupPolicy()
    result = RemovalResult(archive=plan.archive)

    if not plan.remove_names:
        result.skipped = True
        return result

    # cbr/cb7 cannot be written back; they are rebuilt as a sibling .cbz.
    converted = detect_kind(plan.archive) in (ArchiveKind.RAR, ArchiveKind.SEVENZIP)
    destination = output_dir / plan.archive.name if output_dir is not None else plan.archive
    if converted:
        destination = destination.with_suffix(".cbz")
    replacing_in_place = output_dir is None

    # The source stays open only while it is checked and copied: Windows will not
    # let it be moved aside for the backup while a handle is still alive.
    tmp_path: Path | None = None
    marker: Path | None = None
    try:
        with ComicArchive(plan.archive) as arc:
            checked = _check_plan(arc, plan)
            if not checked.removing:
                result.skipped = True
                return result
            result.removed = len(checked.removing)
            result.bytes_freed = checked.freed
            result.converted = converted
            result.output = destination

            # The one destination we may write over is the archive itself, and only
            # when replacing in place (that path takes a backup). Anything else that
            # exists is a different file: a cleaned copy from an earlier run, the
            # output folder being the source folder, or book.cbz sitting next to the
            # book.cbr being converted. Checked before the dry-run exit so a dry run
            # reports the same refusal.
            if destination.exists() and not (
                replacing_in_place and destination == plan.archive
            ):
                return _failed(
                    result, f"refusing to overwrite existing file: {destination.name}"
                )
            if dry_run:
                return result

            # Folders are only created once we are really writing, so a dry run
            # leaves no trace, and an unusable destination is reported plainly.
            try:
                if output_dir is not None:
                    output_dir.mkdir(parents=True, exist_ok=True)
                tmp_fd, tmp_name = tempfile.mkstemp(
                    dir=str(destination.parent), prefix=TEMP_PREFIX, suffix=".cbz"
                )
            except OSError as exc:
                return _failed(
                    result,
                    f"cannot write to {destination.parent}: {_explain(exc, destination)}",
                )
            os.close(tmp_fd)
            tmp_path = Path(tmp_name)
            _rebuild(arc, tmp_path, checked, compress=compress)
            if quarantine is not None:
                try:
                    _quarantine(arc, sorted(checked.removing, key=natural_key), quarantine,
                                result)
                except (OSError, ArchiveError) as exc:
                    raise RemovalError(
                        f"could not copy the removed pages to {quarantine}: "
                        f"{_explain(exc, quarantine) if isinstance(exc, OSError) else exc}"
                    ) from exc
    except (ArchiveError, RemovalError) as exc:
        if tmp_path is not None:
            _discard(tmp_path)
        return _failed(result, str(exc))
    except (OSError, zipfile.BadZipFile) as exc:
        if tmp_path is None:
            return _failed(result, f"read failed: {exc}")
        _discard(tmp_path)
        message = _explain(exc, plan.archive) if isinstance(exc, OSError) else str(exc)
        return _failed(result, message)

    original_mode = _mode_of(plan.archive)
    try:
        _verify_cbz(tmp_path, checked.remaining)

        # Converting cbr to cbz with no backup: the source must still go, but only
        # once the replacement is in place - otherwise a failed rename loses both.
        drop_source = False
        if replacing_in_place and plan.archive.exists():
            if policy.enabled:
                backup_target = _backup_path(plan.archive, policy)
                marker = _write_marker(plan.archive, destination, backup_target, tmp_path)
                _move_aside(plan.archive, backup_target)
                result.backup = backup_target
            else:
                drop_source = result.converted

        _apply_mode(tmp_path, original_mode)
        os.replace(tmp_path, destination)
        _drop_marker(marker)
        if drop_source:
            try:
                plan.archive.unlink()
            except OSError as exc:
                # The cleaned .cbz exists; a leftover original is harmless.
                log.warning("could not remove converted original %s: %s", plan.archive, exc)
    except (RemovalError, OSError, zipfile.BadZipFile) as exc:
        _discard(tmp_path)
        # Restore the original if it was already moved aside.
        if result.backup is not None and not plan.archive.exists():
            _move_aside(result.backup, plan.archive)
            result.backup = None
        # Not reached if putting it back failed: the note then lets the next
        # start finish the job.
        _drop_marker(marker)
        _failed(
            result, _explain(exc, plan.archive) if isinstance(exc, OSError) else str(exc)
        )
    return result


def _common_parent(plans: list[RemovalPlan]) -> Path | None:
    """The deepest folder containing every archive, or None if there isn't one."""
    try:
        return Path(os.path.commonpath([plan.archive.parent for plan in plans]))
    except ValueError:  # nothing to compare, or archives on different drives
        return None


def apply_removals(
    plans: Iterable[RemovalPlan],
    *,
    backup: BackupPolicy | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
    compress: bool = False,
    quarantine: Path | None = None,
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> RemovalReport:
    """Apply every plan in turn. Disk-bound, so there is no thread pool here.

    `quarantine` keeps a copy of every removed page; see apply_plan.
    """
    todo = list(plans)
    report = RemovalReport()
    # Checked before the first book, dry run or not, so a run that cannot finish
    # never starts and a dry run reports exactly what a real one would.
    report.space_shortfall = check_free_space(todo, backup=backup, output_dir=output_dir)
    if report.space_shortfall:
        return report
    # Cleaned copies keep their folder layout under the output folder. Flattening
    # them would make two series that both have a "Vol 01.cbz" collide, and the
    # second would be refused.
    mirror_root = _common_parent(todo) if output_dir is not None else None
    for done, plan in enumerate(todo, start=1):
        if should_cancel is not None and should_cancel():
            report.cancelled = True
            break
        plan_output = output_dir
        if output_dir is not None and mirror_root is not None:
            plan_output = output_dir / plan.archive.parent.relative_to(mirror_root)
        # One archive blowing up must not discard the results of those already
        # rewritten - the user still needs to hear which files changed.
        try:
            result = apply_plan(
                plan,
                backup=backup,
                output_dir=plan_output,
                dry_run=dry_run,
                compress=compress,
                quarantine=quarantine,
            )
        except Exception as exc:
            log.exception("removal failed for %s", plan.archive)
            result = RemovalResult(archive=plan.archive, error=f"unexpected error: {exc}")
        report.results.append(result)
        if progress is not None:
            progress(done, len(todo), plan.archive.name)
    return report
