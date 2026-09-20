"""Synthetic comic archives for the core tests."""

from __future__ import annotations

import io
import random
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

COMICINFO_TEMPLATE = """<?xml version="1.0"?>
<ComicInfo>
  <Series>Test Series</Series>
  <Number>{number}</Number>
  <PageCount>{count}</PageCount>
  <Pages>
{pages}
  </Pages>
</ComicInfo>
"""


def make_page(seed: int, size: tuple[int, int] = (400, 600), quality: int = 92) -> bytes:
    """A deterministic, visually distinct JPEG page."""
    rng = random.Random(seed)
    img = Image.new("RGB", size, (250, 248, 240))
    draw = ImageDraw.Draw(img)
    for _ in range(18):
        x0 = rng.randint(0, size[0] - 40)
        y0 = rng.randint(0, size[1] - 40)
        x1 = x0 + rng.randint(30, 160)
        y1 = y0 + rng.randint(30, 160)
        colour = (rng.randint(0, 220), rng.randint(0, 220), rng.randint(0, 220))
        draw.rectangle([x0, y0, x1, y1], fill=colour)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def make_flat_page(shade: int = 255) -> bytes:
    """A blank page - the kind that falsely groups under perceptual hashing."""
    img = Image.new("RGB", (400, 600), (shade, shade, shade))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def write_archive(
    path: Path, pages: list[bytes], *, comicinfo: bool = True
) -> Path:
    """Write a CBZ with numbered pages and an optional ComicInfo.xml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for index, data in enumerate(pages):
            zf.writestr(f"page{index + 1:03d}.jpg", data)
        if comicinfo:
            page_els = "\n".join(
                f'    <Page Image="{i}" ImageSize="{len(d)}" />'
                for i, d in enumerate(pages)
            )
            zf.writestr(
                "ComicInfo.xml",
                COMICINFO_TEMPLATE.format(
                    number=path.stem, count=len(pages), pages=page_els
                ),
            )
    return path


@pytest.fixture
def ad_page() -> bytes:
    """The injected advert that appears in every book."""
    return make_page(seed=9999)


@pytest.fixture
def library(tmp_path: Path, ad_page: bytes) -> Path:
    """Three books, each with unique story pages plus the same advert.

    Book 3's advert is re-encoded at a lower JPEG quality, so it is perceptually
    identical but not byte-identical - the case exact hashing alone would miss.
    """
    books = tmp_path / "library"

    # Books 1 and 2 carry the advert byte-for-byte.
    for number in (1, 2):
        story = [make_page(seed=number * 100 + i) for i in range(4)]
        pages = [story[0], ad_page, *story[1:]]
        write_archive(books / f"Book {number:02d}.cbz", pages)

    # Book 3 carries a recompressed copy of the same advert.
    recompressed = _recompress(ad_page, quality=55)
    story = [make_page(seed=300 + i) for i in range(4)]
    write_archive(books / "Book 03.cbz", [story[0], recompressed, *story[1:]])

    return books


def _recompress(data: bytes, quality: int) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
