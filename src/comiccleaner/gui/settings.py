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
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, ORGANISATION
from ..core.extern import describe_backends
from ..core.grouping import EDGE_PAGES, MAX_EDGE_PAGES, MAX_THRESHOLD, GroupingOptions
from ..core.remover import BackupPolicy
from .theme import Theme
from .thumbs import THUMB_SIZE, THUMB_SIZES

ORG = ORGANISATION
APP = APP_NAME

# Ready-made matching setups. Each only fills in the ordinary matching fields;
# "custom" is whatever the fields say when they match none of these.
CUSTOM_PRESET = "custom"
PRESETS: dict[str, tuple[str, dict[str, int | bool]]] = {
    "strict": ("Strict identical", {
        "threshold": 0, "min_pages": 2, "min_archives": 2,
        "skip_first_page": True, "skip_last_page": False, "include_flat": False,
    }),
    "ads": ("Scanlation ads", {
        "threshold": 6, "min_pages": 2, "min_archives": 2,
        "skip_first_page": True, "skip_last_page": False, "include_flat": False,
    }),
    "one_book": ("Within one book", {
        "threshold": 0, "min_pages": 2, "min_archives": 1,
        "skip_first_page": True, "skip_last_page": False, "include_flat": False,
    }),
}


@dataclass
class AppSettings:
    threshold: int = 0
    min_pages: int = 2
    min_archives: int = 2
    include_flat: bool = False
    skip_first_page: bool = True
    skip_last_page: bool = False
    edge_pages: int = EDGE_PAGES
    preset: str = CUSTOM_PRESET
    backup_enabled: bool = True
    backup_dir: str = ""
    delete_backups_after: bool = False
    output_dir: str = ""
    quarantine_dir: str = ""
    compress: bool = False
    theme: str = "system"
    thumb_size: int = THUMB_SIZE
    restore_session: bool = True
    remember_junk: bool = True
    check_updates: bool = False  # opt-in: it is the app's only network request
    # One glob per line; files matching one are never imported.
    exclude_globs: str = ""
    # One folder per line; books inside are reviewed but never rewritten.
    protected_folders: str = ""
    # View state that should survive a restart.
    library_grouped: bool = False
    edges_only: bool = False

    def grouping(self) -> GroupingOptions:
        return GroupingOptions(
            threshold=self.threshold,
            min_pages=self.min_pages,
            min_archives=self.min_archives,
            include_flat=self.include_flat,
            skip_first_page=self.skip_first_page,
            skip_last_page=self.skip_last_page,
            edge_pages=self.edge_pages,
        )

    def backup_policy(self) -> BackupPolicy:
        return BackupPolicy(
            enabled=self.backup_enabled,
            directory=Path(self.backup_dir) if self.backup_dir else None,
        )

    def output_path(self) -> Path | None:
        return Path(self.output_dir) if self.output_dir else None

    def quarantine_path(self) -> Path | None:
        return Path(self.quarantine_dir) if self.quarantine_dir else None

    def exclude_patterns(self) -> list[str]:
        return _lines(self.exclude_globs)

    def protected_paths(self) -> list[str]:
        return _lines(self.protected_folders)

    def theme_mode(self) -> Theme:
        return Theme.parse(self.theme)

    def matching_preset(self) -> str:
        """The preset whose values the matching fields hold, else custom."""
        return matching_preset(
            {name: getattr(self, name) for name in PRESETS["strict"][1]}
        )

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls) -> AppSettings:
        store = QSettings(ORG, APP)
        defaults = cls()

        def text(name: str) -> str:
            return str(store.value(name, getattr(defaults, name)) or "")

        def flag(name: str) -> bool:
            return _as_bool(store.value(name, getattr(defaults, name)))

        sizes = {px for _, px in THUMB_SIZES}
        thumb = _as_int(store.value("thumb_size", defaults.thumb_size), THUMB_SIZE, 1, 10_000)
        preset = text("preset")
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
            include_flat=flag("include_flat"),
            skip_first_page=flag("skip_first_page"),
            skip_last_page=flag("skip_last_page"),
            edge_pages=_as_int(
                store.value("edge_pages", defaults.edge_pages),
                defaults.edge_pages, 1, MAX_EDGE_PAGES,
            ),
            preset=preset if preset in PRESETS else CUSTOM_PRESET,
            backup_enabled=flag("backup_enabled"),
            backup_dir=text("backup_dir"),
            delete_backups_after=flag("delete_backups_after"),
            output_dir=text("output_dir"),
            quarantine_dir=text("quarantine_dir"),
            compress=flag("compress"),
            theme=text("theme"),
            thumb_size=thumb if thumb in sizes else THUMB_SIZE,
            restore_session=flag("restore_session"),
            remember_junk=flag("remember_junk"),
            check_updates=flag("check_updates"),
            exclude_globs=text("exclude_globs"),
            protected_folders=text("protected_folders"),
            library_grouped=flag("library_grouped"),
            edges_only=flag("edges_only"),
        )

    def save(self) -> None:
        store = QSettings(ORG, APP)
        for field_name, value in self.__dict__.items():
            store.setValue(field_name, value)
        store.sync()


