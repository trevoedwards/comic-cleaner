"""Byte counts as people read them."""

from __future__ import annotations

import pytest

from comiccleaner.units import human_bytes


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (2 * 1024**4, "2.0 TB"),
    ],
)
def test_human_bytes(count: int, text: str) -> None:
    assert human_bytes(count) == text


def test_terabytes_are_the_largest_unit() -> None:
    assert human_bytes(2048 * 1024**4) == "2048.0 TB"
