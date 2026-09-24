"""Known junk: remembering removed pages, and sharing the list.

Once a library has been cleaned, the advert that used to repeat across forty
books is gone, so the same advert arriving in book forty-one repeats against
nothing and would never be grouped. Remembering what was removed closes that
gap: pages matching a remembered hash are grouped, and marked, on sight.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .. import APP_NAME, __version__
from .archive import ArchiveError, ComicArchive, detect_kind
from .cache import HashCache, KnownEntry
from .hashing import DecodeError, make_thumbnail
from .model import ArchiveKind, DuplicateGroup, PageEntry
from .remover import RemovalReport

log = logging.getLogger(__name__)

FILE_FORMAT = "comiccleaner-known-junk"
FILE_VERSION = 1
THUMBNAIL_SIZE = 160

# An imported list comes from someone else, so it is held to sensible limits.
_MAX_ENTRIES = 20_000
_MAX_HASHES_PER_ENTRY = 1_000
_MAX_THUMBNAIL_BYTES = 512 * 1024
_MAX_NOTE = 200


class SignatureFileError(ValueError):
    """The file is not a known-junk list this version can read."""


def describe(group: DuplicateGroup) -> str:
    return f"{group.page_count} copies in {group.archive_count} book(s)"


def _sample_page(group: DuplicateGroup) -> PageEntry:
    """A copy worth making the thumbnail from: a zip if possible, since reading
    one page of a cbr means extracting the whole archive first."""
    return max(
        group.pages,
        key=lambda p: (detect_kind(p.archive) is ArchiveKind.ZIP, p.width * p.height),
    )


def sample_thumbnail(group: DuplicateGroup) -> bytes | None:
    page = _sample_page(group)
    try:
        with ComicArchive(page.archive) as arc:
            return make_thumbnail(arc.read(page.name), THUMBNAIL_SIZE)
    except (ArchiveError, DecodeError, OSError, ValueError) as exc:
        log.debug("no thumbnail for %s: %s", page.label, exc)
        return None
    except Exception as exc:  # Pillow raises a wide variety of types
        log.debug("no thumbnail for %s: %s", page.label, exc)
        return None


def capture_samples(groups: Iterable[DuplicateGroup]) -> dict[str, bytes | None]:
    """Thumbnails for the groups about to be removed, taken while the pages exist."""
    return {group.gid: sample_thumbnail(group) for group in groups}


def learn_from_run(
    cache: HashCache,
    groups: Iterable[DuplicateGroup],
    report: RemovalReport,
    samples: dict[str, bytes | None] | None = None,
) -> int:
    """Remember every group that was actually removed from at least one book.

    A group whose every book failed, or was never reached because the run was
    cancelled, is not remembered: nothing about it was confirmed. Returns how
    many groups were new to the list.
    """
    if report.cancelled and not report.succeeded:
        return 0
    cleaned = {r.archive for r in report.succeeded}
    samples = samples or {}
    added = 0
    for group in groups:
        if not any(p.archive in cleaned for p in group.pages_to_remove()):
            continue
        if cache.remember(
            group.gid,
            {p.dhash for p in group.pages},
            note=describe(group),
            thumbnail=samples.get(group.gid),
        ):
            added += 1
    return added


# -- sharing ---------------------------------------------------------------


@dataclass(slots=True)
class ImportResult:
    added: int = 0
    merged: int = 0


def export_known(entries: Iterable[KnownEntry], path: Path) -> int:
    """Write a list others can import. Returns the number of entries written."""
    rows = [
        {
            "id": entry.sid,
            "note": entry.note,
            "hashes": sorted(f"{h:016x}" for h in entry.hashes),
            "thumbnail": (
                base64.b64encode(entry.thumbnail).decode("ascii") if entry.thumbnail else None
            ),
        }
        for entry in entries
    ]
    payload = {
        "format": FILE_FORMAT,
        "version": FILE_VERSION,
        "exported_by": f"{APP_NAME} {__version__}",
        "entries": rows,
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return len(rows)


def _parse_hash(raw: object) -> int:
    if not isinstance(raw, str) or len(raw) != 16:
        raise SignatureFileError(f"not a 64-bit hash: {raw!r}")
    try:
        return int(raw, 16)
    except ValueError as exc:
        raise SignatureFileError(f"not a 64-bit hash: {raw!r}") from exc


def read_known(path: Path) -> list[KnownEntry]:
    """Parse and validate a list, without storing anything."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SignatureFileError(f"cannot read {path.name}: {exc}") from exc
    except ValueError as exc:
        raise SignatureFileError(f"{path.name} is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("format") != FILE_FORMAT:
        raise SignatureFileError(f"{path.name} is not a Comic Cleaner known-junk list")
    if payload.get("version") != FILE_VERSION:
        raise SignatureFileError(
            f"{path.name} is version {payload.get('version')}, "
            f"this app reads version {FILE_VERSION}"
        )
    rows = payload.get("entries")
    if not isinstance(rows, list):
        raise SignatureFileError(f"{path.name} has no entries")
    if len(rows) > _MAX_ENTRIES:
        raise SignatureFileError(f"{path.name} has more than {_MAX_ENTRIES} entries")

    entries: list[KnownEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SignatureFileError("an entry is not an object")
        raw_hashes = row.get("hashes")
        if not isinstance(raw_hashes, list) or not raw_hashes:
            raise SignatureFileError("an entry has no hashes")
        if len(raw_hashes) > _MAX_HASHES_PER_ENTRY:
            raise SignatureFileError("an entry has too many hashes")
        hashes = {_parse_hash(h) for h in raw_hashes}
        thumbnail = None
        raw_thumb = row.get("thumbnail")
        if isinstance(raw_thumb, str):
            try:
                thumbnail = base64.b64decode(raw_thumb, validate=True)
            except (binascii.Error, ValueError):
                thumbnail = None
            if thumbnail is not None and (
                len(thumbnail) > _MAX_THUMBNAIL_BYTES or not thumbnail.startswith(b"\x89PNG")
            ):
                thumbnail = None  # the entry is still useful without its picture
        # The id is recomputed rather than trusted, so it always means the same
        # thing as an id this app made itself: the entry's lowest hash.
        entries.append(
            KnownEntry(
                sid=f"{min(hashes):016x}",
                note=str(row.get("note") or "")[:_MAX_NOTE],
                source="imported",
                created_at=0.0,
                thumbnail=thumbnail,
                hashes=hashes,
            )
        )
    return entries


def import_known(cache: HashCache, path: Path) -> ImportResult:
    """Merge a list into the store. Nothing is stored if the file is invalid."""
    result = ImportResult()
    for entry in read_known(path):
        if cache.remember(
            entry.sid, entry.hashes, note=entry.note,
            thumbnail=entry.thumbnail, source="imported",
        ):
            result.added += 1
        else:
            result.merged += 1
    return result
