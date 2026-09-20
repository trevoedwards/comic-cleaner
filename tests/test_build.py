"""Guards on the cross-platform build logic.

PyInstaller cannot cross-compile, so the macOS and Linux binaries can only be
produced on those machines (CI does that). What *can* be checked anywhere is
that build.py computes the right arguments and paths for each platform, which
is where the portability bugs actually live.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_build_module():
    """Import build.py by path; it sits at the repo root, not in the package."""
    spec = importlib.util.spec_from_file_location("_build_script", ROOT / "build.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def build_module():
    return load_build_module()


def _retarget(module, monkeypatch, *, windows: bool, macos: bool) -> None:
    monkeypatch.setattr(module, "IS_WINDOWS", windows)
    monkeypatch.setattr(module, "IS_MACOS", macos)


# -- --add-data separator --------------------------------------------------


def test_add_data_uses_semicolon_on_windows(build_module, monkeypatch):
    _retarget(build_module, monkeypatch, windows=True, macos=False)

    flag, value = build_module.add_data_argument()

    assert flag == "--add-data"
    assert value.endswith(";comiccleaner/assets")


@pytest.mark.parametrize(
    ("windows", "macos"), [(False, True), (False, False)], ids=["macos", "linux"]
)
def test_add_data_uses_colon_off_windows(build_module, monkeypatch, windows, macos):
    """A semicolon here is the classic bug: it silently bundles nothing."""
    _retarget(build_module, monkeypatch, windows=windows, macos=macos)

    _, value = build_module.add_data_argument()

    assert value.endswith(":comiccleaner/assets")
    assert ";" not in value


# -- icons -----------------------------------------------------------------


def test_windows_build_uses_the_ico(build_module, monkeypatch):
    _retarget(build_module, monkeypatch, windows=True, macos=False)

    argument = build_module.icon_argument()

    assert argument[0] == "--icon"
    assert argument[1].endswith(".ico")


def test_macos_build_uses_the_icns(build_module, monkeypatch):
    _retarget(build_module, monkeypatch, windows=False, macos=True)

    argument = build_module.icon_argument()

    assert argument[0] == "--icon"
    assert argument[1].endswith(".icns")


def test_linux_build_passes_no_icon(build_module, monkeypatch):
    """Linux ELF binaries carry no icon; the window icon comes from the PNG."""
    _retarget(build_module, monkeypatch, windows=False, macos=False)

    assert build_module.icon_argument() == []


def test_both_icon_files_exist():
    assert (ROOT / "assets" / "comiccleaner.ico").is_file()
    assert (ROOT / "assets" / "comiccleaner.icns").is_file()


def test_icns_is_a_readable_image():
    from PIL import Image

    with Image.open(ROOT / "assets" / "comiccleaner.icns") as img:
        assert img.width == img.height


# -- output paths ----------------------------------------------------------


def test_windows_onefile_is_an_exe(build_module, monkeypatch):
    _retarget(build_module, monkeypatch, windows=True, macos=False)

    assert build_module.output_path(onedir=False).name == "ComicCleaner.exe"


def test_macos_onefile_is_an_app_bundle(build_module, monkeypatch):
    """--windowed on macOS always yields a .app, never a bare executable."""
    _retarget(build_module, monkeypatch, windows=False, macos=True)

    assert build_module.output_path(onedir=False).name == "ComicCleaner.app"


def test_linux_onefile_has_no_extension(build_module, monkeypatch):
    _retarget(build_module, monkeypatch, windows=False, macos=False)

    produced = build_module.output_path(onedir=False)

    assert produced.name == "ComicCleaner"
    assert produced.suffix == ""


def test_executable_inside_a_mac_bundle_is_found(build_module, tmp_path):
    bundle = tmp_path / "ComicCleaner.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "MacOS" / "ComicCleaner").write_text("#!/bin/sh\n")

    found = build_module._executable_within(bundle)

    assert found.parent.name == "MacOS"
    assert found.name == "ComicCleaner"


# -- source portability ----------------------------------------------------


def test_no_module_imports_winreg_at_top_level():
    """winreg only exists on Windows, so it must stay inside a guarded branch."""
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("import winreg", "from winreg")):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"top-level winreg import in: {offenders}"


def test_subprocess_flags_are_empty_off_windows():
    """creationflags does not exist on POSIX, so it must not be passed there."""
    from comiccleaner.core import extern

    if sys.platform == "win32":
        assert "creationflags" in extern._QUIET_LAUNCH
    else:
        assert extern._QUIET_LAUNCH == {}


def test_archive_tool_candidates_cover_unix():
    """The 7-Zip/unrar lookup must know where these live on macOS and Linux."""
    from comiccleaner.core import extern

    seven = " ".join(extern._SEVENZIP_CANDIDATES)
    unrar = " ".join(extern._UNRAR_CANDIDATES)

    assert "/usr/bin/" in seven and "/usr/bin/" in unrar
    assert "/opt/homebrew/" in seven  # Apple Silicon Homebrew prefix