def matching_preset(values: dict[str, int | bool]) -> str:
    """The preset these matching values amount to, or custom."""
    for key, (_label, preset) in PRESETS.items():
        if all(values.get(name) == value for name, value in preset.items()):
            return key
    return CUSTOM_PRESET


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


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


def _page(*boxes: QWidget) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    for box in boxes:
        layout.addWidget(box)
    layout.addStretch(1)
    return page


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(600)
        self._settings = settings
        # Set while a preset fills the fields, so that does not count as an edit.
        self._filling = False

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(_page(self._general_box(settings)), "General")
        self.tabs.addTab(_page(self._matching_box(settings)), "Matching")
        self.tabs.addTab(_page(self._safety_box(settings)), "Removing")
        self.tabs.addTab(_page(self._library_box(settings), self._backends_box()), "Library")
        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _general_box(self, settings: AppSettings) -> QGroupBox:
        box = QGroupBox("General")
        form = QFormLayout(box)

        self.theme = QComboBox()
        for mode in Theme:
            self.theme.addItem(mode.label, mode.value)
        current = self.theme.findData(settings.theme_mode().value)
        self.theme.setCurrentIndex(max(0, current))
        # Applies straight away so the choice can be judged before committing.
        self.theme.currentIndexChanged.connect(self._preview_theme)
        form.addRow("Theme:", self.theme)

        self.thumb_size = QComboBox()
        for label, pixels in THUMB_SIZES:
            self.thumb_size.addItem(f"{label} ({pixels} px)", pixels)
        self.thumb_size.setCurrentIndex(max(0, self.thumb_size.findData(settings.thumb_size)))
        form.addRow("Thumbnails:", self.thumb_size)

        self.restore_session = QCheckBox("Reopen the last library and its review on startup")
        self.restore_session.setChecked(settings.restore_session)
        self.restore_session.setToolTip(
            "Unchanged books come back from the hash cache, so this costs almost nothing."
        )
        form.addRow(self.restore_session)

        self.check_updates = QCheckBox("Check for a new version when the app starts")
        self.check_updates.setChecked(settings.check_updates)
        self.check_updates.setToolTip(
            "Asks GitHub for the latest release number, and nothing else. No details of "
            "your library or computer are sent."
        )
        form.addRow(self.check_updates)
        return box

    def _preview_theme(self, index: int) -> None:
        from .theme import apply_theme

        apply_theme(Theme.parse(self.theme.itemData(index)))

    def _matching_box(self, settings: AppSettings) -> QGroupBox:
        box = QGroupBox("Matching")
        form = QFormLayout(box)

        self.preset = QComboBox()
        self.preset.addItem("Custom", CUSTOM_PRESET)
        for key, (label, _values) in PRESETS.items():
            self.preset.addItem(label, key)
        self.preset.setToolTip(
            "Fills in the fields below. Changing any of them afterwards makes it Custom."
        )
        form.addRow("Preset:", self.preset)

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

        self.edge_pages = QSpinBox()
        self.edge_pages.setRange(1, MAX_EDGE_PAGES)
        self.edge_pages.setValue(settings.edge_pages)
        self.edge_pages.setSuffix(" page(s)")
        self.edge_pages.setToolTip(
            "How close to the start or end of a book counts as where adverts and credits "
            "sit. Used to rank groups, to warn about mid-book matches, and by "
            "\"Only near the edges\"."
        )
        form.addRow("Near the edge:", self.edge_pages)

        self.preset.setCurrentIndex(max(0, self.preset.findData(settings.matching_preset())))
        self.preset.currentIndexChanged.connect(self._apply_preset)
        for signal in (
            self.threshold.valueChanged, self.min_pages.valueChanged,
            self.min_archives.valueChanged, self.skip_first.toggled,
            self.skip_last.toggled, self.include_flat.toggled,
        ):
            signal.connect(self._matching_edited)
        return box

    def _apply_preset(self, index: int) -> None:
        key = self.preset.itemData(index)
        if key not in PRESETS:
            return
        values = PRESETS[key][1]
        self._filling = True
        try:
            self.threshold.setValue(int(values["threshold"]))
            self.min_pages.setValue(int(values["min_pages"]))
            self.min_archives.setValue(int(values["min_archives"]))
            self.skip_first.setChecked(bool(values["skip_first_page"]))
            self.skip_last.setChecked(bool(values["skip_last_page"]))
            self.include_flat.setChecked(bool(values["include_flat"]))
        finally:
            self._filling = False

    def _matching_edited(self, *_args: object) -> None:
        if self._filling:
            return
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(self.preset.findData(CUSTOM_PRESET))
        self.preset.blockSignals(False)

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

        self.quarantine_dir = _FolderPicker("Do not keep copies")
        self.quarantine_dir.set_value(settings.quarantine_dir)
        self.quarantine_dir.setToolTip(
            "Each removed page is copied here, in a folder named after its book, before "
            "the book is replaced. A book whose pages cannot be copied is left untouched."
        )
        form.addRow("Copy removed pages to:", self.quarantine_dir)

        self.remember_junk = QCheckBox(
            "Remember removed pages, and mark them for removal in new books"
        )
        self.remember_junk.setChecked(settings.remember_junk)
        self.remember_junk.setToolTip(
            "Once a library is clean an advert no longer repeats, so a new book's "
            "copy would go unnoticed. Remembered pages are found even in one book."
        )
        form.addRow(self.remember_junk)

        self.compress = QCheckBox("Deflate images when rebuilding (slower, rarely smaller)")
        self.compress.setChecked(settings.compress)
        form.addRow(self.compress)
        return box

    def _library_box(self, settings: AppSettings) -> QGroupBox:
        box = QGroupBox("Importing and protecting")
        form = QFormLayout(box)

        self.exclude_globs = QPlainTextEdit(settings.exclude_globs)
        self.exclude_globs.setPlaceholderText("One pattern per line, e.g. *sample*  or  Scans/*")
        self.exclude_globs.setToolTip(
            "Files whose name, or path inside the folder being added, matches a pattern "
            "are not imported."
        )
        self.exclude_globs.setMaximumHeight(90)
        form.addRow("Skip files matching:", self.exclude_globs)

        self.protected_folders = QPlainTextEdit(settings.protected_folders)
        self.protected_folders.setPlaceholderText("One folder per line")
        self.protected_folders.setToolTip(
            "Books in these folders are imported and reviewed as usual, but never "
            "rewritten. Apply lists them as left out."
        )
        self.protected_folders.setMaximumHeight(90)
        form.addRow("Never change books in:", self.protected_folders)
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
            edge_pages=self.edge_pages.value(),
            preset=str(self.preset.currentData()),
            backup_enabled=self.backup_enabled.isChecked(),
            backup_dir=self.backup_dir.value(),
            delete_backups_after=self.delete_backups_after.isChecked(),
            output_dir=self.output_dir.value(),
            quarantine_dir=self.quarantine_dir.value(),
            compress=self.compress.isChecked(),
            theme=str(self.theme.currentData()),
            thumb_size=int(self.thumb_size.currentData()),
            restore_session=self.restore_session.isChecked(),
            remember_junk=self.remember_junk.isChecked(),
            check_updates=self.check_updates.isChecked(),
            exclude_globs=self.exclude_globs.toPlainText(),
            protected_folders=self.protected_folders.toPlainText(),
            # View state is not edited here; it carries over as it was.
            library_grouped=self._settings.library_grouped,
            edges_only=self._settings.edges_only,
        )
