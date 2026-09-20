"""Turning per-page hashes into duplicate groups."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from .hashing import DHASH_BITS, hamming_matrix
from .model import ArchiveInfo, Decision, DuplicateGroup, MatchKind, PageEntry

log = logging.getLogger(__name__)

# Cap on the temporary array built per comparison chunk (~50 MB), so a huge
# library never spikes memory during clustering.
_CHUNK_BUDGET_BYTES = 50_000_000

# Below this many distinct hashes, a full pairwise sweep beats building an index.
_BRUTE_FORCE_LIMIT = 4000


@dataclass(slots=True)
class GroupingOptions:
    """Tunables for what counts as a duplicate worth showing."""

    threshold: int = 0        # max Hamming distance on the 64-bit dhash
    min_pages: int = 2        # a group needs at least this many page occurrences
    min_archives: int = 1     # ...spread over at least this many archives
    include_flat: bool = False  # surface blank/solid-colour pages
    skip_first_page: bool = False   # never group a book's cover
    skip_last_page: bool = False


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _bands(threshold: int) -> list[tuple[int, int]]:
    """Split 64 bits into `threshold + 1` disjoint bands, as evenly as possible."""
    count = threshold + 1
    width, extra = divmod(DHASH_BITS, count)
    bands, offset = [], 0
    for index in range(count):
        size = width + (1 if index < extra else 0)
        if size == 0:
            break
        bands.append((offset, size))
        offset += size
    return bands


def _union_subset(
    uf: _UnionFind, hashes: np.ndarray, indices: np.ndarray, threshold: int
) -> None:
    """Compare every pair within `indices` and union those within `threshold`.

    Rows are processed in chunks so the temporary distance matrix stays bounded
    no matter how large the subset is.
    """
    subset = hashes[indices]
    n = len(subset)
    if n < 2:
        return
    chunk = max(1, _CHUNK_BUDGET_BYTES // max(1, n * 8))
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        dist = hamming_matrix(subset[start:stop], subset)
        rows, cols = np.nonzero(dist <= threshold)
        for r, c in zip(rows, cols, strict=True):
            i, j = start + int(r), int(c)
            if i < j:  # skip the diagonal and each mirrored pair
                uf.union(int(indices[i]), int(indices[j]))


def _cluster_by_distance(hashes: np.ndarray, threshold: int) -> list[list[int]]:
    """Group hash indices whose pairwise Hamming distance is <= threshold.

    Clustering is transitive (single-linkage): if A matches B and B matches C,
    all three land in one group even if A and C differ by more than `threshold`.
    That is the standard trade-off, and is why the threshold should stay small.

    Large sets use multi-index hashing. By the pigeonhole principle, splitting
    each hash into `threshold + 1` disjoint bands means two hashes differing in
    at most `threshold` bits must share at least one band exactly. Comparing
    only within band buckets finds every true match - it is an exact speed-up,
    not an approximation - while skipping almost all of the n^2 pairs.
    """
    n = len(hashes)
    if n == 0:
        return []
    if threshold <= 0:
        return [[i] for i in range(n)]

    uf = _UnionFind(n)
    if n <= _BRUTE_FORCE_LIMIT:
        # Small sets are faster to sweep wholesale than to index.
        _union_subset(uf, hashes, np.arange(n), threshold)
    else:
        for offset, width in _bands(threshold):
            keys = (hashes >> np.uint64(offset)) & np.uint64((1 << width) - 1)
            order = np.argsort(keys, kind="stable")
            boundaries = np.flatnonzero(np.diff(keys[order])) + 1
            for bucket in np.split(order, boundaries):
                _union_subset(uf, hashes, bucket, threshold)

    buckets: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        buckets[uf.find(i)].append(i)
    return list(buckets.values())


def collect_pages(
    archives: Iterable[ArchiveInfo], options: GroupingOptions
) -> list[PageEntry]:
    """Every scannable page, minus whatever the options exclude."""
    pages: list[PageEntry] = []
    for archive in archives:
        if archive.error:
            continue
        last = archive.page_count - 1
        for page in archive.pages:
            if page.error:
                continue
            if options.skip_first_page and page.index == 0:
                continue
            if options.skip_last_page and page.index == last:
                continue
            if page.flat and not options.include_flat:
                continue
            pages.append(page)
    return pages


def build_groups(
    archives: Iterable[ArchiveInfo],
    options: GroupingOptions | None = None,
    ignored: set[str] | None = None,
) -> list[DuplicateGroup]:
    """Cluster pages into duplicate groups, best candidates first."""
    opts = options or GroupingOptions()
    ignored = ignored or set()

    pages = collect_pages(archives, opts)
    if not pages:
        return []

    # Collapse to unique perceptual hashes first — identical ads share a dhash,
    # so this usually shrinks the comparison set by an order of magnitude.
    by_hash: dict[int, list[PageEntry]] = defaultdict(list)
    for page in pages:
        by_hash[page.dhash].append(page)

    unique = np.array(sorted(by_hash), dtype=np.uint64)
    clusters = _cluster_by_distance(unique, opts.threshold)

    groups: list[DuplicateGroup] = []
    for cluster in clusters:
        members: list[PageEntry] = []
        for idx in cluster:
            members.extend(by_hash[int(unique[idx])])
        if len(members) < opts.min_pages:
            continue
        if len({p.archive for p in members}) < opts.min_archives:
            continue

        distinct_content = {p.content_sha for p in members}
        kind = MatchKind.EXACT if len(distinct_content) == 1 else MatchKind.SIMILAR
        gid = group_id(members)
        if gid in ignored:
            continue

        members.sort(key=lambda p: (str(p.archive).lower(), p.index))
        groups.append(DuplicateGroup(gid=gid, kind=kind, pages=members))

    groups.sort(key=_group_rank, reverse=True)
    return groups


def group_id(members: list[PageEntry]) -> str:
    """Stable id for a group, so ignore decisions survive a rescan.

    Derived from the lowest dhash in the cluster: adding or removing a book that
    contains the same ad does not change it.
    """
    return f"{min(p.dhash for p in members):016x}"


def _group_rank(group: DuplicateGroup) -> tuple:
    # Spread across many books is the strongest "this is an ad" signal, then
    # how much space removing it frees.
    return (group.archive_count, group.recoverable_bytes, group.page_count)


def sort_groups(groups: list[DuplicateGroup], key: str) -> list[DuplicateGroup]:
    """Re-sort for the UI. `key` is one of: books, space, count, size."""
    keyfns = {
        "books": lambda g: (g.archive_count, g.recoverable_bytes),
        "space": lambda g: (g.recoverable_bytes, g.archive_count),
        "count": lambda g: (g.page_count, g.recoverable_bytes),
        "size": lambda g: (g.representative.width * g.representative.height,),
    }
    fn = keyfns.get(key, keyfns["books"])
    return sorted(groups, key=fn, reverse=True)


def summarise(groups: list[DuplicateGroup]) -> dict[str, int]:
    """Headline numbers for the status bar."""
    marked = [g for g in groups if g.decision is Decision.DELETE]
    return {
        "groups": len(groups),
        "pages": sum(g.page_count for g in groups),
        "marked_groups": len(marked),
        "marked_pages": sum(len(g.pages_to_remove()) for g in marked),
        "recoverable": sum(g.recoverable_bytes for g in marked),
    }
