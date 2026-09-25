# How matching works

Back to the [README](../README.md).

| Signal | Catches | Shown as |
|---|---|---|
| SHA-256 of the stored bytes | Byte-identical copies | **identical** |
| 64-bit difference hash | The same image re-encoded, rescaled or recoloured | **similar** |

The perceptual hash compares each pixel to its right-hand neighbour, so it
survives the rescaling and recompression an advert picks up on its way through
three repackagings.

Past a few thousand pages, similarity search switches to **multi-index
hashing** — each hash is split into `threshold + 1` bands, and by the pigeonhole
principle any true match must share a band exactly, so only band buckets get
compared. It is an exact speed-up, not an approximation, and the tests assert it
matches brute force. 50,000 pages at threshold 6: ~1.3 s instead of ~102 s.

> [!NOTE]
> Clustering is single-linkage — if A matches B and B matches C, all three group
> together even when A and C are further apart than the threshold. Hence the
> default of 0, and why loose values deserve a look before you bulk delete.

**Ignore** remembers every hash in the group, and hides any page within the
current threshold of one of them. An ignore made at similarity 6 therefore
still holds at 0, and one made at 0 also hides re-encoded copies once you
loosen the setting. Known junk matches the same way, and ignoring wins over it.
**Remembered Pages…** on the toolbar lists everything ignored, with a
thumbnail, and brings any of it back.

**Position** counts too. Credits follow the cover and adverts are tacked on at
the back, so a match within three pages of either end is ranked above one that
only ever turns up mid-book, and a group found *only* mid-book is flagged: it is
more likely a coincidence, or content that legitimately recurs.
