"""Image hashing: exact content digest plus a perceptual difference hash."""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFile

log = logging.getLogger(__name__)

# Comic archives are full of slightly-corrupt scans; decode what we can.
ImageFile.LOAD_TRUNCATED_IMAGES = True
# Guard against decompression-bomb DoS while still allowing big double-page scans.
Image.MAX_IMAGE_PIXELS = 300_000_000

# dhash works on a (HASH_SIZE+1) x HASH_SIZE grid -> HASH_SIZE**2 bits.
HASH_SIZE = 8
DHASH_BITS = HASH_SIZE * HASH_SIZE

# Below this grayscale standard deviation a page is treated as "flat" (blank,
# solid black, plain colour). Such pages collide perceptually with each other
# even when unrelated, so the UI warns before bulk-deleting them.
FLAT_STDDEV_THRESHOLD = 6.0


class DecodeError(RuntimeError):
    pass


@dataclass(slots=True)
class ImageDigest:
    width: int
    height: int
    content_sha: str
    dhash: int
    flat: bool


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_reduced(
    data: bytes, target: int, mode: str | None = None
) -> tuple[Image.Image, int, int]:
    """Open an image, asking the JPEG decoder for a reduced-size decode.

    draft() lets libjpeg decode at 1/2, 1/4 or 1/8 scale straight from the DCT
    coefficients, which is several times faster than a full decode plus resize.

    It also rewrites ``img.size`` in place, so the true dimensions are captured
    first and returned alongside the (possibly reduced) image.
    """
    img = Image.open(io.BytesIO(data))
    width, height = img.size
    # Not a JPEG, or no reduced mode available - decode at full size instead.
    with contextlib.suppress(AttributeError, ValueError):
        img.draft(mode, (target, target))
    return img, width, height


def dhash_from_image(img: Image.Image) -> tuple[int, bool]:
    """Row-wise difference hash, plus whether the image is near-uniform.

    Each bit records whether a pixel is brighter than the one to its right, which
    makes the hash invariant to scaling and to overall brightness/contrast shifts
    — exactly the changes a re-encoded ad page goes through.
    """
    small = img.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS)
    pixels = np.asarray(small, dtype=np.int16)
    diff = pixels[:, 1:] > pixels[:, :-1]

    bits = 0
    for bit in diff.flatten():
        bits = (bits << 1) | int(bit)

    # Judge flatness on a slightly larger sample than the 9x8 hash grid.
    sample = np.asarray(img.convert("L").resize((32, 32), Image.Resampling.BILINEAR))
    flat = bool(sample.std() < FLAT_STDDEV_THRESHOLD)
    return bits, flat


def digest_image(data: bytes) -> ImageDigest:
    """Full digest for one page's bytes. Raises DecodeError on unreadable images."""
    sha = content_digest(data)
    try:
        img, width, height = _open_reduced(data, 64, mode="L")
        bits, flat = dhash_from_image(img)
    except Exception as exc:  # Pillow raises a wide variety of types
        raise DecodeError(f"could not decode image: {exc}") from exc
    return ImageDigest(width=width, height=height, content_sha=sha, dhash=bits, flat=flat)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


_POPCOUNT_LUT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def hamming_matrix(hashes: np.ndarray, other: np.ndarray) -> np.ndarray:
    """Pairwise Hamming distance between two uint64 hash arrays.

    Returns a (len(hashes), len(other)) uint8 matrix. Callers pass `hashes` in
    chunks so a large library never materialises an N*N array.
    """
    xor = hashes[:, None] ^ other[None, :]
    # Popcount via a 256-entry lookup over the 8 bytes of each uint64.
    as_bytes = xor.view(np.uint8).reshape(*xor.shape, 8)
    return _POPCOUNT_LUT[as_bytes].sum(axis=-1).astype(np.uint8)


def make_thumbnail(data: bytes, size: int = 256) -> bytes:
    """PNG thumbnail bytes for the review grid.

    mode=None keeps the image in colour; drafting to "L" here would render every
    thumbnail greyscale.
    """
    img, _, _ = _open_reduced(data, size, mode=None)
    img = img.convert("RGB")
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
