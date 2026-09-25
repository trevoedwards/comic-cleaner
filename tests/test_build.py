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


def test_windows_cli_build_is_its_own_exe(build_module, monkeypatch):
    _retarget(build_module, monkeypatch, windows=True, macos=False)

    cli = build_module.output_path(onedir=False, cli=True)
    gui = build_module.output_path(onedir=False)

    assert cli.name == "comiccleaner-cli.exe"
    # Windows filenames ignore case, so the two must differ by more than that.
    assert cli.name.lower() != gui.name.lower()


def test_macos_cli_build_is_a_plain_binary(build_module, monkeypatch):
    """Only --windowed makes a .app; the console build is a bare executable."""
    _retarget(build_module, monkeypatch, windows=False, macos=True)

    assert build_module.output_path(onedir=False, cli=True).name == "comiccleaner-cli"


def test_macos_onedir_is_an_app_bundle_too(build_module, monkeypatch):
    """--windowed --onedir on macOS also yields dist/<name>.app, not a folder."""
    _retarget(build_module, monkeypatch, windows=False, macos=True)

    produced = build_module.output_path(onedir=True)

    assert produced == build_module.ROOT / "dist" / "ComicCleaner.app"


@pytest.mark.parametrize("onedir", [False, True], ids=["onefile", "onedir"])
def test_macos_console_build_is_a_plain_binary(build_module, monkeypatch, onedir):
    _retarget(build_module, monkeypatch, windows=False, macos=True)

    produced = build_module.output_path(onedir=onedir, console=True)

    assert produced.suffix == ""
    assert produced.name == "ComicCleaner"


@pytest.mark.parametrize(
    ("windows", "binary"), [(True, "ComicCleaner.exe"), (False, "ComicCleaner")],
    ids=["windows", "linux"],
)
def test_onedir_elsewhere_is_a_folder_with_the_binary(build_module, monkeypatch, windows, binary):
    _retarget(build_module, monkeypatch, windows=windows, macos=False)

    produced = build_module.output_path(onedir=True)

    assert produced == build_module.ROOT / "dist" / "ComicCleaner" / binary


class _Ran:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


@pytest.mark.parametrize("present", [True, False], ids=["present", "missing"])
def test_pyinstaller_is_installed_pinned_and_only_when_missing(
    build_module, monkeypatch, present
):
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _Ran(0 if present else 1)

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)

    build_module.ensure_pyinstaller("python")

    installs = [c for c in calls if "install" in c]
    if present:
        assert installs == []
    else:
        assert installs == [["python", "-m", "pip", "install", "--quiet", "pyinstaller>=6.3,<8"]]


def test_cli_smoke_test_png_is_a_real_image(build_module):
    """The console build is smoke-tested on this; a bad PNG would fail every build."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(build_module._tiny_png(24))) as img:
        img.load()
        assert img.size == (24, 24)
        assert img.mode == "RGB"


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


def test_7zip_override_accepts_the_new_and_the_pre_rename_variable(tmp_path, monkeypatch):
    from comiccleaner.core import extern

    tool = tmp_path / "7z-custom"
    tool.write_text("")
    for variable in ("COMICCLEANER_7Z", "COMICDEDUPE_7Z"):
        monkeypatch.delenv("COMICCLEANER_7Z", raising=False)
        monkeypatch.delenv("COMICDEDUPE_7Z", raising=False)
        monkeypatch.setenv(variable, str(tool))

        assert extern._first_existing(["definitely-not-installed"], []) == str(tool)


def test_new_7zip_variable_wins_over_the_old_one(tmp_path, monkeypatch):
    from comiccleaner.core import extern

    new, old = tmp_path / "new-7z", tmp_path / "old-7z"
    new.write_text("")
    old.write_text("")
    monkeypatch.setenv("COMICCLEANER_7Z", str(new))
    monkeypatch.setenv("COMICDEDUPE_7Z", str(old))

    assert extern._first_existing(["definitely-not-installed"], []) == str(new)
