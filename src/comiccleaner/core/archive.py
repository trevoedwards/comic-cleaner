"""Reading comic archives (cbz/cbr/cb7) and writing cbz."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import IO

from .extern import ExtractionError, extract_all
from .model import IMAGE_SUFFIXES, NON_PAGE_NAMES, ArchiveKind

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z"}

# What a folder walk picks up. Plain .rar and .7z files are more often ordinary
# archives (installers, backups, downloads) than comics, so a folder import skips
# them; a file chosen by name is still accepted with any suffix above.
WALKED_SUFFIXES = {".cbz", ".zip", ".cbr", ".cb7"}

# Prefix of the temp file a removal writes before swapping it in for the original.
TEMP_PREFIX = ".comiccleaner-"

# Windows path separator, spelled via chr() so it survives any escaping layer.
SEP_ALT = chr(92)

# Read size when streaming an entry from one archive into another.
_COPY_CHUNK = 1 << 20

# Everything zipfile raises for one bad entry: a bad CRC, truncated data,
# encryption or an exotic compression method.
_ZIP_ENTRY_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError)


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
        mapping = _extracted_files(tmp)
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
            # A zip may hold several entries under one name, but zipfile can only
            # ever read the last of them. Listing the name once keeps pages unique
            # (the hash cache keys on it) and avoids reporting a book as a
            # duplicate of itself.
            names = (i.filename for i in self._zip.infolist() if not i.is_dir())
            return list(dict.fromkeys(names))
        if self._extracted is not None:
            return list(self._extracted)
        raise ArchiveError("archive is not open")

    def page_names(self) -> list[str]:
        """Page entries only, in reading order."""
        return sorted((n for n in self.entry_names() if is_page_name(n)), key=natural_key)

    def read(self, name: str) -> bytes:
        if self._zip is not None:
            try:
                return self._zip.read(name)
            except _ZIP_ENTRY_ERRORS as exc:
                # One bad entry, not a reason to abort the whole scan/run.
                raise ArchiveError(f"cannot read {name}: {exc}") from exc
        if self._extracted is not None:
            return self._extracted_file(name).read_bytes()
        raise ArchiveError("archive is not open")

    def copy_to(self, name: str, dest: IO[bytes]) -> None:
        """Stream one entry into `dest`, never holding the whole entry in memory."""
        if self._zip is not None:
            try:
                with self._zip.open(name) as src:
                    shutil.copyfileobj(src, dest, _COPY_CHUNK)
            except _ZIP_ENTRY_ERRORS as exc:
                raise ArchiveError(f"cannot read {name}: {exc}") from exc
            return
        if self._extracted is not None:
            with self._extracted_file(name).open("rb") as src:
                shutil.copyfileobj(src, dest, _COPY_CHUNK)
            return
        raise ArchiveError("archive is not open")

    def _extracted_file(self, name: str) -> Path:
        assert self._extracted is not None
        file = self._extracted.get(name)
        if file is None:
            raise ArchiveError(f"missing entry {name}")
        # The listing already left links out; this keeps it so if the tree changed.
        if file.is_symlink():
            raise ArchiveError(f"refusing to follow a link: {name}")
        return file

    def entry_size(self, name: str) -> int:
        """Uncompressed size of an entry, without reading it."""
        if self._zip is not None:
            return self._zip.getinfo(name).file_size
        if self._extracted is not None:
            file = self._extracted.get(name)
            return file.stat(follow_symlinks=False).st_size if file else 0
        raise ArchiveError("archive is not open")

    def entry_date(self, name: str) -> tuple[int, int, int, int, int, int] | None:
        """The zip timestamp of an entry, or None where there is none (RAR/7z)."""
        if self._zip is not None:
            try:
                return self._zip.getinfo(name).date_time
            except KeyError:
                return None
        return None

    def stored_size(self, name: str) -> int:
        """On-disk cost of an entry — what we actually reclaim by removing it."""
        if self._zip is not None:
            return self._zip.getinfo(name).compress_size
        return self.entry_size(name)


def _extracted_files(root: Path) -> dict[str, Path]:
    """Archive-relative entry names mapped to the regular files under `root`.

    A crafted RAR or 7z can hold symlinks, and following one would hash, or
    re-pack into the cleaned book, some file elsewhere on this machine. Links
    (to files or folders, and Windows junctions) are skipped, never followed,
    and anything that still resolves outside `root` is refused.
    """
    base = root.resolve()
    mapping: dict[str, Path] = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        here = Path(folder)
        dirs[:] = [d for d in dirs if not _is_link(here / d)]
        for filename in files:
            file = here / filename
            if _is_link(file) or not file.is_file():
                log.warning("skipping link or special file in archive: %s", filename)
                continue
            if not file.resolve().is_relative_to(base):
                log.warning("skipping entry that escapes the archive: %s", filename)
                continue
            mapping[file.relative_to(root).as_posix()] = file
    return mapping


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)  # Python 3.12+
    return path.is_symlink() or bool(is_junction and is_junction())


def write_cbz(
    dest: Path,
    source: ComicArchive,
    names: Iterable[str],
    *,
    replace: dict[str, bytes] | None = None,
    compress: bool = False,
) -> None:
    """Write a CBZ of `names` from `source`, streaming each entry straight across.

    Only one read buffer is held at a time, so a book never has to fit in memory.
    `replace` gives new bytes for small entries, such as an updated ComicInfo.xml.

    Images are stored uncompressed by default — they already are. Deflating a
    JPEG costs CPU and saves ~0%; XML is the one thing worth compressing.

    Entries copied from a zip keep their original timestamps, so a cleaned book
    still shows when each page was made; pages extracted from a RAR or 7z have
    none to keep and are stamped with the time of writing.
    """
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    replace = replace or {}
    now = time.localtime(time.time())[:6]
    with zipfile.ZipFile(dest, "w") as zf:
        for name in names:
            per_entry = zipfile.ZIP_DEFLATED if name.lower().endswith(".xml") else mode
            if name in replace:
                zf.writestr(name, replace[name], compress_type=per_entry)
                continue
            info = zipfile.ZipInfo(name, date_time=source.entry_date(name) or now)
            info.compress_type = per_entry
            info.external_attr = 0o600 << 16  # what writestr gives a named entry
            # Known up front, so zipfile can choose zip64 for a huge entry.
            info.file_size = source.entry_size(name)
            with zf.open(info, "w") as out:
                source.copy_to(name, out)
