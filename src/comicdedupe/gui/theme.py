"""Light / dark / follow-system appearance."""

from __future__ import annotations

import enum
import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

log = logging.getLogger(__name__)

# Whether the UI is currently painted dark. Cached because Qt cannot always be
# asked - see apply_theme().
_is_dark = False


class Theme(enum.Enum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"

    @property
    def label(self) -> str:
        return {"system": "Follow system", "light": "Light", "dark": "Dark"}[self.value]

    @classmethod
    def parse(cls, value: object) -> Theme:
        try:
            return cls(str(value).lower())
        except ValueError:
            return cls.SYSTEM


def system_is_dark() -> bool:
    """The OS-level preference, as best we can determine it."""
    app = QGuiApplication.instance()
    if app is not None:
        hints = app.styleHints()
        if hasattr(hints, "colorScheme"):
            scheme = hints.colorScheme()
            if scheme is Qt.ColorScheme.Dark:
                return True
            if scheme is Qt.ColorScheme.Light:
                return False

    # Qt reports Unknown on some platforms and styles, so ask Windows directly.
    if sys.platform == "win32":
        try:
            import winreg

            subkey = (
                "Software" + chr(92) + "Microsoft" + chr(92) + "Windows"
                + chr(92) + "CurrentVersion" + chr(92) + "Themes"
                + chr(92) + "Personalize"
            )
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                uses_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return uses_light == 0
        except OSError:
            log.debug("could not read the Windows theme preference")

    return False


def apply_theme(theme: Theme) -> None:
    """Switch the running application to `theme`.

    QStyleHints.setColorScheme is asked first so native window decorations
    follow along where the platform supports it, but it is silently ignored on
    some styles and platforms, so the palette is always set explicitly too.
    That is what actually guarantees the widgets change.
    """
    global _is_dark

    app = QGuiApplication.instance()
    if app is None:
        return

    dark = system_is_dark() if theme is Theme.SYSTEM else theme is Theme.DARK

    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):
        hints.setColorScheme(
            {
                Theme.LIGHT: Qt.ColorScheme.Light,
                Theme.DARK: Qt.ColorScheme.Dark,
                Theme.SYSTEM: Qt.ColorScheme.Unknown,
            }[theme]
        )

    app.setPalette(_dark_palette() if dark else _light_palette())
    _is_dark = dark


def effective_is_dark() -> bool:
    """Whether the UI is currently rendering dark."""
    return _is_dark


def _build(entries: dict, disabled_text: QColor) -> QPalette:
    palette = QPalette()
    for role, colour_value in entries.items():
        palette.setColor(role, colour_value)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled_text)
    return palette


def _light_palette() -> QPalette:
    text = QColor(28, 28, 30)
    return _build(
        {
            QPalette.ColorRole.Window: QColor(240, 240, 240),
            QPalette.ColorRole.WindowText: text,
            QPalette.ColorRole.Base: QColor(255, 255, 255),
            QPalette.ColorRole.AlternateBase: QColor(246, 246, 248),
            QPalette.ColorRole.Text: text,
            QPalette.ColorRole.Button: QColor(240, 240, 240),
            QPalette.ColorRole.ButtonText: text,
            QPalette.ColorRole.ToolTipBase: QColor(255, 255, 240),
            QPalette.ColorRole.ToolTipText: text,
            QPalette.ColorRole.Mid: QColor(196, 196, 200),
            QPalette.ColorRole.Highlight: QColor(48, 110, 200),
            QPalette.ColorRole.HighlightedText: QColor(255, 255, 255),
            QPalette.ColorRole.Link: QColor(30, 100, 190),
        },
        QColor(150, 150, 154),
    )


def _dark_palette() -> QPalette:
    text = QColor(228, 228, 230)
    return _build(
        {
            QPalette.ColorRole.Window: QColor(37, 37, 40),
            QPalette.ColorRole.WindowText: text,
            QPalette.ColorRole.Base: QColor(28, 28, 31),
            QPalette.ColorRole.AlternateBase: QColor(44, 44, 48),
            QPalette.ColorRole.Text: text,
            QPalette.ColorRole.Button: QColor(53, 53, 57),
            QPalette.ColorRole.ButtonText: text,
            QPalette.ColorRole.ToolTipBase: QColor(53, 53, 57),
            QPalette.ColorRole.ToolTipText: text,
            QPalette.ColorRole.Mid: QColor(80, 80, 86),
            QPalette.ColorRole.Highlight: QColor(64, 132, 214),
            QPalette.ColorRole.HighlightedText: QColor(255, 255, 255),
            QPalette.ColorRole.Link: QColor(110, 170, 240),
        },
        QColor(128, 128, 134),
    )


# Semantic colours need different saturation per theme: the light-mode reds and
# greens turn muddy against a dark background.
_SEMANTIC = {
    "light": {
        "delete": "#c0392b",
        "keep": "#1e8449",
        "ignore": "#6b7275",
        "warning": "#9a6b00",
        "muted": "#5f6368",
    },
    "dark": {
        "delete": "#ff7b6b",
        "keep": "#5fd08a",
        "ignore": "#9aa0a6",
        "warning": "#e3b341",
        "muted": "#9aa0a6",
    },
}


def colour(name: str) -> QColor:
    """A semantic UI colour appropriate to the current theme."""
    table = _SEMANTIC["dark" if _is_dark else "light"]
    return QColor(table.get(name, table["muted"]))
