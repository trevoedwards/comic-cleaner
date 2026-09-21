"""Persisted preferences and the settings dialog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, ORGANISATION
from ..core.extern import describe_backends
from ..core.grouping import GroupingOptions
from ..core.remover import BackupPolicy
from .theme import Theme

ORG = ORGANISATION
APP = APP_NAME

# Above roughly a quarter of the 64 bits, "similar" stops meaning anything.
MAX_THRESHOLD = 16


@dataclass
class AppSettings:
    threshold: int = 0
    min_pages: int = 2
    min_archives: int = 2
    include_flat: bool = False
    skip_first_page: bool = True
    skip_last_page: bool = False
    backup_enabled: bool = True
    backup_dir: str = ""
    delete_backups_after: bool = False
    output_dir: str = ""
    compress: bool = False
    theme: str = "system"

    def grouping(self) -> GroupingOptions:
        return GroupingOptions(
            threshold=self.threshold,
            min_pages=self.min_pages,
            min_archives=self.min_archives,
            include_flat=self.include_flat,
            skip_first_page=self.skip_first_page,
            skip_last_page=self.skip_last_page,
        )

    def backup_policy(self) -> BackupPolicy:
        return BackupPolicy(
            enabled=self.backup_enabled,
            directory=Path(self.backup_dir) if self.backup_dir else None,
        )

    def output_path(self) -> Path | None:
        return Path(self.output_dir) if self.output_dir else None

    def theme_mode(self) -> Theme:
        return Theme.parse(self.theme)

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls) -> AppSettings:
        store = QSettings(ORG, APP)
        defaults = cls()
        return cls(
            threshold=_as_int(
                store.value("threshold", defaults.threshold),
                defaults.threshold, 0, MAX_THRESHOLD,
            ),
            min_pages=_as_int(
                store.value("min_pages", defaults.min_pages), defaults.min_pages, 2, 100
            ),
            min_archives=_as_int(
                store.value("min_archives", defaults.min_archives),
                defaults.min_archives, 1, 100,
            ),
            include_flat=_as_bool(store.value("include_flat", defaults.include_flat)),
            skip_first_page=_as_bool(
                store.value("skip_first_page", defaults.skip_first_page)
            ),
            skip_last_page=_as_bool(
                store.value("skip_last_page", defaults.skip_last_page)
            ),
            backup_enabled=_as_bool(
                store.value("backup_enabled", defaults.backup_enabled)
            ),
            backup_dir=str(store.value("backup_dir", defaults.backup_dir)),
            delete_backups_after=_as_bool(
                store.value("delete_backups_after", defaults.delete_backups_after)
            ),
            output_dir=str(store.value("output_dir", defaults.output_dir)),
            compress=_as_bool(store.value("compress", defaults.compress)),
            theme=str(store.value("theme", defaults.theme)),
        )

    def save(self) -> None:
        store = QSettings(ORG, APP)
        for field_name, value in self.__dict__.items():
            store.setValue(field_name, value)
        store.sync()


def _as_int(value: object, default: int, low: int, high: int) -> int:
    """A stored number, clamped to what the settings dialog allows.

    QSettings hands back whatever is on disk. A hand-edited or damaged value must
    fall back to the default rather than raise, because this runs while the main
    window is being built and a failure there means the app can never start.
    """
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes")


def data_dir() -> Path:
    """Per-user data folder for the app.

    Built from GenericDataLocation plus the org and app names rather than from
    AppDataLocation, because Qt only folds those names into AppDataLocation once
    QApplication has been configured - so anything touching it earlier would
    drop files loose in the user's roaming folder.
    """
    from PySide6.QtCore import QStandardPaths

    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.GenericDataLocation
    )
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / ORG / APP


def cache_path() -> Path:
    """Where the hash cache and ignore list live."""
    return data_dir() / "hashes.sqlite"


class _FolderPicker(QWidget):
    """A read-only line edit with Browse/Clear buttons."""

    def __init__(self, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setReadOnly(True)
        browse = QPushButton("Browse...")
        clear = QPushButton("Clear")
        browse.clicked.connect(self._browse)
        clear.clicked.connect(lambda: self.edit.setText(""))
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)
        layout.addWidget(clear)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose folder")
        if chosen:
            self.edit.setText(chosen)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, text: str) -> None:
        self.edit.setText(text)


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._settings = settings

        layout = QVBoxLayout(self)
        layout.addWidget(self._appearance_box(settings))
        layout.addWidget(self._matching_box(settings))
        layout.addWidget(self._safety_box(settings))
        layout.addWidget(self._backends_box())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _appearance_box(self, settings: AppSettings) -> QGroupBox:
        box = QGroupBox("Appearance")
        form = QFormLayout(box)

        self.theme = QComboBox()
        for mode in Theme:
            self.theme.addItem(mode.label, mode.value)
        current = self.theme.findData(settings.theme_mode().value)
        self.theme.setCurrentIndex(max(0, current))
        # Applies straight away so the choice can be judged before committing.
        self.theme.currentIndexChanged.connect(self._preview_theme)
        form.addRow("Theme:", self.theme)
        return box

    def _preview_theme(self, index: int) -> None:
        from .theme import apply_theme

        apply_theme(Theme.parse(self.theme.itemData(index)))

    def _matching_box(self, settings: AppSettings) -> QGroupBox:
        box = QGroupBox("Matching")
        form = QFormLayout(box)

        self.threshold = QSlider(Qt.Orientation.Horizontal)
        self.threshold.setRange(0, MAX_THRESHOLD)
        self.threshold.setValue(settings.threshold)
        self.threshold.setTickInterval(2)
        self.threshold.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.threshold_label = QLabel()
        self.threshold.valueChanged.connect(self._update_threshold_label)
        self._update_threshold_label(settings.threshold)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.threshold, 1)
        row_layout.addWidget(self.threshold_label)
        form.addRow("Similarity:", row)

        self.min_pages = QSpinBox()
        self.min_pages.setRange(2, 100)
        self.min_pages.setValue(settings.min_pages)
        form.addRow("Minimum copies:", self.min_pages)

        self.min_archives = QSpinBox()
        self.min_archives.setRange(1, 100)
        self.min_archives.setValue(settings.min_archives)
        self.min_archives.setToolTip(
            "Ads usually appear across several books. Raise this to cut noise."
        )
        form.addRow("Across at least (books):", self.min_archives)

        self.skip_first = QCheckBox("Never match the first page (protects covers)")
        self.skip_first.setChecked(settings.skip_first_page)
        form.addRow(self.skip_first)

        self.skip_last = QCheckBox("Never match the last page")
        self.skip_last.setChecked(settings.skip_last_page)
        form.addRow(self.skip_last)

        self.include_flat = QCheckBox("Include blank and solid-colour pages")
        self.include_flat.setChecked(settings.include_flat)
        self.include_flat.setToolTip(
            "Blank pages look alike to a perceptual hash, so they are hidden by default."
        )
        form.addRow(self.include_flat)
        return box

    def _safety_box(self, settings: AppSettings) -> QGroupBox:
        box = QGroupBox("When removing pages")
        form = QFormLayout(box)

        self.backup_enabled = QCheckBox("Back up the original before replacing it")
        self.backup_enabled.setChecked(settings.backup_enabled)
        form.addRow(self.backup_enabled)

        self.backup_dir = _FolderPicker("Alongside the original, as <name>.bak")
        self.backup_dir.set_value(settings.backup_dir)
        form.addRow("Backup folder:", self.backup_dir)

        self.delete_backups_after = QCheckBox(
            "Delete the backups once the whole run has succeeded"
        )
        self.delete_backups_after.setChecked(settings.delete_backups_after)
        self.delete_backups_after.setToolTip(
            "Backups still protect each rewrite while it happens; they are only "
            "removed at the end, after every archive has been verified."
        )
        form.addRow(self.delete_backups_after)

        self.output_dir = _FolderPicker("Replace the originals in place")
        self.output_dir.set_value(settings.output_dir)
        form.addRow("Write cleaned copies to:", self.output_dir)

        self.compress = QCheckBox("Deflate images when rebuilding (slower, rarely smaller)")
        self.compress.setChecked(settings.compress)
        form.addRow(self.compress)
        return box

    def _backends_box(self) -> QGroupBox:
        box = QGroupBox("Archive tools")
        layout = QVBoxLayout(box)
        backends = describe_backends()
        for name, path in backends.items():
            layout.addWidget(QLabel(f"{name}: {path or 'not found'}"))
        if not any(backends.values()):
            warning = QLabel(
                "No external tool found - .cbr and .cb7 files cannot be read. "
                "Install 7-Zip to enable them."
            )
            warning.setWordWrap(True)
            layout.addWidget(warning)
        return box

    def _update_threshold_label(self, value: int) -> None:
        if value == 0:
            text = "0 - identical images only"
        elif value <= 4:
            text = f"{value} - near-identical (re-encoded copies)"
        elif value <= 10:
            text = f"{value} - similar (rescaled, recoloured)"
        else:
            text = f"{value} - loose (expect false matches)"
        self.threshold_label.setText(text)

    def result_settings(self) -> AppSettings:
        return AppSettings(
            threshold=self.threshold.value(),
            min_pages=self.min_pages.value(),
            min_archives=self.min_archives.value(),
            include_flat=self.include_flat.isChecked(),
            skip_first_page=self.skip_first.isChecked(),
            skip_last_page=self.skip_last.isChecked(),
            backup_enabled=self.backup_enabled.isChecked(),
            backup_dir=self.backup_dir.value(),
            delete_backups_after=self.delete_backups_after.isChecked(),
            output_dir=self.output_dir.value(),
            compress=self.compress.isChecked(),
            theme=str(self.theme.currentData()),
        )
