"""Entry point for the GUI."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="comiccleaner",
        description="Find and remove duplicate pages (ads, injected images) "
        "across comic archives.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Archives or folders to import on startup.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Scan PATHS headlessly and report what was found, then exit. "
        "Use this to check a packaged build without a display.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.self_test:
        return run_self_test(args.paths)

    # Imported late so --help works without a display or PySide6 present.
    # Absolute, not relative: PyInstaller runs this file as a top-level script,
    # where there is no parent package for a relative import to resolve against.
    from PySide6.QtWidgets import QApplication

    from comiccleaner import APP_NAME, ORGANISATION
    from comiccleaner.gui.main_window import MainWindow
    from comiccleaner.resources import app_icon

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANISATION)
    # Set before any window exists so the taskbar entry picks it up.
    app.setWindowIcon(app_icon())

    window = MainWindow()
    window.show()
    if args.paths:
        window.import_paths(args.paths)
    return app.exec()


def run_self_test(paths: list[Path]) -> int:
    """Exercise the whole non-GUI pipeline and report, without opening a window.

    A packaged build can start cleanly and still be subtly broken - most often a
    missing Pillow codec, which would silently make every scan find zero pages.
    This proves decoding, hashing and grouping actually work in the binary.

    A windowed PyInstaller build has no stdout, so the report falls back to a
    file; otherwise this would be unusable on exactly the build people ship.
    """
    from comiccleaner.core.extern import describe_backends
    from comiccleaner.core.grouping import GroupingOptions, build_groups
    from comiccleaner.core.scanner import find_archives, scan_archives

    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)
        if sys.stdout is not None:
            with contextlib.suppress(OSError, ValueError):
                print(text, flush=True)

    def finish(code: int) -> int:
        if sys.stdout is None:
            report = Path.cwd() / "comiccleaner-selftest.txt"
            with contextlib.suppress(OSError):
                report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return code

    from comiccleaner import APP_NAME
    from comiccleaner.resources import icon_path

    emit(f"{APP_NAME} self-test (frozen={getattr(sys, 'frozen', False)})")

    for name, found in describe_backends().items():
        emit(f"  archive tool {name:8} {found or 'not found'}")

    # --add-data paths are easy to get wrong, and a missing icon is silent.
    found_icon = icon_path()
    emit(f"  app icon        {found_icon or 'NOT BUNDLED'}")

    # Prove the image codecs we depend on survived packaging.
    try:
        from PIL import Image, features

        codecs = [c for c in ("jpg", "zlib", "webp") if features.check(c)]
        emit(f"  Pillow {Image.__version__} codecs: {', '.join(codecs) or 'NONE'}")
        if "jpg" not in codecs:
            emit("  FAIL: no JPEG support - nearly every comic page would fail")
            return finish(1)
    except Exception as exc:
        emit(f"  FAIL: Pillow unusable: {exc}")
        return finish(1)

    if not paths:
        emit("No paths given, so nothing was scanned. Pass a folder to scan.")
        return finish(0)

    archives = find_archives(paths)
    emit("")
    emit(f"Found {len(archives)} archive(s)")
    if not archives:
        return finish(0)

    scanned = scan_archives(archives)
    pages = sum(a.page_count for a in scanned)
    failed = [a for a in scanned if a.error]
    emit(f"Scanned {len(scanned)} archive(s), {pages} page(s), {len(failed)} unreadable")
    for archive in failed[:5]:
        emit(f"  unreadable: {archive.path.name}: {archive.error}")

    if pages == 0:
        emit("FAIL: no pages were decoded - the image codecs are not working")
        return finish(1)

    for threshold in (0, 4):
        groups = build_groups(scanned, GroupingOptions(threshold=threshold))
        summary = ", ".join(
            f"{g.page_count} copies/{g.archive_count} books/{g.kind.value}"
            for g in groups[:4]
        )
        emit(f"threshold {threshold}: {len(groups)} group(s) {summary}")

    emit("")
    emit("Self-test OK")
    return finish(0)


if __name__ == "__main__":
    raise SystemExit(main())
