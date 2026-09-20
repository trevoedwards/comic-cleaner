"""Reading comic archives (cbz/cbr/cb7) and writing cbz."""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from .extern import ExtractionError, extract_all
from .model import IMAGE_SUFFIXES, NON_PAGE_NAMES, ArchiveKind

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z"}

# Windows path separator, spelled via chr() so it survives any escaping layer.
SEP_ALT = chr(92)


class ArchiveError(RuntimeError):
    pass


def is_page_name(name: str) -> bool:
    """True if an archive entry looks like a comic page image."""
    if name.endswith(("/", SEP_ALT)):
        return False
    base = name.rsplit("/", 1)[-1].rsplit(SEP_ALT, 1)[-1]
    if not base or base.startswith("."):
        return False
    if base.lower() in NON_PAGE_NAMES:
        return False
    if "__MACOSX" in name:  # Mac resource-fork junk
        return False
    return Path(base).suffix.lower() in IMAGE_SUFFIXES


def natural_key(name: str) -> tuple:
    """Sort '9.jpg' before '10.jpg', the way a reader would order pages."""
    parts = re.split(r"(\d+)", name.lower())
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts)


def detect_kind(path: Path) -> ArchiveKind:
    """Sniff the container by magic bytes, falling back to the extension.

    Extensions lie constantly in comic libraries — plenty of .cbr files are zips.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError:
        head = b""
    if head[:2] == b"PK":
        return ArchiveKind.ZIP
    if head[:4] == b"Rar!":
        return ArchiveKind.RAR
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return ArchiveKind.SEVENZIP
    suffix = path.suffix.lower()
    if suffix in (".cbz", ".zip"):
        return ArchiveKind.ZIP
    if suffix in (".cbr", ".rar"):
        return ArchiveKind.RAR
    if suffix in (".cb7", ".7z"):
        return ArchiveKind.SEVENZIP
    return ArchiveKind.UNKNOWN


class ComicArchive:
    """Uniform read access over cbz/cbr/cb7.

    ZIP is read in-process. RAR/7z are extracted once to a temp directory via an
    external tool, because that is both faster and more compatible than any pure
    Python implementation.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.kind = detect_kind(self.path)
        self._zip: zipfile.ZipFile | None = None
        self._tmpdir: Path | None = None
        self._extracted: dict[str, Path] | None = None

    def __enter__(self) -> ComicArchive:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        if self.kind is ArchiveKind.ZIP:
            try:
                self._zip = zipfile.ZipFile(self.path)
            except (zipfile.BadZipFile, OSError) as exc:
                raise ArchiveError(f"Not a readable zip: {exc}") from exc
        elif self.kind in (ArchiveKind.RAR, ArchiveKind.SEVENZIP):
            self._extract_to_temp()
        else:
            raise ArchiveError(f"Unrecognised archive format: {self.path.name}")

    def _extract_to_temp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="comiccleaner-"))
        self._tmpdir = tmp
        try:
            extract_all(self.path, tmp, kind=self.kind.value)
        except ExtractionError as exc:
            self.close()
            raise ArchiveError(str(exc)) from exc
        # Map archive-relative entry names to the files on disk.
        mapping: dict[str, Path] = {}
        for file in tmp.rglob("*"):
            if file.is_file():
                mapping[file.relative_to(tmp).as_posix()] = file
        if not mapping:
            self.close()
            raise ArchiveError(f"{self.path.name} extracted to nothing")
        self._extracted = mapping

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        self._extracted = None

    # -- reading -----------------------------------------------------------
    def entry_names(self) -> list[str]:
        """Every non-directory entry, so we can preserve ComicInfo.xml etc."""
        if self._zip is not None:
            return [i.filename for i in self._zip.infolist() if not i.is_dir()]
        if self._extracted is not None:
            return list(self._extracted)
        raise ArchiveError("archive is not open")

    def page_names(self) -> list[str]:
        """Page entries only, in reading order."""
        return sorted((n for n in self.entry_names() if is_page_name(n)), key=natural_key)

    def read(self, name: str) -> bytes:
        if self._zip is not None:
            return self._zip.read(name)
        if self._extracted is not None:
            file = self._extracted.get(name)
            if file is None:
                raise ArchiveError(f"missing entry {name}")
            return file.read_bytes()
        raise ArchiveError("archive is not open")

    def entry_size(self, name: str) -> int:
        """Uncompressed size of an entry, without reading it."""
        if self._zip is not None:
            return self._zip.getinfo(name).file_size
        if self._extracted is not None:
            file = self._extracted.get(name)
            return file.stat().st_size if file else 0
        raise ArchiveError("archive is not open")

    def stored_size(self, name: str) -> int:
        """On-disk cost of an entry — what we actually reclaim by removing it."""
        if self._zip is not None:
            return self._zip.getinfo(name).compress_size
        return self.entry_size(name)

    def iter_pages(self) -> Iterator[tuple[str, bytes]]:
        for name in self.page_names():
            yield name, self.read(name)


def write_cbz(dest: Path, entries: list[tuple[str, bytes]], *, compress: bool = False) -> None:
    """Write a CBZ. Images are stored uncompressed by default — they already are.

    Deflating a JPEG costs CPU and saves ~0%; XML is the one thing worth compressing.
    """
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(dest, "w") as zf:
        for name, data in entries:
            per_entry = zipfile.ZIP_DEFLATED if name.lower().endswith(".xml") else mode
            zf.writestr(name, data, compress_type=per_entry)
