"""Review packs: matching settings, known junk and the ignore list, in one file.

A pack moves a whole review setup to another machine, or shares it. Known junk
and ignored pages are merged exactly as their own importers merge them; the
matching settings are only a proposal, which the GUI applies after asking. The
command line has no saved settings of its own, so it neither writes nor applies
them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import APP_NAME, __version__
from .cache import HashCache, KnownEntry
from .grouping import MAX_EDGE_PAGES, MAX_THRESHOLD
from .signatures import (
    ImportResult,
    SignatureFileError,
    known_rows,
    parse_hash,
    parse_known_rows,
    store_known,
)

FILE_FORMAT = "comiccleaner-review-pack"
FILE_VERSION = 1

# Setting name -> (type, lowest, highest). Nothing else in a pack is applied.
SETTING_LIMITS: dict[str, tuple[type, int, int]] = {
    "threshold": (int, 0, MAX_THRESHOLD),
    "min_pages": (int, 2, 100),
    "min_archives": (int, 1, 100),
    "edge_pages": (int, 1, MAX_EDGE_PAGES),
    "include_flat": (bool, 0, 1),
    "skip_first_page": (bool, 0, 1),
    "skip_last_page": (bool, 0, 1),
}

_MAX_IGNORED = 20_000
_MAX_HASHES = 1_000
_MAX_NOTE = 200


@dataclass(slots=True)
class IgnoredRow:
    gid: str
    note: str
    hashes: set[int]


@dataclass(slots=True)
class ReviewPack:
    settings: dict[str, Any] | None = None
    known: list[KnownEntry] = field(default_factory=list)
    ignored: list[IgnoredRow] = field(default_factory=list)


@dataclass(slots=True)
class PackImport:
    known: ImportResult = field(default_factory=ImportResult)
    ignored_added: int = 0
    ignored_merged: int = 0


def export_pack(cache: HashCache, path: Path, settings: Mapping[str, Any] | None) -> ReviewPack:
    """Write the current known junk and ignore list, and `settings` if given."""
    notes = {entry.gid: entry.note for entry in cache.ignored_entries()}
    ignored = [
        IgnoredRow(gid=gid, note=notes.get(gid, ""), hashes=hashes)
        for gid, hashes in sorted(cache.ignored_hash_map().items())
    ]
    known = cache.known_entries()
    chosen = {k: settings[k] for k in SETTING_LIMITS if k in settings} if settings else None
    payload = {
        "format": FILE_FORMAT,
        "version": FILE_VERSION,
        "exported_by": f"{APP_NAME} {__version__}",
        "settings": chosen,
        "known": known_rows(known),
        "ignored": [
            {"id": row.gid, "note": row.note,
             "hashes": sorted(f"{h:016x}" for h in row.hashes)}
            for row in ignored
        ],
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return ReviewPack(settings=chosen, known=known, ignored=ignored)


def _settings(raw: object, name: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SignatureFileError(f"{name} has settings that are not an object")
    chosen: dict[str, Any] = {}
    for key, (kind, low, high) in SETTING_LIMITS.items():
        if key not in raw:
            continue
        value = raw[key]
        if kind is bool:
            if not isinstance(value, bool):
                raise SignatureFileError(f"{name}: setting {key} must be true or false")
            chosen[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise SignatureFileError(f"{name}: setting {key} must be from {low} to {high}")
        chosen[key] = value
    return chosen or None


def _ignored(raw: object, name: str) -> list[IgnoredRow]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > _MAX_IGNORED:
        raise SignatureFileError(f"{name} has an unreadable ignore list")
    rows: list[IgnoredRow] = []
    for row in raw:
        if not isinstance(row, dict):
            raise SignatureFileError(f"{name}: an ignored entry is not an object")
        raw_hashes = row.get("hashes")
        if not isinstance(raw_hashes, list) or not raw_hashes or len(raw_hashes) > _MAX_HASHES:
            raise SignatureFileError(f"{name}: an ignored entry has no usable hashes")
        hashes = {parse_hash(h) for h in raw_hashes}
        # The id is recomputed, as for known junk: the group's lowest hash.
        rows.append(IgnoredRow(
            gid=f"{min(hashes):016x}", note=str(row.get("note") or "")[:_MAX_NOTE],
            hashes=hashes,
        ))
    return rows


def read_pack(path: Path) -> ReviewPack:
    """Parse and validate a pack, without storing anything."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SignatureFileError(f"cannot read {path.name}: {exc}") from exc
    except ValueError as exc:
        raise SignatureFileError(f"{path.name} is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("format") != FILE_FORMAT:
        raise SignatureFileError(f"{path.name} is not a Comic Cleaner review pack")
    if payload.get("version") != FILE_VERSION:
        raise SignatureFileError(
            f"{path.name} is version {payload.get('version')}, "
            f"this app reads version {FILE_VERSION}"
        )
    known_raw = payload.get("known", [])
    return ReviewPack(
        settings=_settings(payload.get("settings"), path.name),
        known=parse_known_rows(known_raw, path.name) if known_raw else [],
        ignored=_ignored(payload.get("ignored"), path.name),
    )


def import_pack(cache: HashCache, pack: ReviewPack) -> PackImport:
    """Merge a pack's known junk and ignore list. Settings are the caller's call."""
    result = PackImport(known=store_known(cache, pack.known))
    existing = cache.ignored_hash_map()
    entries = {entry.gid: entry for entry in cache.ignored_entries()}
    for row in pack.ignored:
        before = existing.get(row.gid)
        if before is None:
            cache.ignore(row.gid, row.hashes, note=row.note)
            result.ignored_added += 1
            continue
        result.ignored_merged += 1
        if row.hashes <= before:
            continue
        # Widen the entry, keeping its own note and sample page.
        entry = entries.get(row.gid)
        sample = (
            (entry.sample_path, entry.sample_name)
            if entry is not None and entry.sample_path is not None and entry.sample_name
            else None
        )
        cache.ignore(
            row.gid, row.hashes | before,
            note=entry.note if entry is not None else row.note, sample=sample,
        )
    return result


def setting_changes(
    current: Mapping[str, Any], proposed: Mapping[str, Any] | None
) -> list[tuple[str, Any, Any]]:
    """(name, now, from the pack) for every setting the pack would change."""
    if not proposed:
        return []
    return [
        (key, current.get(key), value)
        for key, value in proposed.items()
        if key in SETTING_LIMITS and current.get(key) != value
    ]
