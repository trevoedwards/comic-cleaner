"""SQLite cache of per-page hashes, keyed by archive identity.

Rescanning a large library is dominated by image decoding, so results are cached
against (path, size, mtime). Touching an archive invalidates only that archive.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .model import PageEntry

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS archives (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    scanned_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS pages (
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    size        INTEGER NOT NULL,
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    content_sha TEXT NOT NULL,
    dhash       INTEGER NOT NULL,
    flat        INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    PRIMARY KEY (path, name),
    FOREIGN KEY (path) REFERENCES archives(path) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS pages_content ON pages(content_sha);

CREATE TABLE IF NOT EXISTS ignored (
    gid        TEXT PRIMARY KEY,
    note       TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
-- Every perceptual hash in an ignored group. Rows from before this table existed
-- have none, and fall back to their gid, which is the group's lowest hash.
CREATE TABLE IF NOT EXISTS ignored_hashes (
    gid   TEXT NOT NULL,
    dhash INTEGER NOT NULL,
    PRIMARY KEY (gid, dhash)
);

-- Known junk: pages removed from the library before (or imported from someone
-- else's list), marked for removal again wherever they turn up.
CREATE TABLE IF NOT EXISTS known (
    sid        TEXT PRIMARY KEY,
    note       TEXT,
    source     TEXT NOT NULL DEFAULT 'removed',
    thumbnail  BLOB,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS known_hashes (
    sid   TEXT NOT NULL,
    dhash INTEGER NOT NULL,
    PRIMARY KEY (sid, dhash)
);

-- Every real removal run, so any book in it can be put back from its backup.
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    source     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    archive         TEXT NOT NULL,
    output          TEXT NOT NULL,
    backup          TEXT,
    removed         INTEGER NOT NULL,
    pages           TEXT NOT NULL,
    bytes_freed     INTEGER NOT NULL,
    converted       INTEGER NOT NULL,
    output_size     INTEGER,
    output_mtime_ns INTEGER,
    restored_at     REAL
);
CREATE INDEX IF NOT EXISTS run_items_run ON run_items(run_id);
"""

# Columns added to `ignored` after it first shipped, so the manager can show a
# thumbnail of what was hidden. Added on open to databases that predate them.
_IGNORED_COLUMNS = {"sample_path": "TEXT", "sample_name": "TEXT"}


@dataclass(slots=True)
class IgnoredEntry:
    """One "ignore" action, as listed in the manager."""

    gid: str
    note: str
    created_at: float
    sample_path: Path | None
    sample_name: str | None


@dataclass(slots=True)
class KnownEntry:
    """One remembered piece of junk, as listed in the manager or exported."""

    sid: str
    note: str
    source: str  # "removed" here, or "imported" from a shared list
    created_at: float
    thumbnail: bytes | None  # PNG; the page itself is gone from the library
    hashes: set[int]


_SIGN_BIT = 1 << 63
_UINT64 = 1 << 64


def _to_signed(value: int) -> int:
    """SQLite INTEGER is signed 64-bit; dhash uses the full unsigned range."""
    return value - _UINT64 if value >= _SIGN_BIT else value


def _to_unsigned(value: int) -> int:
    return value + _UINT64 if value < 0 else value


