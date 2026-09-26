"""Whole books that are the same issue twice, as opposed to repeated filler pages.

Two copies of one issue match each other page for page, so every page of
either would show up as a group of two. That is not junk to remove, and
removing it would gut both books; the removal cap already refuses such a plan.
This finds those pairs up front so they can be named for what they are.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from .model import ArchiveInfo

# The share of the smaller book's pages that must also be in the other book.
DEFAULT_MIN_OVERLAP = 0.8

# A page found in this many books or more is filler (an advert, a credits
# page), not evidence that two books are one issue. Leaving such pages out also
# keeps the pairing from growing with the square of the library.
_COMMON_PAGE_BOOKS = 6


@dataclass(slots=True, frozen=True)
class DuplicateBooks:
    """Two books that look like the same issue."""

    smaller: Path
    larger: Path
    shared: int          # distinct pages the two have in common
    smaller_pages: int   # distinct pages in the smaller book, filler aside

    @property
    def overlap(self) -> float:
        return self.shared / self.smaller_pages if self.smaller_pages else 0.0

    def describe(self) -> str:
        return (
            f"{self.smaller.name} and {self.larger.name} share {self.overlap:.0%} of "
            f"{self.smaller.name}'s pages"
        )


def find_duplicate_books(
    archives: Iterable[ArchiveInfo], min_overlap: float = DEFAULT_MIN_OVERLAP
) -> list[DuplicateBooks]:
    """Pairs of books whose pages are mostly byte-identical to each other.

    A pair is flagged when at least `min_overlap` of the smaller book's distinct
    page hashes are also in the larger one. Pages found in many books are left
    out of both the count and the total, since filler proves nothing. Nothing
    here marks or removes anything; it only reports.
    """
    content: dict[Path, set[str]] = {}
    for archive in archives:
        if archive.error:
            continue
        shas = {p.content_sha for p in archive.pages if p.content_sha and not p.error}
        if shas:
            content[archive.path] = shas

    holders: dict[str, list[Path]] = defaultdict(list)
    for path, shas in content.items():
        for sha in shas:
            holders[sha].append(path)
    common = {sha for sha, books in holders.items() if len(books) >= _COMMON_PAGE_BOOKS}

    shared: Counter[tuple[Path, Path]] = Counter()
    for sha, books in holders.items():
        if sha in common or len(books) < 2:
            continue
        for a, b in combinations(sorted(books), 2):
            shared[(a, b)] += 1

    pairs: list[DuplicateBooks] = []
    for (a, b), count in shared.items():
        size_a = len(content[a] - common)
        size_b = len(content[b] - common)
        smaller, larger = (a, b) if (size_a, str(a)) <= (size_b, str(b)) else (b, a)
        pair = DuplicateBooks(
            smaller=smaller, larger=larger, shared=count,
            smaller_pages=min(size_a, size_b),
        )
        if pair.smaller_pages and pair.overlap >= min_overlap:
            pairs.append(pair)
    pairs.sort(key=lambda p: (-p.overlap, str(p.smaller).lower(), str(p.larger).lower()))
    return pairs
