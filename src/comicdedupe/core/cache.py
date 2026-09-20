"""SQLite cache of per-page hashes, keyed by archive identity.

Rescanning a large library is dominated by image decoding, so results are cached
against (path, size, mtime). Touching an archive invalidates only that archive.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
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
"""


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
        self.conn.commit()

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
        if row is None or row[0] != size or row[1] != mtime_ns:
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
    def ignored_gids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self.conn.execute("SELECT gid FROM ignored")}

    def ignore(self, gid: str, note: str = "") -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO ignored(gid, note) VALUES (?, ?)", (gid, note)
            )

    def unignore(self, gid: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM ignored WHERE gid = ?", (gid,))

    def clear_ignored(self) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM ignored")
