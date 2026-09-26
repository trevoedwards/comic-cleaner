"""Core data types shared by the scanner, grouper and remover."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path

# Entry names we never treat as a comic page, even if they decode as images.
NON_PAGE_NAMES = {"comicinfo.xml", "thumbs.db", ".ds_store"}

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif", ".jxl", ".tif", ".tiff",
}


class ArchiveKind(enum.Enum):
    ZIP = "zip"
    RAR = "rar"
    SEVENZIP = "7z"
    UNKNOWN = "unknown"


class Decision(enum.Enum):
    """What the user wants done with a duplicate group."""

    UNDECIDED = "undecided"
    DELETE = "delete"
    KEEP = "keep"
    IGNORE = "ignore"  # keep, and never surface this group again
    # Come back to it later. Not a keep: nothing is remembered about the page, and
    # the group stays in the list, but "next undecided" steps over it.
    DEFER = "defer"


class MatchKind(enum.Enum):
    EXACT = "exact"      # byte- or pixel-identical
    SIMILAR = "similar"  # perceptual hash within threshold


@dataclass(slots=True)
class PageEntry:
    """One image inside one archive."""

    archive: Path
    name: str            # entry name inside the archive
    index: int           # 0-based position among *pages* (not among all entries)
    size: int            # compressed-source byte size of the image data
    width: int
    height: int
    content_sha: str     # sha256 of the raw stored bytes -> exact match
    dhash: int           # 64-bit difference hash -> perceptual match
    flat: bool = False   # near-uniform image (blank/black page); prone to false grouping
    error: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.archive), self.name)

    @property
    def label(self) -> str:
        return f"{self.archive.name} — p{self.index + 1} ({self.name})"


@dataclass(slots=True)
class ArchiveInfo:
    """A comic archive that has been imported and (maybe) scanned."""

    path: Path
    kind: ArchiveKind
    size: int
    mtime_ns: int
    page_count: int = 0
    pages: list[PageEntry] = field(default_factory=list)
    error: str | None = None
    # Came straight from the hash cache, with no page decoded; see ScanStats.
    cached: bool = False
    # From ComicInfo.xml, when the book has one; empty otherwise.
    series: str = ""
    number: str = ""
    title: str = ""

    @property
    def readable(self) -> bool:
        return self.error is None

    @property
    def display_name(self) -> str:
        """"Series #Number" when ComicInfo names the series, else the file name."""
        if not self.series:
            return self.path.name
        return f"{self.series} #{self.number}" if self.number else self.series


@dataclass(slots=True)
class DuplicateGroup:
    """A set of pages considered to be the same image."""

    gid: str                 # stable id, derived from the group's representative hash
    kind: MatchKind
    pages: list[PageEntry]
    decision: Decision = Decision.UNDECIDED
    # Pages explicitly excluded from removal by the user (by PageEntry.key).
    kept: set[tuple[str, str]] = field(default_factory=set)
    # Matches a page removed from the library before. Such a group is shown even
    # below the copy and book minimums: once the old books are clean, the same
    # advert in a new book has nothing left to repeat against.
    known: bool = False
    # Share of the copies within a few pages of the start or end of their book,
    # which is where adverts and credits sit. A match found only mid-book is more
    # likely to be a coincidence, or content that legitimately recurs.
    edge_share: float = 1.0

    @property
    def archive_count(self) -> int:
        return len({p.archive for p in self.pages})

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def recoverable_bytes(self) -> int:
        """Bytes freed if every non-kept page in this group is removed."""
        return sum(p.size for p in self.pages if p.key not in self.kept)

    @property
    def representative(self) -> PageEntry:
        # Prefer the largest image; it is the best thumbnail source.
        return max(self.pages, key=lambda p: (p.width * p.height, p.size))

    def pages_to_remove(self) -> list[PageEntry]:
        if self.decision is not Decision.DELETE:
            return []
        return [p for p in self.pages if p.key not in self.kept]
