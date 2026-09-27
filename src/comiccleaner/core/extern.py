"""Locating and driving external archive tools (7-Zip, UnRAR, bsdtar).

We shell out instead of taking a pip dependency. The official 7-Zip, from
7-zip.org, reads both .cbr and .cb7 on every platform, so it is the one tool to
recommend. Debian, Ubuntu, Fedora and Homebrew build 7-Zip without its RAR
codec, whose licence forbids using it to re-create RAR compression, so theirs
reads .cb7 but not .cbr. UnRAR and bsdtar are still used when they are there.
"""

from __future__ import annotations

import functools
import logging
import ntpath
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Suppress the console window flash when shelling out on Windows. The keyword
# does not exist on POSIX, so it is passed as **kwargs rather than as a literal
# zero, which only happens to be tolerated. Any, as subprocess.run's overloads
# must still be matched by its other arguments.
_QUIET_LAUNCH: dict[str, Any] = {}
if sys.platform == "win32":
    _QUIET_LAUNCH["creationflags"] = subprocess.CREATE_NO_WINDOW

# The official build is 7zz on Linux and macOS; distributions call theirs 7z.
_SEVENZIP_NAMES = ["7zz", "7z", "7za"]

_SEVENZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "/usr/local/bin/7zz",
    "/usr/bin/7zz",
    "/opt/homebrew/bin/7zz",
    "/usr/bin/7z",
    "/usr/local/bin/7z",
    "/opt/homebrew/bin/7z",
]

_UNRAR_CANDIDATES = [
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    r"C:\Program Files\7-Zip\7z.exe",
    "/usr/bin/unrar",
    "/usr/local/bin/unrar",
]


def _override() -> str | None:
    # COMICDEDUPE_7Z is the name from before the project was renamed.
    for variable in ("COMICCLEANER_7Z", "COMICDEDUPE_7Z"):
        env_override = os.environ.get(variable)
        if env_override and Path(env_override).exists():
            return env_override
    return None


def _all_existing(names: list[str], candidates: list[str]) -> list[str]:
    """Everything found, on PATH first and then in well-known install locations."""
    found: list[str] = []
    for name in names:
        hit = shutil.which(name)
        if hit and hit not in found:
            found.append(hit)
    for path in candidates:
        if Path(path).exists() and path not in found:
            found.append(path)
    return found


def _first_existing(names: list[str], candidates: list[str]) -> str | None:
    """Prefer something on PATH, then fall back to well-known install locations."""
    found = _all_existing(names, candidates)
    return _override() or (found[0] if found else None)


@functools.lru_cache(maxsize=1)
def sevenzip_path() -> str | None:
    """Path to a 7-Zip binary, or None.

    With several installed (a distribution's 7z beside the official 7zz, say),
    the first that can read RAR wins, since only that one reads every .cbr.
    COMICCLEANER_7Z always wins.
    """
    override = _override()
    if override:
        return override
    found = _all_existing(_SEVENZIP_NAMES, _SEVENZIP_CANDIDATES)
    return next((path for path in found if _reads_rar(path)), found[0] if found else None)


@functools.lru_cache(maxsize=1)
def unrar_path() -> str | None:
    """Path to something that can extract RAR (UnRAR.exe or 7-Zip)."""
    return _first_existing(["unrar", "7z", "7zz"], _UNRAR_CANDIDATES)


def _is_unrar(path: str | None) -> bool:
    """Whether `path` is UnRAR itself, rather than the 7-Zip `unrar_path` falls back to."""
    # ntpath splits on both separators, so a Windows path reads right anywhere.
    return path is not None and ntpath.basename(path).lower().startswith("unrar")


@functools.cache
def _reads_rar(sevenz: str) -> bool:
    """Whether this 7-Zip can unpack RAR, going by the codecs `7z i` lists.

    A build without the RAR codec still lists RAR among its formats, and fails
    on every compressed .cbr. When the listing cannot be read, 7-Zip is given
    the benefit of the doubt.
    """
    try:
        listing = _run([sevenz, "i"], timeout=10).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return True
    _, found, codecs = listing.partition("Codecs:")
    return not found or "rar" in codecs.lower()


def sevenzip_reads_rar() -> bool:
    """Whether the 7-Zip in use can unpack RAR."""
    sevenz = sevenzip_path()
    return sevenz is not None and _reads_rar(sevenz)


