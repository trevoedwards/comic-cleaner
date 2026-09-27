"""Real RAR4 and RAR5 archives, read through each RAR reader installed here.

The synthetic fixtures are all zips, since only RARLAB's `rar` can make a RAR.
These are libarchive's test archives (see tests/data/rar/README.md). Each one is
read with every tool present in turn, 7-Zip, UnRAR and bsdtar, so CI covers the
official 7-Zip on Linux and macOS and Windows' own tar.exe.
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest

from comiccleaner.core import extern
from comiccleaner.core.archive import ComicArchive, write_cbz
from comiccleaner.core.model import ArchiveKind

DATA = Path(__file__).parent / "data" / "rar"

# Every regular file in each archive and its SHA-256, as both the official
# 7-Zip and bsdtar extract it. Links are not listed: a symlink inside an
# archive is skipped, never followed (see _extracted_files).
EXPECTED: dict[str, dict[str, str]] = {
    "test_read_format_rar.rar": {  # plus the symlink testlink -> test.txt
        "test.txt": "5a5f16e01faf8adf92eb4499a2d3e93010c4b41dbb7f698f4a8466d9f58e6dd2",
        "testdir/test.txt": "5a5f16e01faf8adf92eb4499a2d3e93010c4b41dbb7f698f4a8466d9f58e6dd2",
    },
    "test_read_format_rar5_stored.rar": {
        "helloworld.txt": "fef9ad8cf601b43f76c6320075f62267c6e5c0a526d750a70b80c919a4a0aad8",
    },
    "test_read_format_rar5_compressed.rar": {
        "test.bin": "588870a2dade35c2650fbb7898c9a9c7f21fce7c281198604e8d0c9737f2c375",
    },
    "test_read_format_rar5_multiple_files_solid.rar": {
        "test1.bin": "7d89f86f9f69d744ffff3fc043e15bf89fc3ffc134ffcbb31d164a99bb8b67b0",
        "test2.bin": "f81e6fceeeab366306b23466bf6bb3aac2875e0906dc20a8652be0696ceb15a2",
        "test3.bin": "5e621f2b6ce8fed758c3df8221f994eda55d1e432c7cc4349c34a30ec2e1c43d",
        "test4.bin": "2627f40180217252956edb9a426e8d3e344adaf89019d3bccbe04f6c3416dcdd",
    },
    "test_read_format_rar5_unicode.rar": {  # plus a symlink to 👋🌎.txt
        "Ⓗⓐⓡⓓ Ⓛⓘⓝⓚ.txt": "315f5bdb76d078c43b8ac0064e4a0164612b1fce77c869345bfc94c75894edd3",
        "👋🌎.txt": "315f5bdb76d078c43b8ac0064e4a0164612b1fce77c869345bfc94c75894edd3",
    },
}


def _rar_readers() -> list[str]:
    """The tools here that can read RAR."""
    extern.refresh_backends()
    found = []
    if extern.sevenzip_path() is not None and extern.sevenzip_reads_rar():
        found.append("7-Zip")
    if extern._is_unrar(extern.unrar_path()):
        found.append("UnRAR")
    if extern.bsdtar_path() is not None:
        found.append("bsdtar")
    return found


READERS = _rar_readers()
needs_a_reader = pytest.mark.skipif(not READERS, reason="no RAR reader installed")


def _only(monkeypatch, reader: str) -> None:
    """Leave `reader` as the one tool extract_all can try."""
    sevenz, unrar, tar = extern.sevenzip_path(), extern.unrar_path(), extern.bsdtar_path()
    monkeypatch.setattr(extern, "sevenzip_path", lambda: sevenz if reader == "7-Zip" else None)
    monkeypatch.setattr(extern, "unrar_path", lambda: unrar if reader == "UnRAR" else None)
    monkeypatch.setattr(extern, "bsdtar_path", lambda: tar if reader == "bsdtar" else None)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_book(tmp_path: Path, name: str) -> Path:
    book = tmp_path / "Book.cbr"
    shutil.copyfile(DATA / name, book)
    return book


def test_every_fixture_is_there_and_is_a_rar():
    for name in EXPECTED:
        assert (DATA / name).read_bytes()[:4] == b"Rar!", name


@needs_a_reader
@pytest.mark.parametrize("reader", READERS or ["none"])
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_rar_reader_gets_every_file_right(tmp_path, monkeypatch, reader, name):
    book = _as_book(tmp_path, name)
    _only(monkeypatch, reader)

    with ComicArchive(book) as archive:
        assert archive.kind is ArchiveKind.RAR
        assert sorted(archive.entry_names()) == sorted(EXPECTED[name])
        for entry, digest in EXPECTED[name].items():
            assert _sha256(archive.read(entry)) == digest, entry


@needs_a_reader
def test_links_inside_a_rar_are_left_out(tmp_path):
    """A symlink in a crafted archive could point anywhere on this machine."""
    with ComicArchive(_as_book(tmp_path, "test_read_format_rar.rar")) as archive:
        assert "testlink" not in archive.entry_names()
    with ComicArchive(_as_book(tmp_path, "test_read_format_rar5_unicode.rar")) as archive:
        assert not any("Link" in n and "Ⓗ" not in n for n in archive.entry_names())


@needs_a_reader
def test_a_solid_rar_is_rebuilt_as_a_cbz_without_the_removed_entry(tmp_path):
    name = "test_read_format_rar5_multiple_files_solid.rar"
    cleaned = tmp_path / "Book.cbz"
    with ComicArchive(_as_book(tmp_path, name)) as archive:
        keep = [n for n in archive.entry_names() if n != "test2.bin"]
        write_cbz(cleaned, archive, keep)

    with zipfile.ZipFile(cleaned) as zf:
        assert sorted(zf.namelist()) == ["test1.bin", "test3.bin", "test4.bin"]
        for entry in zf.namelist():
            assert _sha256(zf.read(entry)) == EXPECTED[name][entry]
