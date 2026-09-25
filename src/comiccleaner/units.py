"""Formatting shared by the GUI and the command line."""

from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_bytes(count: int) -> str:
    value = float(count)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")
