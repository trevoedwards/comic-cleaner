"""Build a portable Comic Cleaner binary for the current platform.

PyInstaller cannot cross-compile, so this must run on the OS you are targeting:
Windows produces ComicCleaner.exe, macOS a ComicCleaner.app bundle, and Linux a
single ComicCleaner executable.

    python build.py [--onedir] [--console | --cli] [--clean] [--smoke-test | --smoke-only]
    python build.py --appimage [--smoke-test]      # Linux only

--cli builds comiccleaner-cli, a console build for the scan and clean commands.
Windows needs it because the normal exe is windowed; elsewhere the normal binary
already works from a terminal.

--appimage builds ComicCleaner-x86_64.AppImage in dist/: a one-folder build in
an AppDir, packed by a pinned appimagetool. It is built under build/appimage/,
so it does not collide with the one-file dist/ComicCleaner.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
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


def output_path(
    onedir: bool, *, cli: bool = False, console: bool = False, dist: Path | None = None
) -> Path:
    dist = dist or ROOT / "dist"
    name = CLI_NAME if cli else APP_NAME
    if IS_MACOS and not (cli or console):
        # --windowed on macOS always produces a .app bundle, onefile or onedir.
        return dist / f"{name}.app"
    binary = f"{name}.exe" if IS_WINDOWS else name
    return dist / name / binary if onedir else dist / binary


def build(args: argparse.Namespace, dist: Path | None = None) -> Path:
    """Run PyInstaller; into `dist` (with its own work and spec folders) if given."""
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
    if dist is not None:
        work = dist.parent / "work"
        command += ["--distpath", str(dist), "--workpath", str(work),
                    "--specpath", str(dist.parent)]
    for module in EXCLUDED_MODULES:
        command += ["--exclude-module", module]
    command.append(str(ENTRY_POINT))

    print(f"Building {name} for {sys.platform}...", flush=True)
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller failed with exit code {result.returncode}")

    produced = output_path(args.onedir, cli=args.cli, console=args.console, dist=dist)
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

    # APPIMAGE_EXTRACT_AND_RUN lets an AppImage run where FUSE is not available,
    # as in CI and Docker; it means nothing to any other build.
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "APPIMAGE_EXTRACT_AND_RUN": "1"}
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


# -- the Linux AppImage ---------------------------------------------------------

# appimagetool packs the AppDir; the runtime is the small launcher at the front of
# every AppImage. Both are pinned and checked, as appimagetool would otherwise
# fetch its runtime from a moving "continuous" release.
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/appimagetool/releases/download/1.9.1/"
    "appimagetool-x86_64.AppImage"
)
APPIMAGETOOL_SHA256 = "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
APPIMAGE_RUNTIME_URL = (
    "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
)
APPIMAGE_RUNTIME_SHA256 = "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"
# The .desktop file's and icon's name, and the app's desktop file name (see
# __main__), so a desktop can tie the running window to its entry.
DESKTOP_ID = "comiccleaner"
APPIMAGE_ROOT = ROOT / "build" / "appimage"


def project_version() -> str:
    text = (ROOT / "src" / "comiccleaner" / "__init__.py").read_text("utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    if match is None:
        raise SystemExit("No __version__ in src/comiccleaner/__init__.py")
    return match.group(1)


def app_run() -> str:
    """The AppDir's entry point: run the one-folder build inside it."""
    return (
        "#!/bin/sh\n"
        "# Comic Cleaner's AppImage entry point: the one-folder build in usr/lib.\n"
        'here="$(dirname "$(readlink -f "$0")")"\n'
        f'exec "$here/usr/lib/{APP_NAME}/{APP_NAME}" "$@"\n'
    )


def desktop_entry(version: str) -> str:
    """The .desktop file, at the AppDir's top and in usr/share/applications."""
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Comic Cleaner",
        "GenericName=Comic page cleaner",
        "Comment=Find and remove duplicate pages across comic archives",
        f"Exec={APP_NAME} %F",
        f"Icon={DESKTOP_ID}",
        "Terminal=false",
        "Categories=Graphics;Utility;",
        "Keywords=comic;cbz;cbr;duplicate;advert;",
        f"StartupWMClass={APP_NAME}",
        f"X-AppImage-Version={version}",
        "",
    ])


def appimage_output() -> Path:
    return ROOT / "dist" / f"{APP_NAME}-x86_64.AppImage"


def _fetch(url: str, sha256: str, dest: Path) -> Path:
    """Download `url` to `dest` once, and refuse it unless its digest matches."""
    import hashlib
    import urllib.request

    if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() != sha256:
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {url}...", flush=True)
        with urllib.request.urlopen(url, timeout=120) as response:
            dest.write_bytes(response.read())
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != sha256:
        dest.unlink()
        raise SystemExit(f"{dest.name}: SHA-256 {digest}, expected {sha256}")
    return dest


def _icon_png(size: int, dest: Path) -> None:
    from PIL import Image  # the build environment has the app's own dependencies

    with Image.open(ROOT / "src" / "comiccleaner" / "assets" / "icon.png") as image:
        image.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS).save(dest)


