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

    @property
    def readable(self) -> bool:
        return self.error is None

    @property
    def writable(self) -> bool:
        """RAR/7z cannot be rewritten in place; they get converted to CBZ."""
        return self.error is None


@dataclass(slots=True)
class DuplicateGroup:
    """A set of pages considered to be the same image."""

    gid: str                 # stable id, derived from the group's representative hash
    kind: MatchKind
    pages: list[PageEntry]
    decision: Decision = Decision.UNDECIDED
    # Pages explicitly excluded from removal by the user (by PageEntry.key).
    kept: set[tuple[str, str]] = field(default_factory=set)

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
