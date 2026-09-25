"""The main application window: import, review, remove."""

from __future__ import annotations

import contextlib
import enum
import html
import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import (
    QAction,
    QBrush,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
    QPalette,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, GITHUB_URL, __version__
from ..core.archive import ARCHIVE_SUFFIXES
from ..core.cache import HashCache
from ..core.extern import (
    SEVENZIP_URL,
    can_extract,
    install_hint,
    refresh_backends,
    sevenzip_path,
)
from ..core.grouping import build_groups, is_safe, review_warnings, sort_groups, summarise
from ..core.model import (
    ArchiveInfo,
    ArchiveKind,
    Decision,
    DuplicateGroup,
    MatchKind,
    PageEntry,
)
from ..core.remover import (
    DEFAULT_MAX_FRACTION,
    RemovalPlan,
    RemovalReport,
    build_plans,
    is_backup_name,
    on_same_volume,
    recover_interrupted,
    split_by_fraction,
)
from ..core.scanner import find_archives
from ..core.updates import Release, is_newer
from ..resources import app_icon
from ..units import human_bytes
from .about import AboutDialog
from .history import HistoryDialog
from .preview import PagePreviewDialog, page_distance
from .remembered import RememberedDialog
from .session import (
    SESSION_FILE,
    SavedDecision,
    Session,
    carry_decisions,
    load_session,
    save_session,
)
from .settings import AppSettings, SettingsDialog, cache_path
from .theme import Theme, apply_theme, colour
from .thumbs import THUMB_SIZE, ThumbnailCache
from .welcome import WelcomePanel
from .workers import RemovalWorker, ScanWorker, UpdateChecker

log = logging.getLogger(__name__)

ROLE_GID = Qt.ItemDataRole.UserRole + 1
ROLE_PAGE_KEY = Qt.ItemDataRole.UserRole + 2
ROLE_THUMB_KEY = Qt.ItemDataRole.UserRole + 3

# How long closing waits for a scan or removal to reach a safe stopping point
# before giving up on this attempt; it closes by itself once they are done.
CLOSE_WAIT_MS = 4000

# The three columns share these so their list areas start and end together.
PANEL_HEADER_HEIGHT = 30
PANEL_FOOTER_HEIGHT = 34

SORT_MODES = [
    ("Books affected", "books"),
    ("Space recoverable", "space"),
    ("Number of copies", "count"),
    ("Image size", "size"),
]

GROUP_FILTERS = [
    ("All groups", "all"),
    ("Undecided", "undecided"),
    ("Marked for removal", "marked"),
    ("Known junk", "known"),
    ("Identical", "identical"),
    ("Similar", "similar"),
    ("With warnings", "warnings"),
]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 860)
        self.setAcceptDrops(True)

        self.settings = AppSettings.load()
        apply_theme(self.settings.theme_mode())
        self.setWindowIcon(app_icon())
        db_path = cache_path()
        self.cache = HashCache(db_path)
        self.thumbs = ThumbnailCache(self)
        self.thumbs.ready.connect(self._on_thumb_ready)

        self.archives: dict[Path, ArchiveInfo] = {}
        self.groups: list[DuplicateGroup] = []
        self._scan_worker: ScanWorker | None = None
        self._removal_worker: RemovalWorker | None = None
        self._removal_was_dry_run = False
        self._removal_total = 0
        # Books the last Apply left alone for losing too much; see apply_removals.
        self._skipped_plans: list[RemovalPlan] = []
        # Books a finished removal rewrote, scanned again once its thread exits.
        self._pending_rescan: list[Path] = []
        self._sort_key = "books"
        self._last_backups: list[Path] = []
        # The pages shown in the detail grid, in grid order, for the preview.
        self._shown_pages: list[PageEntry] = []
        # The groups in the list right now, in row order: self.groups narrowed by
        # the library selection and the group filter.
        self._shown_groups: list[DuplicateGroup] = []
        self._library_selection: set[Path] = set()
        self._group_filter = "all"

        # Kept beside the hash cache, so anything that relocates one moves both.
        self._session_file = db_path.with_name(SESSION_FILE)
        # Decisions from the last session still waiting for their books to be scanned.
        self._restored: dict[str, SavedDecision] = {}
        # Shown once the next scan ends (restoring the last session, or books put
        # back from History); the scan's own messages would bury it.
        self._startup_note = ""
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.setInterval(1500)
        self._session_timer.timeout.connect(self.save_session)
        # Set once the window starts closing. A worker's result can already be
        # queued by then, and must not land on a cache that has been shut.
        self._closing = False
        # Workers a deferred close is waiting on; see closeEvent.
        self._close_hooked: list[object] = []
        self._update_checker: UpdateChecker | None = None

        self._build_ui()
        self._restyle()
        self._refresh_status()

        # "Follow system" follows the OS while running, not just at launch.
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):  # Qt 6.5+
            hints.colorSchemeChanged.connect(self._on_system_colour_scheme_changed)

    # -- construction ------------------------------------------------------
    def _build_ui(self) -> None:
        self._build_toolbar()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_library_panel())
        splitter.addWidget(self._build_groups_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([280, 380, 740])
        self.review = splitter

        # An empty library gets a welcome page in place of three blank columns.
        self.welcome = WelcomePanel()
        self.welcome.add_folder.connect(self.add_folder)
        self.welcome.add_files.connect(self.add_files)
        self.views = QStackedWidget()
        self.views.addWidget(self.welcome)
        self.views.addWidget(self.review)

        central = QWidget()
        self._central = central
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_tools_banner())
        layout.addWidget(self._build_missing_banner())
        layout.addWidget(self.views, 1)
        self.setCentralWidget(central)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        # Outside the central widget, so it stays usable while a removal locks it.
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setToolTip("Stop after the book currently being processed.")
        self.btn_cancel.clicked.connect(self.cancel_work)
        self.btn_cancel.setVisible(False)
        self.status_label = QLabel("Drop comic archives here to begin.")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.btn_cancel)
        # Appears only when an update check finds something newer.
        self.update_link = QLabel()
        self.update_link.setOpenExternalLinks(True)
        self.update_link.setVisible(False)
        self.statusBar().addPermanentWidget(self.update_link)

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("Main")
        bar.setMovable(False)

        self.act_add_files = QAction("Add Files", self)
        self.act_add_files.setShortcut(QKeySequence.StandardKey.Open)
        self.act_add_files.triggered.connect(self.add_files)

        self.act_add_folder = QAction("Add Folder", self)
        self.act_add_folder.triggered.connect(self.add_folder)

        self.act_remove = QAction("Remove From List", self)
        self.act_remove.triggered.connect(self.remove_selected_archives)

        self.act_scan = QAction("Scan", self)
        self.act_scan.setShortcut("Ctrl+R")
        self.act_scan.triggered.connect(self.start_scan)

        self.act_apply = QAction("Apply Removals...", self)
        self.act_apply.triggered.connect(self.apply_removals)
        self.act_apply.setEnabled(False)

        self.act_history = QAction("History...", self)
        self.act_history.setToolTip("Past removals, and putting books back as they were.")
        self.act_history.triggered.connect(lambda: self.show_history())

        self.act_clean_backups = QAction("Clean Up Backups...", self)
        self.act_clean_backups.setToolTip(
            "Delete .bak files left next to your archives by previous runs."
        )
        self.act_clean_backups.triggered.connect(self.clean_up_backups)

        self.act_remembered = QAction("Remembered Pages...", self)
        self.act_remembered.setToolTip(
            "Known junk that is removed on sight, and pages you have ignored. "
            "Forget, restore, import or export them."
        )
        self.act_remembered.triggered.connect(lambda: self.manage_remembered())

        self.act_settings = QAction("Settings", self)
        self.act_settings.triggered.connect(self.open_settings)

        bar.addAction(self.act_add_files)
        bar.addAction(self.act_add_folder)
        bar.addAction(self.act_remove)
        bar.addSeparator()
        bar.addAction(self.act_scan)
        bar.addSeparator()
        bar.addAction(self.act_apply)
        bar.addAction(self.act_history)
        bar.addAction(self.act_clean_backups)
        bar.addSeparator()
        bar.addAction(self.act_remembered)
        bar.addAction(self.act_settings)
        bar.addWidget(self._help_button())

    def _help_button(self) -> QWidget:
        """Toolbar dropdown with About and the project link."""
        self.act_about = QAction("About", self)
        self.act_about.triggered.connect(self.show_about)

        self.act_github = QAction("GitHub", self)
        self.act_github.setToolTip(GITHUB_URL)
        self.act_github.triggered.connect(self.open_github)

        self.act_check_updates = QAction("Check for Updates...", self)
        self.act_check_updates.setToolTip("Ask GitHub whether a newer version is out.")
        self.act_check_updates.triggered.connect(lambda: self.check_for_updates(quiet=False))

        menu = QMenu(self)
        menu.addAction(self.act_about)
        menu.addAction(self.act_github)
        menu.addAction(self.act_check_updates)

        button = QToolButton()
        button.setText("Help")
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._help_menu_button = button
        return button

    @Slot()
    def show_about(self) -> None:
        AboutDialog(self).exec()

    @Slot()
    def open_github(self) -> None:
        if not QDesktopServices.openUrl(QUrl(GITHUB_URL)):
            QMessageBox.warning(
                self,
                "Could not open the browser",
                f"Open this address manually:{chr(10)}{chr(10)}{GITHUB_URL}",
            )

    def _panel(
        self, title: str, header_extra: QWidget | None, body: QWidget, footer: QWidget
    ) -> _Panel:
        """One column of the main view.

        Every column is built the same way - a fixed-height header, the list
        itself, then a fixed-height footer - so the three list areas line up
        across the window instead of each starting at its own height.
        """
        panel = _Panel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QWidget()
        header.setFixedHeight(PANEL_HEADER_HEIGHT)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(f"<b>{title}</b>")
        header_layout.addWidget(label)
        header_layout.addStretch(1)
        if header_extra is not None:
            header_layout.addWidget(header_extra)
        layout.addWidget(header)

        layout.addWidget(body, 1)

        footer.setFixedHeight(PANEL_FOOTER_HEIGHT)
        layout.addWidget(footer)
        panel.title_label = label
        return panel

    @staticmethod
    def _row(*widgets: QWidget, stretch_last: bool = False) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for index, widget in enumerate(widgets):
            last = index == len(widgets) - 1
            layout.addWidget(widget, 1 if (stretch_last and last) else 0)
        if not stretch_last:
            layout.addStretch(1)
        return row

    def _build_library_panel(self) -> QWidget:
        self.archive_list = QListWidget()
        self.archive_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.archive_list.setAlternatingRowColors(True)
        # Scoped to the list: Delete elsewhere must never drop books from the library.
        _widget_shortcut(
            QKeySequence(QKeySequence.StandardKey.Delete),
            self.archive_list,
            self.remove_selected_archives,
        )

        # Selecting books narrows the groups to the ones they contain.
        self.archive_list.itemSelectionChanged.connect(self._on_library_selection)

        self.library_search = QLineEdit()
        self.library_search.setPlaceholderText("Filter books")
        self.library_search.setClearButtonEnabled(True)
        self.library_search.setMaximumWidth(170)
        self.library_search.textChanged.connect(self._apply_library_search)

        # Elided rather than wrapped, so the footer height never changes.
        self.library_summary = QLabel("No archives imported.")
        self.library_summary.setWordWrap(False)

        return self._panel(
            "Library",
            self.library_search,
            self.archive_list,
            self._row(self.library_summary, stretch_last=True),
        )

    def _build_groups_panel(self) -> QWidget:
        self.sort_combo = QComboBox()
        for label, key in SORT_MODES:
            self.sort_combo.addItem(label, key)
        self.sort_combo.setToolTip("Sort the groups")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self.filter_combo = QComboBox()
        for label, key in GROUP_FILTERS:
            self.filter_combo.addItem(label, key)
        self.filter_combo.setToolTip("Show only some of the groups")
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)

        self.btn_show_all = QToolButton()
        self.btn_show_all.setText("Show all")
        self.btn_show_all.setToolTip("Clear the book selection and the group filter.")
        self.btn_show_all.clicked.connect(self.show_all_groups)
        self.btn_show_all.setVisible(False)

        self.group_list = QListWidget()
        self.group_list.setIconSize(QSize(72, 72))
        self.group_list.setAlternatingRowColors(True)
        self.group_list.currentItemChanged.connect(self._on_group_selected)

        self.btn_mark_safe = QPushButton("Mark safe")
        self.btn_mark_safe.setToolTip(
            "Mark for removal only what needs no second look: known junk, and pages "
            "identical across several books with no warnings (no blank pages, covers "
            "or mid-book matches)."
        )
        self.btn_mark_safe.clicked.connect(self.mark_safe_groups)
        self.btn_mark_all = QPushButton("Mark all")
        self.btn_mark_all.setToolTip("Mark every group shown for removal.")
        self.btn_mark_all.clicked.connect(lambda: self._set_all_decisions(Decision.DELETE))
        self.btn_clear_all = QPushButton("Clear all")
        self.btn_clear_all.setToolTip("Clear the decision on every group shown.")
        self.btn_clear_all.clicked.connect(
            lambda: self._set_all_decisions(Decision.UNDECIDED)
        )

        panel = self._panel(
            "Duplicate groups",
            self._row(self.btn_show_all, self.filter_combo, self.sort_combo),
            self.group_list,
            self._row(self.btn_mark_safe, self.btn_mark_all, self.btn_clear_all),
        )
        self.groups_title = panel.title_label
        return panel

    def _build_detail_panel(self) -> QWidget:
        # Warnings live in the header so the body keeps a constant top edge.
        self.detail_warning = QLabel()
        self.detail_warning.setVisible(False)
        self.detail_warning.setToolTip("")

        self.page_list = QListWidget()
        self.page_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.page_list.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.page_list.setGridSize(QSize(THUMB_SIZE + 40, THUMB_SIZE + 78))
        self.page_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.page_list.setMovement(QListWidget.Movement.Static)
        self.page_list.setWordWrap(True)
        self.page_list.setSpacing(6)
        self.page_list.itemChanged.connect(self._on_page_checked)
        # Double-click or Enter opens the page at full size.
        self.page_list.itemActivated.connect(self._preview_item)

        self.btn_delete = QPushButton("Remove this page everywhere")
        self.btn_delete.setToolTip("Remove every ticked copy, then go to the next group (D)")
        self.btn_delete.clicked.connect(lambda: self._set_current_decision(Decision.DELETE))
        self.btn_keep = QPushButton("Keep")
        self.btn_keep.setToolTip("Keep every copy, then go to the next group (K)")
        self.btn_keep.clicked.connect(lambda: self._set_current_decision(Decision.KEEP))
        self.btn_ignore = QPushButton("Ignore")
        self.btn_ignore.setToolTip(
            "Hide this page from future scans (I). Undo it from Remembered Pages."
        )
        self.btn_ignore.clicked.connect(self._ignore_current)
        for button in (self.btn_delete, self.btn_keep, self.btn_ignore):
            button.setEnabled(False)

        # Single keys, so a long review can be driven from the keyboard. Each one
        # moves on to the next group still waiting for a decision. Like Delete on
        # the library they are scoped: to the review lists and these buttons. A
        # window-wide letter could also fire while typing in Filter books or in a
        # drop-down, and mark a group nobody meant to touch.
        self.decision_keys = {"D": self.btn_delete, "K": self.btn_keep, "I": self.btn_ignore}
        for key, button in self.decision_keys.items():
            for widget in (self.group_list, self.page_list, *self.decision_keys.values()):
                _widget_shortcut(QKeySequence(key), widget, button.click)
        # Space ticks and Enter opens the selected copy from anywhere in the review,
        # scoped the same way so Filter books and the drop-downs keep both keys. Not
        # on the page grid: it already ticks on Space and opens on Enter, and a
        # second binding there would toggle the copy twice, changing nothing.
        for widget in (self.group_list, *self.decision_keys.values()):
            _widget_shortcut(QKeySequence("Space"), widget, self._toggle_selected_copy)
            for key in ("Return", "Enter"):
                _widget_shortcut(QKeySequence(key), widget, self._open_selected_copy)

        self.detail_hint = QLabel(
            "Ticked copies are removed. Double-click a page to see it full size."
        )

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        for button in (self.btn_delete, self.btn_keep, self.btn_ignore):
            footer_layout.addWidget(button)
        footer_layout.addSpacing(12)
        footer_layout.addWidget(self.detail_hint, 1)

        panel = self._panel(
            "Select a group to review it", self.detail_warning, self.page_list, footer
        )
        # Kept so the header text can be rewritten as groups are selected.
        self.detail_header = panel.title_label
        return panel

    # -- archive tools -----------------------------------------------------
    def _build_tools_banner(self) -> QFrame:
        """Says so up front when imported books cannot be read on this machine."""
        banner = QFrame()
        banner.setObjectName("toolsBanner")
        row = QHBoxLayout(banner)
        row.setContentsMargins(10, 6, 10, 6)
        self.tools_banner_text = QLabel()
        self.tools_banner_text.setWordWrap(True)
        self.tools_banner_text.setOpenExternalLinks(True)
        self.tools_banner_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        check = QPushButton("Check Again")
        check.setToolTip("Look for 7-Zip, UnRAR and bsdtar again, without restarting.")
        check.clicked.connect(self.recheck_archive_tools)
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self._dismiss_tools_banner)
        row.addWidget(self.tools_banner_text, 1)
        row.addWidget(check)
        row.addWidget(dismiss)
        banner.setVisible(False)
        self.tools_banner = banner
        # How many unreadable books there were when the banner was dismissed; it
        # comes back only if more are imported.
        self._banner_dismissed_at = 0
        return banner

    def _build_missing_banner(self) -> QFrame:
        """Names the books from last session that are gone, until dismissed."""
        banner = QFrame()
        banner.setObjectName("missingBanner")
        row = QHBoxLayout(banner)
        row.setContentsMargins(10, 6, 10, 6)
        self.missing_banner_text = QLabel()
        self.missing_banner_text.setWordWrap(True)
        self.missing_banner_text.setTextFormat(Qt.TextFormat.RichText)
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self._dismiss_missing_banner)
        row.addWidget(self.missing_banner_text, 1)
        row.addWidget(dismiss)
        banner.setVisible(False)
        self.missing_banner = banner
        return banner

    def _show_missing_banner(self, missing: list[Path], limit: int = 10) -> None:
        names = ", ".join(html.escape(p.name) for p in missing[:limit])
        if len(missing) > limit:
            names += f", and {len(missing) - limit} more"
        self.missing_banner_text.setText(
            f"<b>{len(missing)} book(s) from last time could not be found:</b> {names}. "
            "They may have been moved, renamed or deleted; add them again from where "
            "they are now."
        )
        self.missing_banner.setToolTip("\n".join(str(p) for p in missing))
        self.missing_banner.setVisible(True)

    @Slot()
    def _dismiss_missing_banner(self) -> None:
        self.missing_banner.setVisible(False)

    def _books_needing_a_tool(self) -> list[ArchiveInfo]:
        return [
            a for a in self.archives.values()
            if a.kind in _NEEDS_TOOL and not can_extract(a.kind.value)
        ]

    def _update_tools_banner(self) -> None:
        blocked = self._books_needing_a_tool()
        if not blocked:
            self._banner_dismissed_at = 0
            self.tools_banner.setVisible(False)
            return
        if len(blocked) <= self._banner_dismissed_at:
            return
        self.tools_banner_text.setText(
            f"<b>{len(blocked)} book(s) cannot be read yet:</b> .cbr and .cb7 files need "
            f"an archive tool that is not installed. {install_hint()}, then press "
            f'Check Again. <a href="{SEVENZIP_URL}">Get 7-Zip</a>'
        )
        self.tools_banner.setVisible(True)

    @Slot()
    def _dismiss_tools_banner(self) -> None:
        self._banner_dismissed_at = len(self._books_needing_a_tool())
        self.tools_banner.setVisible(False)

    @Slot()
    def recheck_archive_tools(self) -> None:
        """Pick up a tool installed since launch, and let failed books be retried."""
        refresh_backends()
        self.welcome.refresh()
        retry = 0
        for path, info in list(self.archives.items()):
            if info.error and info.kind in _NEEDS_TOOL and can_extract(info.kind.value):
                self.archives[path] = ArchiveInfo(
                    path=path, kind=info.kind, size=0, mtime_ns=0
                )
                retry += 1
        self._refresh_archive_list()
        if self._books_needing_a_tool():
            self.status_label.setText(
                f"Still no 7-Zip, UnRAR or bsdtar found. {install_hint()}."
            )
        elif retry:
            self.status_label.setText(
                f"Archive tool found. Press Scan to read the {retry} book(s) that failed."
            )
        else:
            self.status_label.setText("Archive tool found. .cbr and .cb7 books can be read.")

    # -- drag and drop -----------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.welcome.set_drag_active(True)

    def dragLeaveEvent(self, event: object) -> None:
        self.welcome.set_drag_active(False)

    def dropEvent(self, event: QDropEvent) -> None:
        self.welcome.set_drag_active(False)
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.import_paths(paths)
            event.acceptProposedAction()

    # -- importing ---------------------------------------------------------
    @Slot()
    def add_files(self) -> None:
        patterns = " ".join(f"*{s}" for s in sorted(ARCHIVE_SUFFIXES))
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add comic archives", "", f"Comic archives ({patterns})"
        )
        if files:
            self.import_paths([Path(f) for f in files])

    @Slot()
    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of comics")
        if folder:
            self.import_paths([Path(folder)])

    def import_paths(self, paths: list[Path]) -> None:
        discovered = find_archives(paths)
        added = 0
        for path in discovered:
            if path not in self.archives:
                self.archives[path] = ArchiveInfo(
                    path=path, kind=_kind_of(path), size=0, mtime_ns=0
                )
                added += 1
        self._refresh_archive_list()
        self._session_changed()
        if not discovered:
            self.status_label.setText("Nothing importable in that drop.")
        elif not added:
            self.status_label.setText(
                f"Already in the library: {len(discovered)} archive(s). "
                "Nothing new to import."
            )
        else:
            self.status_label.setText(
                f"Imported {added} new archive(s). Press Scan to hash their pages."
            )
        self._refresh_status()

    @Slot()
    def remove_selected_archives(self) -> None:
        for item in self.archive_list.selectedItems():
            path = Path(item.data(Qt.ItemDataRole.UserRole))
            self.archives.pop(path, None)
        self._refresh_archive_list()
        self.rebuild_groups()
        self._session_changed()

    # -- scanning ----------------------------------------------------------
    @Slot()
    def start_scan(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.cancel()
            return
        if self._job is _Job.REMOVING:
            return  # archives are being rewritten; hashing them now would race it
        if not self.archives:
            QMessageBox.information(self, "Nothing to scan", "Import some archives first.")
            return
        self._run_scan(list(self.archives))

    def _run_scan(self, paths: list[Path]) -> None:
        worker = ScanWorker(paths, self.cache, self)
        worker.progressed.connect(self._on_scan_progress)
        worker.finished_scan.connect(self._on_scan_done)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_scan_thread_finished)
        self._scan_worker = worker
        self._job_changed("Scanning")
        worker.start()

    @Slot(int, int, str)
    def _on_scan_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.status_label.setText(f"Scanning {done}/{total}: {name}")

    @Slot(list)
    def _on_scan_done(self, results: list) -> None:
        if self._closing:
            return
        for info in results:
            self.archives[info.path] = info
        self._refresh_archive_list()
        self.rebuild_groups()
        self._session_changed()

        unreadable = [a for a in self.archives.values() if a.error]
        if unreadable:
            hint = ""
            # Windows' own tar and UnRAR miss some formats that 7-Zip reads.
            if sevenzip_path() is None and any(a.kind in _NEEDS_TOOL for a in unreadable):
                hint = (
                    "\n\n7-Zip reads more .cbr and .cb7 files than the other tools. "
                    f"{install_hint()}."
                )
            names = "\n".join(f"  {a.path.name}: {a.error}" for a in unreadable[:8])
            more = f"\n  ...and {len(unreadable) - 8} more" if len(unreadable) > 8 else ""
            QMessageBox.warning(
                self,
                "Some archives could not be read",
                f"{len(unreadable)} archive(s) were skipped:\n\n{names}{more}{hint}",
            )

    @Slot()
    def _on_scan_thread_finished(self) -> None:
        self._scan_worker = None
        self._job_changed()
        if self._startup_note:
            self.status_label.setText(f"{self._startup_note} {self.status_label.text()}")
            self._startup_note = ""

    @Slot(str)
    def _on_worker_failed(self, message: str) -> None:
        if self._closing:
            return  # the crash report is already on disk; no dialogs while quitting
        QMessageBox.critical(self, "Something went wrong", message)

    # -- grouping ----------------------------------------------------------
    def rebuild_groups(self) -> None:
        """Re-cluster from already-hashed pages. No disk access, so it is instant."""
        scanned = [a for a in self.archives.values() if a.pages]
        # Decisions already made, last session's first so this session's win.
        prior = dict(self._restored)
        for group in self.groups:
            if group.decision is not Decision.UNDECIDED or group.kept:
                prior[group.gid] = _saved(group)

        self.groups = build_groups(
            scanned,
            self.settings.grouping(),
            ignored=self.cache.ignored_hashes(),
            known=self.cache.known_hashes() if self.settings.remember_junk else None,
        )
        # Carry them over, following the pages when a group's id has changed.
        carried, used = carry_decisions(prior, self.groups)
        for group in self.groups:
            source = carried.get(group.gid)
            if source is not None:
                group.decision = source.decision
                group.kept = {k for k in source.kept if k in {p.key for p in group.pages}}
            elif group.known:
                # Removed before, so marked again; Apply still asks first.
                group.decision = Decision.DELETE
        # A saved decision that matched nothing is dropped, unless some of its
        # books are yet to be scanned and it may still find its group.
        self._restored = {
            gid: saved for gid, saved in self._restored.items()
            if gid not in used and self._awaiting_scan(saved)
        }

        self.groups = sort_groups(self.groups, self._sort_key)
        self._refresh_group_list()
        self._refresh_status()
        self._update_library_texts()

    def _awaiting_scan(self, saved: SavedDecision) -> bool:
        for archive in {Path(a) for a, _ in saved.pages}:
            info = self.archives.get(archive)
            if info is not None and not info.pages and not info.error:
                return True
        return False

    @Slot(int)
    def _on_sort_changed(self, index: int) -> None:
        self._sort_key = self.sort_combo.itemData(index)
        self.groups = sort_groups(self.groups, self._sort_key)
        self._refresh_group_list()

    # -- view refresh ------------------------------------------------------
    def _book_stats(self) -> dict[Path, tuple[int, int]]:
        """Per book: pages in any group, and pages marked for removal."""
        stats: dict[Path, list[int]] = {}
        for group in self.groups:
            removing = {p.key for p in group.pages_to_remove()}
            for page in group.pages:
                counts = stats.setdefault(page.archive, [0, 0])
                counts[0] += 1
                counts[1] += page.key in removing
        return {path: (c[0], c[1]) for path, c in stats.items()}

    @staticmethod
    def _archive_text(info: ArchiveInfo, stats: tuple[int, int] | None) -> str:
        name = info.path.name
        if info.error:
            return f"{name}  —  unreadable"
        if not info.pages:
            return f"{name}  —  not scanned"
        text = f"{name}  —  {info.page_count} pages"
        if stats:
            # One count, so the row fits the column; the tooltip has both.
            repeated, marked = stats
            text += f", {marked} to remove" if marked else f", {repeated} repeated"
        return text

    @staticmethod
    def _archive_tooltip(info: ArchiveInfo, stats: tuple[int, int] | None) -> str:
        lines = [str(info.path)]
        if info.error:
            lines.append(info.error)
        elif stats:
            lines.append(f"{stats[0]} page(s) repeated elsewhere, {stats[1]} marked for removal")
        return "\n".join(lines)

    def _refresh_archive_list(self) -> None:
        # Rebuilt from scratch, so the selection (which filters the groups) is
        # put back afterwards rather than silently dropped.
        selected = set(self._library_selection)
        stats = self._book_stats()
        self.archive_list.blockSignals(True)
        self.archive_list.clear()
        for path in sorted(self.archives, key=lambda p: str(p).lower()):
            info = self.archives[path]
            item = QListWidgetItem(self._archive_text(info, stats.get(path)))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(self._archive_tooltip(info, stats.get(path)))
            if info.error:
                item.setForeground(QBrush(colour("delete")))
            self.archive_list.addItem(item)
            item.setSelected(path in selected)
        self.archive_list.blockSignals(False)
        self._apply_library_search()

        # The search may already have dropped hidden books from the selection.
        still_there = {p for p in self._library_selection if p in self.archives}
        if still_there != self._library_selection:
            self._library_selection = still_there
            self._refresh_group_list()
        self._update_library_summary()
        self.views.setCurrentWidget(self.review if self.archives else self.welcome)
        self._update_tools_banner()

    def _refresh_group_list(self) -> None:
        # Stay on the group being reviewed. If it is gone (just ignored, say), land
        # on whatever slid into its place rather than snapping back to the top of a
        # list the user may have been working down for a while.
        previous = self.group_list.currentItem()
        previous_gid = previous.data(ROLE_GID) if previous is not None else None
        previous_row = self.group_list.currentRow()

        self._shown_groups = [g for g in self.groups if self._group_visible(g)]
        self._update_groups_title()

        self.group_list.blockSignals(True)
        self.group_list.clear()
        for group in self._shown_groups:
            item = QListWidgetItem(self._group_text(group))
            item.setData(ROLE_GID, group.gid)
            representative = group.representative
            item.setData(ROLE_THUMB_KEY, self.thumbs.key_for(representative))
            item.setIcon(self.thumbs.icon(representative))
            item.setForeground(QBrush(_decision_colour(group.decision)))
            self.group_list.addItem(item)
        self.group_list.blockSignals(False)
        if not self._shown_groups:
            self._clear_detail()
            return
        row = next(
            (i for i, g in enumerate(self._shown_groups) if g.gid == previous_gid), None
        )
        if row is None:
            row = min(max(previous_row, 0), len(self._shown_groups) - 1)
        self.group_list.setCurrentRow(row)

    # -- filtering ---------------------------------------------------------
    def _group_visible(self, group: DuplicateGroup) -> bool:
        if self._library_selection and not any(
            p.archive in self._library_selection for p in group.pages
        ):
            return False
        mode = self._group_filter
        if mode == "undecided":
            return group.decision is Decision.UNDECIDED
        if mode == "marked":
            return group.decision is Decision.DELETE
        if mode == "known":
            return group.known
        if mode == "identical":
            return group.kind is MatchKind.EXACT
        if mode == "similar":
            return group.kind is MatchKind.SIMILAR
        if mode == "warnings":
            return bool(review_warnings(group))
        return True

    def _filtered(self) -> bool:
        return bool(self._library_selection) or self._group_filter != "all"

    def _update_groups_title(self) -> None:
        if self._filtered():
            self.groups_title.setText(
                f"<b>Groups: {len(self._shown_groups)} of {len(self.groups)}</b>"
            )
        else:
            self.groups_title.setText("<b>Duplicate groups</b>")
        self.btn_show_all.setVisible(self._filtered())

    def _update_library_summary(self) -> None:
        if self._library_selection:
            self.library_summary.setText(
                f"{len(self._library_selection)} selected: showing their groups only."
            )
            return
        scanned = sum(1 for a in self.archives.values() if a.pages)
        pages = sum(a.page_count for a in self.archives.values())
        self.library_summary.setText(
            f"{len(self.archives)} archive(s), {scanned} scanned, {pages} pages."
        )

    def _update_library_texts(self) -> None:
        """Refresh each book's repeat counts in place, keeping the selection."""
        stats = self._book_stats()
        for row in range(self.archive_list.count()):
            item = self.archive_list.item(row)
            info = self.archives.get(Path(item.data(Qt.ItemDataRole.UserRole)))
            if info is not None:
                item.setText(self._archive_text(info, stats.get(info.path)))
                item.setToolTip(self._archive_tooltip(info, stats.get(info.path)))

    @Slot()
    def _on_library_selection(self) -> None:
        self._library_selection = {
            Path(item.data(Qt.ItemDataRole.UserRole))
            for item in self.archive_list.selectedItems()
        }
        self._update_library_summary()
        self._refresh_group_list()

    @Slot()
    def _apply_library_search(self) -> None:
        needle = self.library_search.text().strip().lower()
        deselected = False
        self.archive_list.blockSignals(True)
        for row in range(self.archive_list.count()):
            item = self.archive_list.item(row)
            hidden = bool(needle) and needle not in item.text().lower()
            item.setHidden(hidden)
            # A book you cannot see must not go on narrowing the groups.
            if hidden and item.isSelected():
                item.setSelected(False)
                deselected = True
        self.archive_list.blockSignals(False)
        if deselected:
            self._on_library_selection()

    @Slot(int)
    def _on_filter_changed(self, index: int) -> None:
        self._group_filter = self.filter_combo.itemData(index)
        self._refresh_group_list()

    @Slot()
    def show_all_groups(self) -> None:
        self.archive_list.clearSelection()  # clears the library filter via the signal
        self.filter_combo.setCurrentIndex(0)
        self._refresh_group_list()

    def _group_text(self, group: DuplicateGroup) -> str:
        marker = {
            Decision.DELETE: "[remove] ",
            Decision.KEEP: "[keep] ",
            Decision.IGNORE: "[ignored] ",
            Decision.UNDECIDED: "",
        }[group.decision]
        kind = "identical" if group.kind is MatchKind.EXACT else "similar"
        if group.known:
            kind += " • known junk"
        rep = group.representative
        return (
            f"{marker}{group.page_count} copies in {group.archive_count} book(s)\n"
            f"{human_bytes(group.recoverable_bytes)} • {kind} • "
            f"{rep.width}x{rep.height}"
        )

    def _current_group(self) -> DuplicateGroup | None:
        item = self.group_list.currentItem()
        if item is None:
            return None
        gid = item.data(ROLE_GID)
        return next((g for g in self.groups if g.gid == gid), None)

    @Slot()
    def _on_group_selected(self) -> None:
        group = self._current_group()
        if group is None:
            self._clear_detail()
            return

        for button in (self.btn_delete, self.btn_keep, self.btn_ignore):
            button.setEnabled(True)

        kind = "Byte-identical" if group.kind is MatchKind.EXACT else "Visually similar"
        self.detail_header.setText(
            f"<b>{group.page_count} copies across {group.archive_count} book(s)</b> — "
            f"{kind}, {human_bytes(group.recoverable_bytes)} recoverable"
            + (". Known junk: removed from your library before." if group.known else "")
        )

        warnings = review_warnings(group)
        self.detail_warning.setText("  ".join(warnings))
        self.detail_warning.setVisible(bool(warnings))

        self._populate_pages(group)

    def _populate_pages(self, group: DuplicateGroup) -> None:
        similar = group.kind is MatchKind.SIMILAR
        pages = list(group.pages)
        if similar:
            # Single-linkage can chain in a page that only resembles a neighbour,
            # so the copies furthest from the reference are shown first.
            pages.sort(key=lambda p: page_distance(p, group), reverse=True)
        self._shown_pages = pages

        self.page_list.blockSignals(True)
        self.page_list.clear()
        for page in pages:
            info = self.archives.get(page.archive)
            total = f" of {info.page_count}" if info is not None and info.page_count else ""
            text = (
                f"{page.archive.name}\n"
                f"page {page.index + 1}{total} • {human_bytes(page.size)}"
            )
            if similar:
                text += f"\n{page_distance(page, group)} bit(s) from reference"
            item = QListWidgetItem(text)
            item.setData(ROLE_PAGE_KEY, list(page.key))
            item.setData(ROLE_THUMB_KEY, self.thumbs.key_for(page))
            item.setIcon(self.thumbs.icon(page))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = page.key not in group.kept
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            item.setToolTip(f"{page.archive}\n{page.name}\n{page.width}x{page.height}")
            self.page_list.addItem(item)
        self.page_list.blockSignals(False)

    def _clear_detail(self) -> None:
        self.detail_header.setText("<b>Select a group to review it</b>")
        self.detail_warning.setVisible(False)
        self.page_list.clear()
        self._shown_pages = []
        for button in (self.btn_delete, self.btn_keep, self.btn_ignore):
            button.setEnabled(False)

    @Slot(str, object)
    def _on_thumb_ready(self, key: str, pixmap: QPixmap) -> None:
        from PySide6.QtGui import QIcon

        icon = QIcon(pixmap)
        for widget in (self.group_list, self.page_list):
            # A new icon is an itemChanged too, and on the page grid that would
            # read the grid's tick back into the group: a copy just unticked in
            # the preview, behind a grid not yet refreshed, came back ticked.
            widget.blockSignals(True)
            try:
                for row in range(widget.count()):
                    item = widget.item(row)
                    if item.data(ROLE_THUMB_KEY) == key:
                        item.setIcon(icon)
            finally:
                widget.blockSignals(False)

    # -- decisions ---------------------------------------------------------
    def _set_current_decision(self, decision: Decision) -> None:
        group = self._current_group()
        if group is None:
            return
        group.decision = decision
        self._refresh_status()
        self._session_changed()
        self._update_library_texts()
        row = self.group_list.currentRow()
        if self._group_visible(group):
            self._update_group_item(group)
            self._select_next_undecided(row + 1)
        else:
            # Filtered out by its new decision ("Undecided" view, say): drop it,
            # and whatever slides into its row is where the search starts.
            self._refresh_group_list()
            self._select_next_undecided(self.group_list.currentRow())

    def _set_all_decisions(self, decision: Decision) -> None:
        """Apply `decision` to every group shown; filtered-out groups are left be."""
        for group in self._shown_groups:
            group.decision = decision
        self._refresh_group_list()
        self._refresh_status()
        self._session_changed()
        self._update_library_texts()

    @Slot()
    def mark_safe_groups(self) -> None:
        """Mark what needs no second look; leave the rest for review."""
        safe = [
            g for g in self._shown_groups
            if g.decision is Decision.UNDECIDED and is_safe(g, self.settings.min_archives)
        ]
        for group in safe:
            group.decision = Decision.DELETE
        left = sum(1 for g in self._shown_groups if g.decision is Decision.UNDECIDED)
        self._refresh_group_list()
        self._refresh_status()
        self._session_changed()
        self._update_library_texts()
        self.status_label.setText(
            f"Marked {len(safe)} safe group(s) for removal. "
            + (f"{left} still need a look." if left else "Nothing is left undecided.")
        )

    def _ignore_current(self) -> None:
        group = self._current_group()
        if group is None:
            return
        rep = group.representative
        self.cache.ignore(
            group.gid,
            {p.dhash for p in group.pages},
            note=f"{group.page_count} copies in {group.archive_count} book(s)",
            sample=(rep.archive, rep.name),
        )
        self.groups = [g for g in self.groups if g.gid != group.gid]
        self._refresh_group_list()
        self._refresh_status()
        self._session_changed()
        # Whatever slid into this row may itself be undecided, so start from it.
        if self.groups:
            self._select_next_undecided(self.group_list.currentRow())
        self.status_label.setText(
            "Ignored. Bring it back any time from Remembered Pages on the toolbar."
        )

    def _select_next_undecided(self, start: int) -> None:
        """Move to the first undecided group at or after `start`, wrapping round."""
        count = len(self._shown_groups)
        for row in [*range(start, count), *range(0, min(start, count))]:
            if self._shown_groups[row].decision is Decision.UNDECIDED:
                self.group_list.setCurrentRow(row)
                return
        if count:
            self.status_label.setText(
                "Every group has a decision. Apply Removals when you are ready."
            )

    def _selected_copy(self) -> QListWidgetItem | None:
        """The copy Space and Enter act on: the grid's current one, else the first."""
        item = self.page_list.currentItem()
        if item is None and self.page_list.count():
            item = self.page_list.item(0)
            self.page_list.setCurrentItem(item)
        return item

    @Slot()
    def _toggle_selected_copy(self) -> None:
        item = self._selected_copy()
        if item is None:
            return
        ticked = item.checkState() is Qt.CheckState.Checked
        # Through itemChanged, so the group and the counts follow as for a click.
        item.setCheckState(Qt.CheckState.Unchecked if ticked else Qt.CheckState.Checked)

    @Slot()
    def _open_selected_copy(self) -> None:
        item = self._selected_copy()
        if item is not None:
            self._preview_item(item)

    @Slot(QListWidgetItem)
    def _preview_item(self, item: QListWidgetItem) -> None:
        group = self._current_group()
        if group is None or not self._shown_pages:
            return
        dialog = PagePreviewDialog(
            group,
            self._shown_pages,
            self.page_list.row(item),
            self.thumbs,
            {path: info.page_count for path, info in self.archives.items()},
            self,
        )
        dialog.exec()
        # The dialog can untick copies, so bring the grid and counts back in line.
        row = self.page_list.row(item)
        self._populate_pages(group)
        self.page_list.setCurrentRow(row)
        self._update_group_item(group)
        self._refresh_status()
        self._session_changed()

    def manage_remembered(self, *, tab: str = "known") -> None:
        if self._job is not _Job.IDLE:
            return  # its thumbnails open archives, which must stay closed meanwhile
        dialog = RememberedDialog(self.cache, self.thumbs, self, tab=tab)
        dialog.exec()
        if dialog.changed:
            self.rebuild_groups()

    def _update_group_item(self, group: DuplicateGroup) -> None:
        for row in range(self.group_list.count()):
            item = self.group_list.item(row)
            if item.data(ROLE_GID) == group.gid:
                item.setText(self._group_text(group))
                item.setForeground(QBrush(_decision_colour(group.decision)))
                return

    @Slot(QListWidgetItem)
    def _on_page_checked(self, item: QListWidgetItem) -> None:
        group = self._current_group()
        if group is None:
            return
        raw = item.data(ROLE_PAGE_KEY)
        if not raw:
            return
        key = (raw[0], raw[1])
        if item.checkState() is Qt.CheckState.Checked:
            group.kept.discard(key)
        else:
            group.kept.add(key)
        self._update_group_item(group)
        self._refresh_status()
        self._session_changed()

    # -- removal -----------------------------------------------------------
    @Slot()
    def apply_removals(self) -> None:
        if self._job is not _Job.IDLE:
            return
        marked = [g for g in self.groups if g.decision is Decision.DELETE]
        if not marked:
            QMessageBox.information(
                self, "Nothing marked", "Mark at least one group for removal first."
            )
            return

        counts = {a.path: a.page_count for a in self.archives.values()}
        plans = build_plans(marked, counts)
        if not plans:
            QMessageBox.information(
                self, "Nothing to do", "Every copy in those groups is set to be kept."
            )
            return

        # The command line's rule: a book losing more than a quarter of its pages
        # is two copies of one issue matching each other, not adverts. Emptying a
        # book is the extreme case. Those are listed and left alone.
        plans, skipped = split_by_fraction(plans, DEFAULT_MAX_FRACTION)
        if not plans:
            QMessageBox.information(
                self,
                "Nothing can be removed",
                f"Every affected book would lose more than {DEFAULT_MAX_FRACTION:.0%} of "
                "its pages, which usually means two copies of the same issue are matching "
                "each other. Nothing was changed:\n\n" + _skipped_lines(skipped),
            )
            return

        dialog = _ConfirmDialog(plans, skipped, self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Thumbnails keep archives open, and Windows will not let a rewritten
        # file be renamed while a handle is alive.
        self.thumbs.release_archives()

        worker = RemovalWorker(
            plans,
            backup=self.settings.backup_policy(),
            output_dir=self.settings.output_path(),
            dry_run=dialog.dry_run(),
            compress=self.settings.compress,
            learn=marked if self.settings.remember_junk else None,
            cache=self.cache,
            parent=self,
        )
        worker.progressed.connect(self._on_removal_progress)
        worker.finished_removal.connect(self._on_removal_done)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_removal_thread_finished)
        self._removal_worker = worker
        self._removal_was_dry_run = dialog.dry_run()
        self._removal_total = len(plans)
        self._skipped_plans = skipped
        self._job_changed("Removing")
        worker.start()

    @Slot()
    def cancel_work(self) -> None:
        """Stop the running scan or removal once the current book is done.

        A removal is never interrupted mid-book: each archive is swapped in whole
        or not at all, so stopping between them is always safe.
        """
        worker = self._removal_worker or self._scan_worker
        if worker is None:
            return
        worker.cancel()
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText("Stopping...")
        self.status_label.setText("Stopping after the current book...")

    def _skipped_note(self, outcome: str) -> list[str]:
        if not self._skipped_plans:
            return []
        return [
            "",
            f"{len(self._skipped_plans)} book(s) {outcome}: each would lose more than "
            f"{DEFAULT_MAX_FRACTION:.0%} of its pages.",
        ]

    def _cancel_note(self, report: RemovalReport, outcome: str) -> list[str]:
        if not report.cancelled:
            return []
        untouched = self._removal_total - len(report.results)
        return ["", f"Cancelled: {untouched} archive(s) {outcome}."]

    @Slot(int, int, str)
    def _on_removal_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.status_label.setText(f"Rewriting {done}/{total}: {name}")

    @Slot(object)
    def _on_removal_done(self, report: RemovalReport) -> None:
        if self._closing:
            return
        if report.blocked:
            self._show_shortfall(report)
            return
        succeeded = report.succeeded
        failed = report.failed
        converted = [r for r in succeeded if r.converted]

        if self._removal_was_dry_run:
            self._show_dry_run_result(report)
            return

        lines = [
            f"Removed {report.total_removed} page(s) from {len(succeeded)} archive(s).",
            f"Freed {human_bytes(report.total_freed)}.",
        ]
        learned = self._removal_worker.learned if self._removal_worker is not None else 0
        if learned:
            lines.append(
                f"{learned} page(s) remembered as known junk: they will be marked for "
                "removal wherever they turn up again."
            )
        if converted:
            lines.append(
                f"{len(converted)} .cbr/.cb7 file(s) were rebuilt as .cbz "
                "(those formats cannot be written in place)."
            )
        if failed:
            lines.append("")
            lines.append(f"{len(failed)} archive(s) failed and were left untouched:")
            lines += [f"  {r.archive.name}: {r.error}" for r in failed[:8]]
        lines += self._skipped_note("were skipped and are unchanged")
        lines += self._cancel_note(report, "were not processed and are unchanged")

        self._last_backups = [
            r.backup for r in succeeded if r.backup is not None and r.backup.exists()
        ]

        # Only sweep the backups when the entire run came through clean - a
        # partial failure is exactly when the originals are still worth having,
        # and a cancelled run is not a finished one.
        auto_clean = bool(
            self.settings.delete_backups_after
            and not failed
            and not report.cancelled
            and self._last_backups
        )
        if auto_clean:
            removed, freed = self._delete_backups(self._last_backups)
            lines.append("")
            lines.append(
                f"Deleted {removed} backup file(s), reclaiming {human_bytes(freed)}."
            )

        box = QMessageBox(self)
        box.setIcon(
            QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information
        )
        box.setWindowTitle("Removal finished")

        clean_button = None
        if self._last_backups and not auto_clean:
            total = sum(b.stat().st_size for b in self._last_backups if b.exists())
            lines.append("")
            lines.append(
                f"{len(self._last_backups)} backup file(s) are still on disk, "
                f"using {human_bytes(total)}."
            )
            clean_button = box.addButton(
                f"Delete {len(self._last_backups)} backup(s)",
                QMessageBox.ButtonRole.DestructiveRole,
            )
        box.addButton(QMessageBox.StandardButton.Ok)
        box.setText("\n".join(lines))
        box.exec()

        if clean_button is not None and box.clickedButton() is clean_button:
            removed, freed = self._delete_backups(self._last_backups)
            QMessageBox.information(
                self,
                "Backups deleted",
                f"Deleted {removed} file(s), reclaiming {human_bytes(freed)}.",
            )

        # The files on disk changed, so every cached hash and thumbnail is stale.
        rescan: list[Path] = []
        for result in succeeded:
            self.cache.invalidate(result.archive)
            self.thumbs.invalidate(result.archive)
            if result.output is not None and result.output != result.archive:
                self.archives.pop(result.archive, None)
                self.archives[result.output] = ArchiveInfo(
                    path=result.output, kind=_kind_of(result.output), size=0, mtime_ns=0
                )
                rescan.append(result.output)
            else:
                # Rewritten in place: the pages hashed earlier no longer exist.
                # Leaving them would keep the finished groups on screen with Apply
                # still enabled, and pressing it again fails on every archive.
                info = self.archives.get(result.archive)
                if info is not None:
                    info.pages = []
                    info.page_count = 0
                rescan.append(result.archive)
        self._refresh_archive_list()
        self.rebuild_groups()
        self._session_changed()
        # Started once the removal thread has fully exited; see
        # _on_removal_thread_finished. Until then a scan would be refused.
        self._pending_rescan = rescan
        self.status_label.setText("Removal finished.")

    def _show_shortfall(self, report: RemovalReport) -> None:
        """A run, real or dry, refused before its first book: nothing changed."""
        QMessageBox.warning(
            self,
            "Not enough disk space",
            "\n\n".join(s.describe() for s in report.space_shortfall)
            + "\n\nFree some space, or choose a backup or output folder on another "
            "drive in Settings, and apply again.",
        )

    def _show_dry_run_result(self, report: RemovalReport) -> None:
        """Report what a dry run would have done. Nothing on disk changed, so the
        library, cache and thumbnails are left exactly as they were."""
        lines = [
            f"Dry run: {report.total_removed} page(s) would be removed from "
            f"{len(report.succeeded)} archive(s), freeing {human_bytes(report.total_freed)}.",
            "Nothing was changed.",
        ]
        converted = [r for r in report.succeeded if r.converted]
        if converted:
            lines.append(
                f"{len(converted)} .cbr/.cb7 file(s) would be rebuilt as .cbz "
                "(those formats cannot be written in place)."
            )
        if report.failed:
            lines.append("")
            lines.append(f"{len(report.failed)} archive(s) would fail:")
            lines += [f"  {r.archive.name}: {r.error}" for r in report.failed[:8]]
        lines += self._skipped_note("would be skipped")
        lines += self._cancel_note(report, "were not checked")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Dry run finished")
        box.setText(chr(10).join(lines))
        box.exec()
        self.status_label.setText("Dry run finished. Nothing was changed.")

    def _delete_backups(self, backups: list[Path]) -> tuple[int, int]:
        """Delete backup files, returning how many went and how much was freed."""
        removed = 0
        freed = 0
        for backup in backups:
            try:
                size = backup.stat().st_size
                backup.unlink()
            except OSError as exc:
                log.warning("could not delete backup %s: %s", backup, exc)
                continue
            removed += 1
            freed += size
        self._last_backups = [b for b in backups if b.exists()]
        return removed, freed

    def show_history(self) -> None:
        if self._job is not _Job.IDLE:
            return
        # Restoring replaces files, which Windows refuses while a thumbnail holds one open.
        self.thumbs.release_archives()
        dialog = HistoryDialog(self.cache, self)
        dialog.exec()
        if dialog.restored:
            self._after_restore(dialog.restored)

    def _after_restore(self, items: list) -> None:
        """Bring the library in line with books that were just put back."""
        rescan: list[Path] = []
        added = 0
        for item in items:
            self.thumbs.invalidate(item.archive)
            if item.output != item.archive:
                # A cbr rebuilt as cbz: the cbz is gone and the cbr is back.
                self.archives.pop(item.output, None)
                self.thumbs.invalidate(item.output)
            # Books restored from History come back into the library even if they
            # had been taken out of it, or the restore would look like it did nothing.
            added += item.archive not in self.archives
            self.archives[item.archive] = ArchiveInfo(
                path=item.archive, kind=_kind_of(item.archive), size=0, mtime_ns=0
            )
            rescan.append(item.archive)
        self._refresh_archive_list()
        self.rebuild_groups()
        self._session_changed()
        note = f" {added} of them were added back to the library." if added else ""
        self.status_label.setText(f"Restored {len(items)} book(s).{note}")
        if rescan:
            # Kept until the rescan ends; its progress messages would bury it.
            self._startup_note = f"Restored {len(items)} book(s).{note}"
            self._run_scan(rescan)

    @Slot()
    def clean_up_backups(self) -> None:
        """Find and offer to delete .bak files left next to imported archives."""
        policy = self.settings.backup_policy()
        found: list[Path] = []
        seen: set[Path] = set()
        folders = {a.parent for a in self.archives}
        if policy.directory is not None:
            folders.add(policy.directory)
        for folder in folders:
            if not folder.is_dir():
                continue
            for backup in folder.iterdir():
                if (
                    is_backup_name(backup.name, policy.suffix)
                    and backup.is_file()
                    and backup not in seen
                ):
                    seen.add(backup)
                    found.append(backup)

        if not found:
            QMessageBox.information(
                self, "No backups found", "There are no backup files to clean up."
            )
            return

        total = sum(b.stat().st_size for b in found)
        preview = "\n".join(f"  {b.name}" for b in sorted(found)[:10])
        more = "\n  ...and " + str(len(found) - 10) + " more" if len(found) > 10 else ""
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Delete backups")
        confirm.setText(
            f"Delete {len(found)} backup file(s), reclaiming {human_bytes(total)}?"
            "\n\n" + preview + more + "\n\nThis cannot be undone."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        removed, freed = self._delete_backups(found)
        QMessageBox.information(
            self,
            "Backups deleted",
            f"Deleted {removed} file(s), reclaiming {human_bytes(freed)}.",
        )

    @Slot()
    def _on_removal_thread_finished(self) -> None:
        self._removal_worker = None
        if self._closing:
            return
        self._job_changed()
        # Re-hash just the books that were rewritten, so the review reflects what
        # is on disk now without a manual rescan of the whole library.
        rescan = [p for p in self._pending_rescan if p in self.archives]
        self._pending_rescan = []
        if rescan:
            self._run_scan(rescan)

    # -- session -----------------------------------------------------------
    def open_startup(self, paths: list[Path] | None = None) -> None:
        """Bring back the last library (if enabled), add `paths`, and scan.

        Only a restored library is scanned automatically. Unchanged books come
        straight from the hash cache, and the scan is what lets the saved review
        decisions find their groups again.
        """
        self._recover_interrupted(paths)
        restored = self._restore_session() if self.settings.restore_session else 0
        if paths:
            self.import_paths(paths)
        if restored:
            self.start_scan()
        if self.settings.check_updates:
            self.check_for_updates(quiet=True)

    # -- updates -----------------------------------------------------------
    def check_for_updates(self, *, quiet: bool) -> None:
        """Ask GitHub for the latest release. Quietly, only a newer one is mentioned."""
        if self._update_checker is not None:
            return
        checker = UpdateChecker(self)
        checker.finished.connect(
            lambda release, error: self._on_update_result(release, error, quiet)
        )
        self._update_checker = checker
        if not quiet:
            self.status_label.setText("Checking for updates...")
        checker.start()

    def _on_update_result(self, release: Release | None, error: str, quiet: bool) -> None:
        self._update_checker = None
        if self._closing:
            return
        if release is not None and is_newer(release.version):
            self.update_link.setText(
                f'<a href="{release.url}">Version {release.version} is available</a>'
            )
            self.update_link.setVisible(True)
            if not quiet:
                box = QMessageBox(self)
                box.setWindowTitle("Update available")
                box.setText(
                    f"{APP_NAME} {release.version} is available; this is "
                    f"{__version__}."
                )
                open_page = box.addButton("Open Download Page", QMessageBox.ButtonRole.AcceptRole)
                box.addButton(QMessageBox.StandardButton.Close)
                box.exec()
                if box.clickedButton() is open_page:
                    QDesktopServices.openUrl(QUrl(release.url))
            return
        if quiet:
            return  # a failed or unnecessary background check is not worth a word
        if error:
            QMessageBox.warning(
                self, "Could not check for updates", error[:1].upper() + error[1:] + "."
            )
        else:
            QMessageBox.information(
                self, "No update", f"You have the latest version, {__version__}."
            )
        self._refresh_status()

    def _recover_interrupted(self, paths: list[Path] | None) -> None:
        """Put back books a killed removal left only as a backup.

        Runs before the last session's books are looked for, or a book that is
        only missing because of it would be reported as gone.
        """
        roots = list(paths or [])
        session = load_session(self._session_file)
        if session is not None:
            roots += session.archives
        if not roots:
            return
        restored = [r for r in recover_interrupted(roots) if r.restored]
        if restored:
            self._add_startup_note(
                f"Put back {len(restored)} book(s) an interrupted removal had left "
                "only as a backup."
            )

    def _add_startup_note(self, note: str) -> None:
        self._startup_note = f"{self._startup_note} {note}".strip()

    def _restore_session(self) -> int:
        session = load_session(self._session_file)
        if session is None:
            return 0
        present = find_archives([p for p in session.archives if p.is_file()])
        for path in present:
            self.archives.setdefault(
                path, ArchiveInfo(path=path, kind=_kind_of(path), size=0, mtime_ns=0)
            )
        self._restored = dict(session.decisions)
        self._refresh_archive_list()
        found = set(present)
        missing = [p for p in session.archives if p.resolve() not in found]
        if missing:
            self._add_startup_note(
                f"{len(missing)} archive(s) from last time could not be found."
            )
            # The status line is soon overwritten; this stays until dismissed.
            self._show_missing_banner(missing)
        return len(present)

    def _session_changed(self) -> None:
        """Save soon, coalescing a burst of changes (a keyboard review) into one write."""
        self._session_timer.start()

    @Slot()
    def save_session(self) -> None:
        self._session_timer.stop()
        if not self.settings.restore_session:
            return
        # Last session's decisions still waiting on a scan, then everything decided
        # now. Ones that were applied or matched nothing are already gone.
        decisions = dict(self._restored)
        for group in self.groups:
            if group.decision is not Decision.UNDECIDED or group.kept:
                decisions[group.gid] = _saved(group)
            else:
                decisions.pop(group.gid, None)
        save_session(
            self._session_file,
            Session(
                archives=sorted(self.archives, key=lambda p: str(p).lower()),
                decisions=decisions,
            ),
        )

    # -- settings ----------------------------------------------------------
    @Slot()
    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            # The dialog previews themes live, so undo it on cancel.
            apply_theme(self.settings.theme_mode())
            self._restyle()
            return
        self.settings = dialog.result_settings()
        self.settings.save()
        if self.settings.restore_session:
            self.save_session()
        else:
            # Otherwise a library from long ago would reappear if it were turned
            # back on, and the user asked for it not to be kept.
            with contextlib.suppress(OSError):
                self._session_file.unlink(missing_ok=True)
        apply_theme(self.settings.theme_mode())
        self._restyle()
        # Matching options only affect clustering, so no rescan is needed.
        self.rebuild_groups()

    def _on_system_colour_scheme_changed(self, scheme: object = None) -> None:
        # Only "Follow system" follows; an explicit Light or Dark stays put.
        if self._closing or self.settings.theme_mode() is not Theme.SYSTEM:
            return
        apply_theme(Theme.SYSTEM)
        self._restyle()

    def _restyle(self) -> None:
        """Re-apply colours that are not driven by the palette."""
        self.welcome.refresh()
        warning = colour("warning").name()
        self.tools_banner.setStyleSheet(
            f"#toolsBanner {{ border: 1px solid {warning}; border-radius: 6px; }}"
        )
        self.tools_banner_text.setStyleSheet(f"color: {warning};")
        self.missing_banner.setStyleSheet(
            f"#missingBanner {{ border: 1px solid {warning}; border-radius: 6px; }}"
        )
        self.missing_banner_text.setStyleSheet(f"color: {warning};")
        for row in range(self.archive_list.count()):
            item = self.archive_list.item(row)
            info = self.archives.get(Path(item.data(Qt.ItemDataRole.UserRole)))
            if info is not None and info.error:
                item.setForeground(QBrush(colour("delete")))
        self.detail_warning.setStyleSheet(f"color: {colour('warning').name()};")
        self.detail_hint.setStyleSheet(f"color: {colour('muted').name()};")
        self.library_summary.setStyleSheet(f"color: {colour('muted').name()};")
        for row in range(self.group_list.count()):
            item = self.group_list.item(row)
            group = next((g for g in self.groups if g.gid == item.data(ROLE_GID)), None)
            if group is not None:
                item.setForeground(QBrush(_decision_colour(group.decision)))

    # -- misc --------------------------------------------------------------
    @property
    def _job(self) -> _Job:
        """What the window is doing, read from the workers so it cannot drift.

        A worker's slot is cleared only once its thread has fully exited, so a
        removal counts as running until then, results dialog and all.
        """
        if self._removal_worker is not None:
            return _Job.REMOVING
        if self._scan_worker is not None:
            return _Job.SCANNING
        return _Job.IDLE

    def _job_changed(self, verb: str = "") -> None:
        """Bring the window in line with a worker that has just started or ended."""
        job = self._job
        busy = job is not _Job.IDLE
        # While archives are being rewritten the review panels stay untouched:
        # browsing a group makes the thumbnail cache open the very files the
        # removal is about to swap, and on Windows an open file cannot be replaced.
        self._central.setEnabled(job is not _Job.REMOVING)
        self.progress.setVisible(busy)
        self.btn_cancel.setVisible(busy)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("Cancel")
        if busy:
            self.progress.setValue(0)
            self.status_label.setText(f"{verb}...")
            self._update_actions()
        else:
            self._refresh_status()

    def _update_actions(self, marked_pages: int | None = None) -> None:
        """The one place the toolbar's actions are switched on and off."""
        job = self._job
        idle = job is _Job.IDLE
        # During a scan, Scan is its Cancel button. During a removal it would start
        # hashing the very files being rewritten, so it is off until the removal
        # thread has fully finished.
        self.act_scan.setEnabled(job is not _Job.REMOVING)
        self.act_scan.setText("Cancel Scan" if job is _Job.SCANNING else "Scan")
        if marked_pages is None:
            marked_pages = summarise(self.groups)["marked_pages"]
        # Marking groups mid-scan must not enable Apply: the groups on screen are
        # about to be replaced by the scan's results.
        self.act_apply.setEnabled(idle and marked_pages > 0)
        for action in (
            self.act_add_files,
            self.act_add_folder,
            self.act_remove,
            self.act_clean_backups,
            # Its thumbnails open archives, which must stay closed during a removal.
            self.act_remembered,
            # Restoring moves the very files a scan or removal is working on.
            self.act_history,
            self.act_settings,
        ):
            action.setEnabled(idle)

    def _refresh_status(self) -> None:
        stats = summarise(self.groups)
        self._update_actions(stats["marked_pages"])
        if not self.groups:
            if any(a.pages for a in self.archives.values()):
                self.status_label.setText("No duplicate pages found with these settings.")
            return
        self.status_label.setText(
            f"{stats['groups']} group(s), {stats['pages']} duplicate page(s). "
            f"Marked: {stats['marked_pages']} page(s), "
            f"{human_bytes(stats['recoverable'])} recoverable."
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        workers = [w for w in (self._scan_worker, self._removal_worker) if w is not None]
        # Both stop between books: a scan drops the books not yet started, and a
        # removal finishes swapping in the one it is on, never half of it.
        for worker in workers:
            worker.cancel()
        for worker in workers:
            worker.wait(CLOSE_WAIT_MS)
        running = [w for w in workers if w.isRunning()]
        if running:
            # Still writing to the hash cache (and a removal to disk), so neither
            # the cache nor the thumbnail pool may be shut yet. Stay open, and
            # close by ourselves the moment the last one is done.
            event.ignore()
            for worker in running:
                if worker not in self._close_hooked:  # once, however often Close is hit
                    self._close_hooked.append(worker)
                    worker.finished.connect(self._close_when_idle)
            self.status_label.setText("Finishing the current book, then closing...")
            return
        self.save_session()
        self.thumbs.shutdown()
        self.cache.close()
        super().closeEvent(event)

    @Slot()
    def _close_when_idle(self) -> None:
        # Queued after the thread-finished handlers, which clear the worker slots.
        QTimer.singleShot(0, self._retry_close)

    def _retry_close(self) -> None:
        if self._scan_worker is None and self._removal_worker is None:
            self.close()


class _ConfirmDialog(QDialog):
    """Last stop before anything on disk is touched."""

    def __init__(
        self,
        plans: list[RemovalPlan],
        skipped: list[RemovalPlan],
        settings: AppSettings,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Apply removals")
        self.setMinimumWidth(560)

        total_pages = sum(len(p.remove_names) for p in plans)
        layout = QVBoxLayout(self)

        headline = QLabel(
            f"<b>{total_pages} page(s) will be removed from "
            f"{len(plans)} archive(s).</b>"
        )
        headline.setWordWrap(True)
        layout.addWidget(headline)

        if settings.output_dir:
            where = f"Cleaned copies go to: {settings.output_dir}\nOriginals stay as they are."
        elif settings.backup_enabled:
            target = settings.backup_dir or "alongside each original, as <name>.bak"
            where = f"Originals will be replaced.\nBackups: {target}"
        else:
            where = "Originals will be replaced with NO backup."
        note = QLabel(where)
        note.setWordWrap(True)
        layout.addWidget(note)

        # A backup on the book's own drive is a rename; on another it is a copy.
        self.volume_note: QLabel | None = None
        backup_dir = settings.backup_policy().directory
        if (
            not settings.output_dir
            and settings.backup_enabled
            and backup_dir is not None
            and any(not on_same_volume(backup_dir, p.archive) for p in plans)
        ):
            self.volume_note = QLabel(
                "The backup folder is on a different drive from these books, so each "
                "one is copied there in full, not moved: the backup is a complete "
                "extra copy, and needs that much free space, before its original "
                "is removed."
            )
            self.volume_note.setWordWrap(True)
            layout.addWidget(self.volume_note)

        listing = QListWidget()
        for plan in plans:
            listing.addItem(
                f"{plan.archive.name}  —  removing {len(plan.remove_names)}, "
                f"{plan.remaining_pages} page(s) left"
            )
        listing.setMaximumHeight(240)
        layout.addWidget(listing)

        # Shown here as well as left out, so nothing is skipped silently.
        self.skipped_listing: QListWidget | None = None
        if skipped:
            why = QLabel(
                f"<b>Skipping {len(skipped)} book(s)</b> that would lose more than "
                f"{DEFAULT_MAX_FRACTION:.0%} of their pages. That usually means two copies "
                "of the same issue are matching each other, not adverts."
            )
            why.setWordWrap(True)
            layout.addWidget(why)
            self.skipped_listing = QListWidget()
            for plan in skipped:
                self.skipped_listing.addItem(
                    f"{plan.archive.name}  —  would remove {len(plan.remove_names)} of "
                    f"{plan.original_pages}"
                )
            self.skipped_listing.setMaximumHeight(120)
            layout.addWidget(self.skipped_listing)

        self.chk_dry_run = QCheckBox("Dry run (report what would happen, change nothing)")
        layout.addWidget(self.chk_dry_run)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def dry_run(self) -> bool:
        return self.chk_dry_run.isChecked()


class _Panel(QWidget):
    """One column of the main view; see MainWindow._panel."""

    title_label: QLabel


# Archive kinds that need an external tool to be read at all.
_NEEDS_TOOL = (ArchiveKind.RAR, ArchiveKind.SEVENZIP)


class _Job(enum.Enum):
    """What the window is busy with; see MainWindow._job."""

    IDLE = "idle"
    SCANNING = "scanning"
    REMOVING = "removing"


def _widget_shortcut(
    keys: QKeySequence, widget: QWidget, handler: Callable[[], object]
) -> QShortcut:
    """A shortcut that fires only while `widget` itself has focus.

    The context is set after construction on purpose: PySide6 silently ignores a
    context passed to the constructor alongside a callable, which leaves the
    shortcut window-wide.
    """
    shortcut = QShortcut(keys, widget, handler)
    shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
    return shortcut


def _saved(group: DuplicateGroup) -> SavedDecision:
    return SavedDecision(group.decision, set(group.kept), {p.key for p in group.pages})


def _skipped_lines(skipped: list[RemovalPlan], limit: int = 10) -> str:
    lines = [
        f"  {p.archive.name}: would remove {len(p.remove_names)} of {p.original_pages}"
        for p in skipped[:limit]
    ]
    if len(skipped) > limit:
        lines.append(f"  ...and {len(skipped) - limit} more")
    return "\n".join(lines)


def _decision_colour(decision: Decision) -> QColor:
    """Row colour for a group. Undecided uses the palette so it follows the theme."""
    if decision is Decision.UNDECIDED:
        if QApplication.instance() is not None:
            return QApplication.palette().color(QPalette.ColorRole.Text)
        return QColor(Qt.GlobalColor.black)
    return colour(
        {
            Decision.DELETE: "delete",
            Decision.KEEP: "keep",
            Decision.IGNORE: "ignore",
        }[decision]
    )


def _kind_of(path: Path):
    from ..core.archive import detect_kind

    return detect_kind(path)
