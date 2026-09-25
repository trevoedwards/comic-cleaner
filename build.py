"""Build a portable Comic Cleaner binary for the current platform.

PyInstaller cannot cross-compile, so this must run on the OS you are targeting:
Windows produces ComicCleaner.exe, macOS a ComicCleaner.app bundle, and Linux a
single ComicCleaner executable.

    python build.py [--onedir] [--console | --cli] [--clean] [--smoke-test | --smoke-only]

--cli builds comiccleaner-cli, a console build for the scan and clean commands.
Windows needs it because the normal exe is windowed; elsewhere the normal binary
already works from a terminal.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "ComicCleaner"
# The console build of the same program, for the scan and clean commands. The
# normal build is windowed, and on Windows a windowed exe has no stdout at all:
# a shell neither waits for it nor sees its output or its exit code. Lower case
# and hyphenated, so it cannot collide with ComicCleaner.exe on a
# case-insensitive filesystem.
CLI_NAME = "comiccleaner-cli"
ENTRY_POINT = ROOT / "src" / "comiccleaner" / "__main__.py"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# Qt ships a great deal we never touch. QtNetwork, QtPrintSupport, QtOpenGL and
# QtDBus are deliberately NOT excluded: QtWidgets can pull them in at import
# time, and on Linux the platform plugin needs DBus.
EXCLUDED_MODULES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "tkinter",
    "pydoc_data",
]


def venv_python() -> str:
    """Prefer the project venv, so a bare `python build.py` still works."""
    candidate = ROOT / ".venv" / ("Scripts" if IS_WINDOWS else "bin") / (
        "python.exe" if IS_WINDOWS else "python"
    )
    return str(candidate) if candidate.exists() else sys.executable


# The floor matches the build extra in pyproject.toml; the ceiling keeps a future
# major release, free to change the flags used here, from being picked up blind.
PYINSTALLER_REQUIREMENT = "pyinstaller>=6.3,<8"


def ensure_pyinstaller(python: str) -> None:
    probe = subprocess.run(
        [python, "-c", "import PyInstaller"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return
    print("Installing PyInstaller...", flush=True)
    subprocess.run(
        [python, "-m", "pip", "install", "--quiet", PYINSTALLER_REQUIREMENT], check=True
    )


def icon_argument() -> list[str]:
    """Platform-appropriate icon, if one is present.

    Linux binaries carry no embedded icon - the window icon comes from the
    bundled PNG at runtime instead.
    """
    if IS_WINDOWS:
        icon = ROOT / "assets" / "comiccleaner.ico"
    elif IS_MACOS:
        icon = ROOT / "assets" / "comiccleaner.icns"
    else:
        return []
    return ["--icon", str(icon)] if icon.is_file() else []


def add_data_argument() -> list[str]:
    """--add-data uses ; on Windows and : everywhere else."""
    separator = ";" if IS_WINDOWS else ":"
    source = ROOT / "src" / "comiccleaner" / "assets"
    return ["--add-data", f"{source}{separator}comiccleaner/assets"]


def output_path(onedir: bool, *, cli: bool = False, console: bool = False) -> Path:
    dist = ROOT / "dist"
    name = CLI_NAME if cli else APP_NAME
    if IS_MACOS and not (cli or console):
        # --windowed on macOS always produces a .app bundle, onefile or onedir.
        return dist / f"{name}.app"
    binary = f"{name}.exe" if IS_WINDOWS else name
    return dist / name / binary if onedir else dist / binary


def build(args: argparse.Namespace) -> Path:
    python = venv_python()
    ensure_pyinstaller(python)
    name = CLI_NAME if args.cli else APP_NAME

    if args.clean:
        for folder in ("build", "dist"):
            shutil.rmtree(ROOT / folder, ignore_errors=True)
        for spec in ROOT.glob("*.spec"):
            spec.unlink()

    command = [
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        name,
        "--paths",
        str(ROOT / "src"),
        "--collect-submodules",
        "comiccleaner",
        # --collect-submodules only gathers code, so the icon needs saying too.
        *add_data_argument(),
        "--onedir" if args.onedir else "--onefile",
        "--console" if (args.console or args.cli) else "--windowed",
        *icon_argument(),
    ]
    for module in EXCLUDED_MODULES:
        command += ["--exclude-module", module]
    command.append(str(ENTRY_POINT))

    print(f"Building {name} for {sys.platform}...", flush=True)
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller failed with exit code {result.returncode}")

    produced = output_path(args.onedir, cli=args.cli, console=args.console)
    if not produced.exists():
        raise SystemExit(f"Build reported success but {produced} is missing")

    size = _size_of(produced) / (1024 * 1024)
    elapsed = time.monotonic() - started
    print(f"\nBuilt {produced.relative_to(ROOT)} ({size:.1f} MB in {elapsed:.0f}s)")
    if not IS_WINDOWS:
        os.chmod(_executable_within(produced), 0o755)
    return produced


def _size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _executable_within(produced: Path) -> Path:
    """The runnable file, which is nested inside a .app bundle on macOS."""
    if produced.suffix == ".app":
        return produced / "Contents" / "MacOS" / produced.stem
    if produced.is_dir():
        return produced / produced.name
    return produced


CRASH_MARKERS = (
    "Traceback (most recent call last)",
    "ImportError",
    "ModuleNotFoundError",
    "Fatal Python error",
)


def _terminate_tree(process: subprocess.Popen, name: str) -> None:
    """Kill the process and anything it spawned.

    A one-file build runs a bootloader that launches the real app as a child.
    Killing only the parent leaves that child alive, still holding the output
    pipes open and keeping an exclusive lock on the binary.
    """
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
        )
        # The bootloader child may be reparented, so sweep by name as well.
        subprocess.run(["taskkill", "/F", "/IM", f"{name}.exe"], capture_output=True)
    else:
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)


def smoke_test(produced: Path, seconds: int = 15) -> int:
    """Launch the binary and fail on an early exit or any traceback.

    Surviving is not proof on its own: the bootloader parent can outlive a
    crashed child, so the process lingers while the app is already dead. Any
    traceback counts as a failure regardless of whether it is still running.
    """
    executable = _executable_within(produced)
    print(f"\nSmoke-testing {executable.name}...", flush=True)

    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    # Output goes to files rather than pipes: a surviving grandchild keeps pipe
    # handles open, which would make a post-kill read block forever.
    with tempfile.TemporaryDirectory() as workspace:
        out_file = Path(workspace) / "stdout.txt"
        err_file = Path(workspace) / "stderr.txt"
        with out_file.open("wb") as out, err_file.open("wb") as err:
            process = subprocess.Popen(
                [str(executable)],
                stdout=out,
                stderr=err,
                env=environment,
                cwd=workspace,
                # Own process group, so the whole tree can be signalled at once.
                start_new_session=not IS_WINDOWS,
            )
            try:
                process.wait(timeout=seconds)
                exited_early = True
            except subprocess.TimeoutExpired:
                exited_early = False
                _terminate_tree(process, executable.stem)

        output = (
            err_file.read_text("utf-8", errors="replace")
            + out_file.read_text("utf-8", errors="replace")
        ).strip()

    if exited_early:
        print(f"FAILED - exited on its own with code {process.returncode}")
        if output:
            print(output)
        return 1

    if any(marker in output for marker in CRASH_MARKERS):
        print("FAILED - process survived but the app crashed on startup:")
        print(output)
        return 1

    print(f"PASS - ran {seconds}s with no startup errors.")
    if output:
        print(f"Startup output:\n{output}")
    return 0


def _tiny_png(size: int = 16) -> bytes:
    """A small valid PNG, built by hand so this script needs no Pillow."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(
        b"\x00"
        + b"".join(
            bytes((x * 16 % 256, y * 16 % 256, (x ^ y) * 16 % 256)) for x in range(size)
        )
        for y in range(size)
    )
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def smoke_test_cli(produced: Path) -> int:
    """Run the console build's headless commands for real.

    Launching it bare would open the GUI with a console attached, which proves
    little. Instead: the self-test (codecs survived packaging), then a JSON scan
    of a generated book, whose output has to parse and add up.
    """
    import json
    import zipfile

    executable = _executable_within(produced)
    print(f"\nSmoke-testing {executable.name}...", flush=True)
    with tempfile.TemporaryDirectory() as workspace:
        books = Path(workspace) / "books"
        books.mkdir()
        with zipfile.ZipFile(books / "Smoke Test.cbz", "w") as zf:
            zf.writestr("page1.png", _tiny_png(16))
            zf.writestr("page2.png", _tiny_png(24))

        checks = [
            (["--self-test"], None),
            (
                ["scan", str(books), "--json", "--no-cache", "--quiet"],
                lambda out: json.loads(out)["library"]["pages"] == 2,
            ),
        ]
        for arguments, verify in checks:
            label = " ".join(arguments[:1])
            try:
                result = subprocess.run(
                    [str(executable), *arguments],
                    capture_output=True, text=True, cwd=workspace, timeout=180,
                )
            except subprocess.TimeoutExpired:
                print(f"FAILED - {label} did not finish")
                return 1
            output = (result.stdout + result.stderr).strip()
            try:
                ok = result.returncode == 0 and (verify is None or verify(result.stdout))
            except (ValueError, KeyError, TypeError):
                ok = False
            if not ok or any(marker in output for marker in CRASH_MARKERS):
                print(f"FAILED - {label} (exit {result.returncode}):")
                print(output)
                return 1
            print(f"PASS - {label}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Emit a folder instead of one file. Starts faster, easier to debug.",
    )
    parser.add_argument(
        "--console", action="store_true", help="Keep the console so tracebacks show."
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help=f"Build the console variant, {CLI_NAME}, for the scan and clean commands.",
    )
    parser.add_argument(
        "--clean", action="store_true", help="Wipe build/ and dist/ first."
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Launch the result afterwards and check it does not crash.",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="Skip building and only smoke-test what is already in dist/.",
    )
    args = parser.parse_args()

    check = smoke_test_cli if args.cli else smoke_test
    if args.smoke_only:
        produced = output_path(args.onedir, cli=args.cli, console=args.console)
        if not produced.exists():
            raise SystemExit(f"Nothing to test: {produced} does not exist")
        return check(produced)

    produced = build(args)
    if args.smoke_test:
        return check(produced)

    hint = "python build.py --smoke-test"
    print(f"Verify it starts with: {hint}")
    print("Note: .cbr/.cb7 support needs 7-Zip or unrar on the target machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
