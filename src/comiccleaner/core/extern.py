"""Locating and driving external archive tools (7-Zip, UnRAR, bsdtar).

We shell out instead of taking a pip dependency, because every Windows box that
has 7-Zip or WinRAR installed already has a working RAR/7z extractor.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Suppress the console window flash when shelling out on Windows. The keyword
# does not exist on POSIX, so it is passed as **kwargs rather than as a literal
# zero, which only happens to be tolerated.
_QUIET_LAUNCH: dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)

_SEVENZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "/usr/bin/7z",
    "/usr/bin/7zz",
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


def _first_existing(names: list[str], candidates: list[str]) -> str | None:
    """Prefer something on PATH, then fall back to well-known install locations."""
    # COMICDEDUPE_7Z is the name from before the project was renamed.
    for variable in ("COMICCLEANER_7Z", "COMICDEDUPE_7Z"):
        env_override = os.environ.get(variable)
        if env_override and Path(env_override).exists():
            return env_override
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for path in candidates:
        if Path(path).exists():
            return path
    return None


@functools.lru_cache(maxsize=1)
def sevenzip_path() -> str | None:
    """Path to a 7-Zip binary, or None. 7-Zip reads .7z AND .rar."""
    return _first_existing(["7z", "7zz", "7za"], _SEVENZIP_CANDIDATES)


@functools.lru_cache(maxsize=1)
def unrar_path() -> str | None:
    """Path to something that can extract RAR (UnRAR.exe or 7-Zip)."""
    return _first_existing(["unrar", "7z", "7zz"], _UNRAR_CANDIDATES)


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
        "UnRAR": unrar_path(),
        "bsdtar": bsdtar_path(),
    }


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
            "Install 7-Zip (or WinRAR) and restart the app."
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
        errors.append(f"{Path(tool).name} exited {proc.returncode}: {stderr}")

    raise ExtractionError(f"Could not extract {archive.name}. Tried: " + "; ".join(errors))
