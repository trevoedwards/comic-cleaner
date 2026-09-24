"""Entry point: the GUI, or the headless scan and clean commands."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    # "scan" and "clean" are the headless commands. They are checked before the
    # GUI's parser so that `comiccleaner FOLDER` still just opens the window, and
    # they never import Qt, so they work on a server with no display.
    from comiccleaner import cli

    if args_in and args_in[0] in cli.COMMANDS:
        from comiccleaner import crashlog

        crashlog.install()
        return cli.main(args_in)

    parser = argparse.ArgumentParser(
        prog="comiccleaner",
        description="Find and remove duplicate pages (ads, injected images) "
        "across comic archives.",
        epilog="Headless commands: 'comiccleaner scan PATH...' reports duplicates and "
        "'comiccleaner clean PATH...' removes them. Add --help to either for options.",
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
    args = parser.parse_args(args_in)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Installed before anything else can fail, so even a startup crash is saved.
    from comiccleaner import crashlog

    crashlog.install()

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

    # Now that there is a GUI, crashes can also be reported to the user.
    crashlog.install(notifier=_report_crash_to_user)

    window = MainWindow()
    window.show()
    window.open_startup(args.paths)
    return app.exec()


def _report_crash_to_user(path: Path | None, summary: str) -> None:
    """Tell the user a crash was recorded, and where it went.

    A windowed build has no console, so without this the app would simply
    vanish with no explanation of where to look.
    """
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return
        where = (
            f"A crash report was saved to:\n{path}"
            if path is not None
            else "The crash report could not be saved - the launch folder is not writable."
        )
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Comic Cleaner stopped unexpectedly")
        box.setText(f"{summary}\n\n{where}")
        box.setStandardButtons(QMessageBox.StandardButton.Close)
        box.exec()
    except Exception:
        pass


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

    from comiccleaner import crashlog

    emit(f"  crash reports   {crashlog.crash_dir(create=False)}")
    reports = crashlog.existing_reports()
    if reports:
        emit(f"  {len(reports)} existing report(s), newest {reports[0].name}")

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
