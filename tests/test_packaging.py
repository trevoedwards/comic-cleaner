"""Guards for things that only break once the app is frozen into an .exe."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ENTRY_POINT = Path(__file__).resolve().parents[1] / "src" / "comiccleaner" / "__main__.py"


def test_entry_point_has_no_relative_imports() -> None:
    """PyInstaller runs __main__.py as a top-level script.

    In that context there is no parent package, so any `from .x import y` raises
    "attempted relative import with no known parent package" and the built exe
    dies on startup - while `python -m comiccleaner` keeps working, which makes
    the bug invisible until someone runs the binary.
    """
    tree = ast.parse(ENTRY_POINT.read_text(encoding="utf-8"))

    relative = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]

    assert not relative, (
        "Entry point uses relative imports, which break the frozen build: "
        + ", ".join(f"line {n.lineno}: from {'.' * n.level}{n.module or ''}" for n in relative)
    )


def test_entry_point_runs_as_a_script() -> None:
    """Executing the file directly must not fail during import resolution."""
    result = subprocess.run(
        [sys.executable, str(ENTRY_POINT), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "comiccleaner" in result.stdout


def test_module_invocation_still_works() -> None:
    """The venv/dev path (`python -m comiccleaner`) must keep working too."""
    result = subprocess.run(
        [sys.executable, "-m", "comiccleaner", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_app_identity_is_the_official_name() -> None:
    from comiccleaner import APP_NAME, AUTHOR, GITHUB_URL, ORGANISATION

    assert APP_NAME == "Comic Cleaner"
    assert AUTHOR == "Trevor Edwards"
    assert ORGANISATION == "Playback Software"
    assert GITHUB_URL.startswith("https://")


def test_cache_lands_in_a_named_app_folder() -> None:
    """Qt only folds the org/app names into AppDataLocation after QApplication
    is configured, so a naive lookup drops the cache loose in the user's data
    folder. The path must be app-scoped no matter when it is asked for."""
    import pytest

    pytest.importorskip("PySide6")
    from comiccleaner.gui.settings import APP, ORG, cache_path

    path = cache_path()

    assert path.name == "hashes.sqlite"
    assert path.parent.name == APP
    assert path.parent.parent.name == ORG