class HashCache:
    """Thread-safe: the scanner hashes archives on a pool of worker threads and
    they all write back through this one connection."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0
        )
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        present = {row[1] for row in self.conn.execute("PRAGMA table_info(ignored)")}
        for column, kind in _IGNORED_COLUMNS.items():
            if column not in present:
                self.conn.execute(f"ALTER TABLE ignored ADD COLUMN {column} {kind}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- page hashes -------------------------------------------------------
    def get(self, path: Path, size: int, mtime_ns: int) -> list[PageEntry] | None:
        """Cached pages for an unchanged archive, or None if stale/absent."""
        key = str(Path(path).resolve())
        with self._lock:
            return self._get_locked(key, Path(path), size, mtime_ns)

    def _get_locked(
        self, key: str, path: Path, size: int, mtime_ns: int
    ) -> list[PageEntry] | None:
        row = self.conn.execute(
            "SELECT size, mtime_ns FROM archives WHERE path = ?", (key,)
        ).fetchone()
        if row is None:
            if not self._adopt_moved(key, size, mtime_ns):
                return None
        elif row[0] != size or row[1] != mtime_ns:
            return None
        rows = self.conn.execute(
            "SELECT name, idx, size, width, height, content_sha, dhash, flat, error "
            "FROM pages WHERE path = ? ORDER BY idx",
            (key,),
        ).fetchall()
        return [
            PageEntry(
                archive=Path(path),
                name=r[0],
                index=r[1],
                size=r[2],
                width=r[3],
                height=r[4],
                content_sha=r[5],
                dhash=_to_unsigned(r[6]),
                flat=bool(r[7]),
                error=r[8],
            )
            for r in rows
        ]

    def _adopt_moved(self, key: str, size: int, mtime_ns: int) -> bool:
        """Reuse the hashes of a book that was moved, renamed folder and all.

        The cache is keyed on the full path, so moving a library would otherwise
        mean decoding every page again. A book with the same file name, size and
        nanosecond modification time as exactly one cached book is taken to be
        that book: moves and copies keep all three. A rename changes the name, so
        it is simply hashed again, which is safe. If the old path is gone this was
        a move and the entry is re-keyed; if it is still there, it is copied.
        """
        name = Path(key).name
        candidates = [
            r[0]
            for r in self.conn.execute(
                "SELECT path FROM archives WHERE size = ? AND mtime_ns = ?", (size, mtime_ns)
            )
            if Path(r[0]).name == name and r[0] != key
        ]
        if len(candidates) != 1:
            return False
        old = candidates[0]
        with self.conn:
            self.conn.execute(
                "INSERT INTO archives(path, size, mtime_ns) VALUES (?, ?, ?)",
                (key, size, mtime_ns),
            )
            if Path(old).exists():
                self.conn.execute(
                    "INSERT INTO pages(path, name, idx, size, width, height, content_sha, "
                    "dhash, flat, error) SELECT ?, name, idx, size, width, height, "
                    "content_sha, dhash, flat, error FROM pages WHERE path = ?",
                    (key, old),
                )
            else:
                # Pages move to the new row before the old one goes: the foreign key
                # cascades deletes but does not follow a renamed key.
                self.conn.execute("UPDATE pages SET path = ? WHERE path = ?", (key, old))
                self.conn.execute("DELETE FROM archives WHERE path = ?", (old,))
        log.debug("reused cached hashes of %s for %s", old, key)
        return True

    def put(self, path: Path, size: int, mtime_ns: int, pages: list[PageEntry]) -> None:
        key = str(Path(path).resolve())
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM archives WHERE path = ?", (key,))
            self.conn.execute(
                "INSERT INTO archives(path, size, mtime_ns) VALUES (?, ?, ?)",
                (key, size, mtime_ns),
            )
            self.conn.executemany(
                "INSERT INTO pages(path, name, idx, size, width, height, content_sha, "
                "dhash, flat, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        key, p.name, p.index, p.size, p.width, p.height,
                        p.content_sha, _to_signed(p.dhash), int(p.flat), p.error,
                    )
                    for p in pages
                ],
            )

    def invalidate(self, path: Path) -> None:
        key = str(Path(path).resolve())
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM archives WHERE path = ?", (key,))
            self.conn.execute("DELETE FROM pages WHERE path = ?", (key,))

    # -- ignore list -------------------------------------------------------
    def ignored_hashes(self) -> set[int]:
        """Every perceptual hash the user has asked never to see again."""
        with self._lock:
            hashes = {
                _to_unsigned(r[0])
                for r in self.conn.execute("SELECT dhash FROM ignored_hashes")
            }
            for (gid,) in self.conn.execute(
                "SELECT gid FROM ignored WHERE gid NOT IN (SELECT gid FROM ignored_hashes)"
            ):
                with contextlib.suppress(ValueError):
                    hashes.add(int(gid, 16))
        return hashes

    def ignored_entries(self) -> list[IgnoredEntry]:
        """Ignore actions, most recent first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT gid, note, created_at, sample_path, sample_name FROM ignored "
                "ORDER BY created_at DESC, gid"
            ).fetchall()
        return [
            IgnoredEntry(
                gid=r[0],
                note=r[1] or "",
                created_at=float(r[2]),
                sample_path=Path(r[3]) if r[3] else None,
                sample_name=r[4],
            )
            for r in rows
        ]

    def ignore(
        self,
        gid: str,
        hashes: Iterable[int] = (),
        *,
        note: str = "",
        sample: tuple[Path, str] | None = None,
    ) -> None:
        sample_path, sample_name = (str(sample[0]), sample[1]) if sample else (None, None)
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM ignored_hashes WHERE gid = ?", (gid,))
            self.conn.execute(
                "INSERT OR REPLACE INTO ignored(gid, note, sample_path, sample_name) "
                "VALUES (?, ?, ?, ?)",
                (gid, note, sample_path, sample_name),
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO ignored_hashes(gid, dhash) VALUES (?, ?)",
                [(gid, _to_signed(h)) for h in set(hashes)],
            )

    def unignore(self, gid: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM ignored WHERE gid = ?", (gid,))
            self.conn.execute("DELETE FROM ignored_hashes WHERE gid = ?", (gid,))

    def clear_ignored(self) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM ignored")
            self.conn.execute("DELETE FROM ignored_hashes")

    # -- known junk --------------------------------------------------------
    def known_hashes(self) -> set[int]:
        with self._lock:
            return {
                _to_unsigned(r[0])
                for r in self.conn.execute("SELECT dhash FROM known_hashes")
            }

    def known_entries(self) -> list[KnownEntry]:
        """Everything remembered, most recent first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT sid, note, source, created_at, thumbnail FROM known "
                "ORDER BY created_at DESC, sid"
            ).fetchall()
            hashes: dict[str, set[int]] = {}
            for sid, dhash in self.conn.execute("SELECT sid, dhash FROM known_hashes"):
                hashes.setdefault(sid, set()).add(_to_unsigned(dhash))
        return [
            KnownEntry(
                sid=r[0], note=r[1] or "", source=r[2], created_at=float(r[3]),
                thumbnail=bytes(r[4]) if r[4] is not None else None,
                hashes=hashes.get(r[0], set()),
            )
            for r in rows
        ]

    def remember(
        self,
        sid: str,
        hashes: Iterable[int],
        *,
        note: str = "",
        thumbnail: bytes | None = None,
        source: str = "removed",
    ) -> bool:
        """Add a piece of known junk, merging with an entry of the same id.

        Merging rather than replacing means removing the same advert again, now
        with a re-encoded copy among it, widens what is recognised. Returns True
        if the entry is new.
        """
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT thumbnail FROM known WHERE sid = ?", (sid,)
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO known(sid, note, source, thumbnail) VALUES (?, ?, ?, ?)",
                    (sid, note, source, thumbnail),
                )
            elif existing[0] is None and thumbnail is not None:
                self.conn.execute(
                    "UPDATE known SET thumbnail = ? WHERE sid = ?", (thumbnail, sid)
                )
            self.conn.executemany(
                "INSERT OR IGNORE INTO known_hashes(sid, dhash) VALUES (?, ?)",
                [(sid, _to_signed(h)) for h in set(hashes)],
            )
        return existing is None

    def forget(self, sid: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM known WHERE sid = ?", (sid,))
            self.conn.execute("DELETE FROM known_hashes WHERE sid = ?", (sid,))

    def clear_known(self) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM known")
            self.conn.execute("DELETE FROM known_hashes")

    # -- removal history ---------------------------------------------------
    def add_run(self, source: str, items: list[tuple]) -> int:
        """Store one run. Each item is (archive, output, backup, removed, pages_json,
        bytes_freed, converted, output_size, output_mtime_ns). Returns the run id."""
        with self._lock, self.conn:
            cursor = self.conn.execute("INSERT INTO runs(source) VALUES (?)", (source,))
            run_id = int(cursor.lastrowid)
            self.conn.executemany(
                "INSERT INTO run_items(run_id, archive, output, backup, removed, pages, "
                "bytes_freed, converted, output_size, output_mtime_ns) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(run_id, *item) for item in items],
            )
        return run_id

    def run_rows(self) -> list[tuple]:
        """(id, started_at, source) for every run, newest first."""
        with self._lock:
            return self.conn.execute(
                "SELECT id, started_at, source FROM runs ORDER BY started_at DESC, id DESC"
            ).fetchall()

    def run_item_rows(self) -> list[tuple]:
        with self._lock:
            return self.conn.execute(
                "SELECT id, run_id, archive, output, backup, removed, pages, bytes_freed, "
                "converted, output_size, output_mtime_ns, restored_at "
                "FROM run_items ORDER BY run_id DESC, archive"
            ).fetchall()

    def mark_restored(self, item_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE run_items SET restored_at = strftime('%s','now') WHERE id = ?",
                (item_id,),
            )