@functools.lru_cache(maxsize=1)
def bsdtar_path() -> str | None:
    """libarchive's tar, which also reads zip/rar/7z. Ships in Windows 10+."""
    for name in ("bsdtar", "tar"):
        found = shutil.which(name)
        if not found:
            continue
        try:
            out = subprocess.run(
                [found, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                **_QUIET_LAUNCH,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "bsdtar" in out.lower() or "libarchive" in out.lower():
            return found
    return None


class ExtractionError(RuntimeError):
    pass


def describe_backends() -> dict[str, str | None]:
    """For the settings dialog / diagnostics."""
    return {
        "7-Zip": sevenzip_path(),
        # Only UnRAR itself: 7-Zip, which unrar_path falls back to, has its own row.
        "UnRAR": unrar if _is_unrar(unrar := unrar_path()) else None,
        "bsdtar": bsdtar_path(),
    }


def can_extract(kind: str) -> bool:
    """Whether any installed tool can read this kind of archive ("rar" or "7z")."""
    if bsdtar_path():
        return True
    if kind == "rar":
        return _is_unrar(unrar_path()) or (sevenzip_path() is not None and sevenzip_reads_rar())
    return sevenzip_path() is not None


def refresh_backends() -> None:
    """Forget what was detected, so a tool installed since launch is found."""
    for finder in (sevenzip_path, unrar_path, bsdtar_path, _reads_rar):
        finder.cache_clear()


SEVENZIP_URL = "https://www.7-zip.org/"


def install_hint() -> str:
    """How to get a RAR/7z reader on this platform, as a clause to end a sentence.

    Always the official 7-Zip: it reads .cbr and .cb7 everywhere. Homebrew's
    sevenzip is built without RAR decompression, so on a Mac it is not enough.
    """
    if sys.platform == "win32":
        return "Install 7-Zip (free, from 7-zip.org)"
    system = "macOS" if sys.platform == "darwin" else "Linux"
    if sevenzip_path() is not None and not sevenzip_reads_rar():
        return (
            f"Install the official 7-Zip for {system}, 7zz from 7-zip.org (the 7-Zip "
            "found here was built without RAR support, as Debian, Ubuntu and "
            "Homebrew build it)"
        )
    return f"Install the official 7-Zip for {system}, 7zz from 7-zip.org"


def _run(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[bytes]:
    log.debug("running %s", cmd)
    return subprocess.run(
        cmd,
        capture_output=True,
        # Without this the tool inherits our stdin, and a password-protected
        # archive makes 7-Zip/UnRAR wait at a prompt nobody can answer.
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        **_QUIET_LAUNCH,
    )


def extract_all(archive: Path, dest: Path, *, kind: str) -> None:
    """Extract every entry of `archive` into `dest` (already-created directory).

    `kind` is "rar" or "7z". Tries 7-Zip, then UnRAR, then bsdtar.
    """
    dest.mkdir(parents=True, exist_ok=True)
    attempts: list[tuple[str, list[str]]] = []

    sevenz = sevenzip_path()
    if sevenz:
        # x = extract with paths, -y = assume yes, -o = output dir (no space after -o)
        attempts.append((sevenz, [sevenz, "x", str(archive), f"-o{dest}", "-y", "-bso0", "-bse0"]))

    if kind == "rar":
        unrar = unrar_path()
        if unrar and unrar != sevenz:
            # UnRAR syntax differs: x <archive> <destdir>\
            attempts.append((unrar, [unrar, "x", "-y", "-idq", str(archive), f"{dest}{os.sep}"]))

    tar = bsdtar_path()
    if tar:
        attempts.append((tar, [tar, "-xf", str(archive), "-C", str(dest)]))

    if not attempts:
        raise ExtractionError(
            f"No tool found to read {archive.suffix} files. "
            f"{install_hint()}, then restart the app."
        )

    errors = []
    for tool, cmd in attempts:
        try:
            proc = _run(cmd)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{Path(tool).name}: {exc}")
            continue
        if proc.returncode == 0:
            return
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:300]
        detail = f": {stderr}" if stderr else ""
        errors.append(f"{Path(tool).name} exited {proc.returncode}{detail}")

    message = f"Could not extract {archive.name}. Tried: " + "; ".join(errors)
    if kind == "rar" and not can_extract("rar"):
        # 7-Zip without its RAR codec fails with nothing on stderr; say why.
        message += f". {install_hint()}."
    raise ExtractionError(message)