def build_appimage(args: argparse.Namespace) -> Path:
    """The one-folder build in an AppDir, packed into dist/ComicCleaner-x86_64.AppImage."""
    if IS_WINDOWS or IS_MACOS:
        raise SystemExit("An AppImage can only be built on Linux")
    shutil.rmtree(APPIMAGE_ROOT, ignore_errors=True)
    executable = build(
        argparse.Namespace(**{**vars(args), "onedir": True, "cli": False, "console": False}),
        dist=APPIMAGE_ROOT / "dist",
    )
    folder = executable.parent  # a one-folder build's executable sits inside it

    appdir = APPIMAGE_ROOT / "AppDir"
    shutil.copytree(folder, appdir / "usr" / "lib" / APP_NAME, symlinks=True)
    run = appdir / "AppRun"
    run.write_text(app_run(), "utf-8")
    run.chmod(0o755)
    version = project_version()
    entry = desktop_entry(version)
    (appdir / f"{DESKTOP_ID}.desktop").write_text(entry, "utf-8")
    applications = appdir / "usr" / "share" / "applications"
    applications.mkdir(parents=True)
    (applications / f"{DESKTOP_ID}.desktop").write_text(entry, "utf-8")
    icons = appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
    icons.mkdir(parents=True)
    _icon_png(256, icons / f"{DESKTOP_ID}.png")
    shutil.copy2(icons / f"{DESKTOP_ID}.png", appdir / f"{DESKTOP_ID}.png")
    (appdir / ".DirIcon").symlink_to(f"{DESKTOP_ID}.png")

    tools = ROOT / "build" / "tools"
    tool = _fetch(APPIMAGETOOL_URL, APPIMAGETOOL_SHA256, tools / "appimagetool-1.9.1")
    tool.chmod(0o755)
    runtime = _fetch(APPIMAGE_RUNTIME_URL, APPIMAGE_RUNTIME_SHA256, tools / "runtime-20251108")
    produced = appimage_output()
    produced.parent.mkdir(parents=True, exist_ok=True)
    produced.unlink(missing_ok=True)
    # Unpacked on the fly, as FUSE may not be there (CI, Docker).
    environment = {**os.environ, "ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"}
    result = subprocess.run(
        [str(tool), "--no-appstream", "--runtime-file", str(runtime), str(appdir),
         str(produced)],
        cwd=ROOT, env=environment,
    )
    if result.returncode != 0 or not produced.exists():
        raise SystemExit(f"appimagetool failed with exit code {result.returncode}")
    produced.chmod(0o755)
    print(f"\nBuilt {produced.relative_to(ROOT)} ({produced.stat().st_size / 2**20:.1f} MB)")
    return produced


CHANGELOG = ROOT / "CHANGELOG.md"


def release_notes(version: str, changelog: str) -> str:
    """The CHANGELOG.md section for `version`, without its heading.

    CI publishes it as the release's notes. A section that is missing, empty
    or still marked unreleased is refused, so a tag cannot publish a release
    with no notes, or with notes nobody finished.
    """
    lines = changelog.splitlines()
    heading = f"## {version}"
    start = next(
        (i for i, line in enumerate(lines)
         if line == heading or line.startswith(heading + " ")),
        None,
    )
    if start is None:
        raise ValueError(f"CHANGELOG.md has no '{heading}' section")
    if "unreleased" in lines[start].lower():
        raise ValueError(
            f"CHANGELOG.md still marks {version} as unreleased: put the release "
            f"date in its heading, as '{heading} (YYYY-MM-DD)'"
        )
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    body = _unwrap(lines[start + 1:end]).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md's {version} section is empty")
    return body + "\n"


_BLOCK_START = re.compile(r"(#{1,6} |[-*+] |\d+[.)] |>|```|\|)")


def _unwrap(lines: list[str]) -> str:
    """Join the lines CHANGELOG.md wraps at 80 columns back into whole paragraphs.

    GitHub shows a release's notes as it shows a comment, where every line
    break is a break, so the wrapped text would break mid-sentence there.
    Headings, list items, quotes, tables and blank lines still start a new
    line, and code blocks are left alone.
    """
    out: list[str] = []
    fenced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        joinable = (
            not fenced and stripped and out and out[-1].strip()
            and not _BLOCK_START.match(stripped)
            and not out[-1].lstrip().startswith(("#", "```", "|"))
        )
        if joinable:
            out[-1] = f"{out[-1].rstrip()} {stripped}"
        else:
            out.append(line)
    return "\n".join(out)


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
    parser.add_argument(
        "--appimage",
        action="store_true",
        help="Linux only: build dist/ComicCleaner-x86_64.AppImage from a one-folder "
        "build, alongside any one-file build.",
    )
    parser.add_argument(
        "--release-notes",
        metavar="VERSION",
        help="Print CHANGELOG.md's section for VERSION, for the release, and build "
        "nothing. Fails if it is missing or still marked unreleased.",
    )
    args = parser.parse_args()

    if args.release_notes:
        try:
            sys.stdout.write(release_notes(args.release_notes, CHANGELOG.read_text("utf-8")))
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.appimage:
        if args.smoke_only:
            produced = appimage_output()
            if not produced.exists():
                raise SystemExit(f"Nothing to test: {produced} does not exist")
            return smoke_test(produced)
        produced = build_appimage(args)
        return smoke_test(produced) if args.smoke_test else 0

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
    print("Note: .cbr/.cb7 support needs the official 7-Zip on the target machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
