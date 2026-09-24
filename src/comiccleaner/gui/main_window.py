"""The main application window: import, review, remove."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
    QPalette,
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

from .. import APP_NAME, GITHUB_URL
from ..core.archive import ARCHIVE_SUFFIXES
from ..core.cache import HashCache
from ..core.extern import (
    SEVENZIP_URL,
    can_extract,
    install_hint,
    refresh_backends,
    sevenzip_path,
)
from ..core.grouping import build_groups, sort_groups, summarise
from ..core.model import (
    ArchiveInfo,
    ArchiveKind,
    Decision,
    DuplicateGroup,
    MatchKind,
    PageEntry,
)
from ..core.remover import build_plans, is_backup_name
from ..core.scanner import find_archives
from ..resources import app_icon
from ..units import human_bytes
from .about import AboutDialog
from .ignored import IgnoredDialog
from .preview import PagePreviewDialog, page_distance
from .session import SESSION_FILE, SavedDecision, Session, load_session, save_session
from .settings import AppSettings, SettingsDialog, cache_path
from .theme import apply_theme, colour
from .thumbs import THUMB_SIZE, ThumbnailCache
from .welcome import WelcomePanel
from .workers import RemovalWorker, ScanWorker

log = logging.getLogger(__name__)

ROLE_GID = Qt.ItemDataRole.UserRole + 1
ROLE_PAGE_KEY = Qt.ItemDataRole.UserRole + 2
ROLE_THUMB_KEY = Qt.ItemDataRole.UserRole + 3

# The three columns share these so their list areas start and end together.
PANEL_HEADER_HEIGHT = 30
PANEL_FOOTER_HEIGHT = 34

SORT_MODES = [
    ("Books affected", "books"),
    ("Space recoverable", "space"),
    ("Number of copies", "count"),
    ("Image size", "size"),
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
        # Books a finished removal rewrote, scanned again once its thread exits.
        self._pending_rescan: list[Path] = []
        self._sort_key = "books"
        self._last_backups: list[Path] = []
        # The pages shown in the detail grid, in grid order, for the preview.
        self._shown_pages: list[PageEntry] = []

        # Kept beside the hash cache, so anything that relocates one moves both.
        self._session_file = db_path.with_name(SESSION_FILE)
        # Decisions from the last session whose groups have not been rebuilt yet.
        self._restored: dict[str, SavedDecision] = {}
        # Shown once the restore scan ends; the scan's own messages would bury it.
        self._startup_note = ""
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.setInterval(1500)
        self._session_timer.timeout.connect(self.save_session)
        # Set once the window starts closing. A worker's result can already be
        # queued by then, and must not land on a cache that has been shut.
        self._closing = False

        self._build_ui()
        self._restyle()
        self._refresh_status()

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
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_tools_banner())
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

        self.act_clean_backups = QAction("Clean Up Backups...", self)
        self.act_clean_backups.setToolTip(
            "Delete .bak files left next to your archives by previous runs."
        )
        self.act_clean_backups.triggered.connect(self.clean_up_backups)

        self.act_ignored = QAction("Ignored Pages...", self)
        self.act_ignored.setToolTip("See the pages you have ignored, and bring any back.")
        self.act_ignored.triggered.connect(self.manage_ignored)

        self.act_settings = QAction("Settings", self)
        self.act_settings.triggered.connect(self.open_settings)

        bar.addAction(self.act_add_files)
        bar.addAction(self.act_add_folder)
        bar.addAction(self.act_remove)
        bar.addSeparator()
        bar.addAction(self.act_scan)
        bar.addSeparator()
        bar.addAction(self.act_apply)
        bar.addAction(self.act_clean_backups)
        bar.addSeparator()
        bar.addAction(self.act_ignored)
        bar.addAction(self.act_settings)
        bar.addWidget(self._help_button())

    def _help_button(self) -> QWidget:
        """Toolbar dropdown with About and the project link."""
        self.act_about = QAction("About", self)
        self.act_about.triggered.connect(self.show_about)

        self.act_github = QAction("GitHub", self)
        self.act_github.setToolTip(GITHUB_URL)
        self.act_github.triggered.connect(self.open_github)

        menu = QMenu(self)
        menu.addAction(self.act_about)
        menu.addAction(self.act_github)

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
    ) -> QWidget:
        """One column of the main view.

        Every column is built the same way - a fixed-height header, the list
        itself, then a fixed-height footer - so the three list areas line up
        across the window instead of each starting at its own height.
        """
        panel = QWidget()
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
        QShortcut(
            QKeySequence(QKeySequence.StandardKey.Delete),
            self.archive_list,
            self.remove_selected_archives,
            context=Qt.ShortcutContext.WidgetShortcut,
        )

        # Elided rather than wrapped, so the footer height never changes.
        self.library_summary = QLabel("No archives imported.")
        self.library_summary.setWordWrap(False)

        return self._panel(
            "Library",
            None,
            self.archive_list,
            self._row(self.library_summary, stretch_last=True),
        )

    def _build_groups_panel(self) -> QWidget:
        self.sort_combo = QComboBox()
        for label, key in SORT_MODES:
            self.sort_combo.addItem(label, key)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self.group_list = QListWidget()
        self.group_list.setIconSize(QSize(72, 72))
        self.group_list.setAlternatingRowColors(True)
        self.group_list.currentItemChanged.connect(self._on_group_selected)

        self.btn_mark_all = QPushButton("Mark all for removal")
        self.btn_mark_all.clicked.connect(lambda: self._set_all_decisions(Decision.DELETE))
        self.btn_clear_all = QPushButton("Clear all")
        self.btn_clear_all.clicked.connect(
            lambda: self._set_all_decisions(Decision.UNDECIDED)
        )

        return self._panel(
            "Duplicate groups",
            self._row(QLabel("Sort:"), self.sort_combo),
            self.group_list,
            self._row(self.btn_mark_all, self.btn_clear_all),
        )

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

        # Single keys, so a long review can be driven from the keyboard. Each one
        # moves on to the next group still waiting for a decision.
        self.btn_delete = QPushButton("Remove this page everywhere")
        self.btn_delete.setShortcut("D")
        self.btn_delete.setToolTip("Remove every ticked copy, then go to the next group (D)")
        self.btn_delete.clicked.connect(lambda: self._set_current_decision(Decision.DELETE))
        self.btn_keep = QPushButton("Keep")
        self.btn_keep.setShortcut("K")
        self.btn_keep.setToolTip("Keep every copy, then go to the next group (K)")
        self.btn_keep.clicked.connect(lambda: self._set_current_decision(Decision.KEEP))
        self.btn_ignore = QPushButton("Ignore")
        self.btn_ignore.setShortcut("I")
        self.btn_ignore.setToolTip(
            "Hide this page from future scans (I). Undo it from Ignored Pages."
        )
        self.btn_ignore.clicked.connect(self._ignore_current)
        for button in (self.btn_delete, self.btn_keep, self.btn_ignore):
            button.setEnabled(False)

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
        if self._removal_worker is not None:
            return  # archives are being rewritten; hashing them now would race it
        if not self.archives:
            QMessageBox.information(self, "Nothing to scan", "Import some archives first.")
            return
        self._run_scan(list(self.archives))

    def _run_scan(self, paths: list[Path]) -> None:
        self._set_busy(True, "Scanning")
        self.act_scan.setText("Cancel Scan")
        worker = ScanWorker(paths, self.cache, self)
        worker.progressed.connect(self._on_scan_progress)
        worker.finished_scan.connect(self._on_scan_done)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_scan_thread_finished)
        self._scan_worker = worker
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
        self.act_scan.setText("Scan")
        self._set_busy(False)
        self._refresh_status()
        if self._startup_note:
            self.status_label.setText(f"{self._startup_note} {self.status_label.text()}")
            self._startup_note = ""

    @Slot(str)
    def _on_worker_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Something went wrong", message)

    # -- grouping ----------------------------------------------------------
    def rebuild_groups(self) -> None:
        """Re-cluster from already-hashed pages. No disk access, so it is instant."""
        scanned = [a for a in self.archives.values() if a.pages]
        previous = {g.gid: g for g in self.groups}

        self.groups = build_groups(
            scanned, self.settings.grouping(), ignored=self.cache.ignored_hashes()
        )
        # Carry over decisions the user already made, this session or the last.
        for group in self.groups:
            old = previous.get(group.gid)
            restored = self._restored.pop(group.gid, None)
            source = old if old is not None else restored
            if source is not None:
                group.decision = source.decision
                group.kept = {k for k in source.kept if k in {p.key for p in group.pages}}

        self.groups = sort_groups(self.groups, self._sort_key)
        self._refresh_group_list()
        self._refresh_status()

    @Slot(int)
    def _on_sort_changed(self, index: int) -> None:
        self._sort_key = self.sort_combo.itemData(index)
        self.groups = sort_groups(self.groups, self._sort_key)
        self._refresh_group_list()

    # -- view refresh ------------------------------------------------------
    def _refresh_archive_list(self) -> None:
        self.archive_list.clear()
        for path in sorted(self.archives, key=lambda p: str(p).lower()):
            info = self.archives[path]
            if info.error:
                text = f"{path.name}  —  unreadable"
            elif info.pages:
                text = f"{path.name}  —  {info.page_count} pages"
            else:
                text = f"{path.name}  —  not scanned"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path) + (f"\n{info.error}" if info.error else ""))
            if info.error:
                item.setForeground(QBrush(QColor("#c0392b")))
            self.archive_list.addItem(item)

        scanned = sum(1 for a in self.archives.values() if a.pages)
        pages = sum(a.page_count for a in self.archives.values())
        self.library_summary.setText(
            f"{len(self.archives)} archive(s), {scanned} scanned, {pages} pages."
        )
        self.views.setCurrentWidget(self.review if self.archives else self.welcome)
        self._update_tools_banner()

    def _refresh_group_list(self) -> None:
        # Stay on the group being reviewed. If it is gone (just ignored, say), land
        # on whatever slid into its place rather than snapping back to the top of a
        # list the user may have been working down for a while.
        previous = self.group_list.currentItem()
        previous_gid = previous.data(ROLE_GID) if previous is not None else None
        previous_row = self.group_list.currentRow()

        self.group_list.blockSignals(True)
        self.group_list.clear()
        for group in self.groups:
            item = QListWidgetItem(self._group_text(group))
            item.setData(ROLE_GID, group.gid)
            representative = group.representative
            item.setData(ROLE_THUMB_KEY, self.thumbs.key_for(representative))
            item.setIcon(self.thumbs.icon(representative))
            item.setForeground(QBrush(_decision_colour(group.decision)))
            self.group_list.addItem(item)
        self.group_list.blockSignals(False)
        if not self.groups:
            self._clear_detail()
            return
        row = next((i for i, g in enumerate(self.groups) if g.gid == previous_gid), None)
        if row is None:
            row = min(max(previous_row, 0), len(self.groups) - 1)
        self.group_list.setCurrentRow(row)

    def _group_text(self, group: DuplicateGroup) -> str:
        marker = {
            Decision.DELETE: "[remove] ",
            Decision.KEEP: "[keep] ",
            Decision.IGNORE: "[ignored] ",
            Decision.UNDECIDED: "",
        }[group.decision]
        kind = "identical" if group.kind is MatchKind.EXACT else "similar"
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
        )

        warnings = []
        if any(p.flat for p in group.pages):
            warnings.append("Contains blank or solid-colour pages.")
        if group.archive_count == 1:
            warnings.append("All copies are in one book — this may be intentional.")
        if any(p.index == 0 for p in group.pages):
            warnings.append("Includes a first page, which is usually the cover.")
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
    def _on_thumb_ready(self, key: str, pixmap: object) -> None:
        from PySide6.QtGui import QIcon

        icon = QIcon(pixmap)
        for widget in (self.group_list, self.page_list):
            for row in range(widget.count()):
                item = widget.item(row)
                if item.data(ROLE_THUMB_KEY) == key:
                    item.setIcon(icon)

    # -- decisions ---------------------------------------------------------
    def _set_current_decision(self, decision: Decision) -> None:
        group = self._current_group()
        if group is None:
            return
        group.decision = decision
        self._update_group_item(group)
        self._refresh_status()
        self._session_changed()
        self._select_next_undecided(self.group_list.currentRow() + 1)

    def _set_all_decisions(self, decision: Decision) -> None:
        for group in self.groups:
            group.decision = decision
        self._refresh_group_list()
        self._refresh_status()
        self._session_changed()

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
            "Ignored. Bring it back any time from Ignored Pages on the toolbar."
        )

    def _select_next_undecided(self, start: int) -> None:
        """Move to the first undecided group at or after `start`, wrapping round."""
        count = len(self.groups)
        for row in [*range(start, count), *range(0, min(start, count))]:
            if self.groups[row].decision is Decision.UNDECIDED:
                self.group_list.setCurrentRow(row)
                return
        if count:
            self.status_label.setText(
                "Every group has a decision. Apply Removals when you are ready."
            )

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

    @Slot()
    def manage_ignored(self) -> None:
        dialog = IgnoredDialog(self.cache, self.thumbs, self)
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
        if self._scan_worker is not None or self._removal_worker is not None:
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

        emptied = [p for p in plans if p.remaining_pages <= 0]
        if emptied:
            QMessageBox.critical(
                self,
                "That would empty a book",
                "These archives would lose every page, so nothing was changed:\n\n"
                + "\n".join(f"  {p.archive.name}" for p in emptied[:10]),
            )
            return

        dialog = _ConfirmDialog(plans, self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Thumbnails keep archives open, and Windows will not let a rewritten
        # file be renamed while a handle is alive.
        self.thumbs.release_archives()

        self._set_busy(True, "Removing", lock_views=True)
        worker = RemovalWorker(
            plans,
            backup=self.settings.backup_policy(),
            output_dir=self.settings.output_path(),
            dry_run=dialog.dry_run(),
            compress=self.settings.compress,
            parent=self,
        )
        worker.progressed.connect(self._on_removal_progress)
        worker.finished_removal.connect(self._on_removal_done)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_removal_thread_finished)
        self._removal_worker = worker
        self._removal_was_dry_run = dialog.dry_run()
        self._removal_total = len(plans)
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

    def _cancel_note(self, report: object, outcome: str) -> list[str]:
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
    def _on_removal_done(self, report: object) -> None:
        if self._closing:
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
        if converted:
            lines.append(
                f"{len(converted)} .cbr/.cb7 file(s) were rebuilt as .cbz "
                "(those formats cannot be written in place)."
            )
        if failed:
            lines.append("")
            lines.append(f"{len(failed)} archive(s) failed and were left untouched:")
            lines += [f"  {r.archive.name}: {r.error}" for r in failed[:8]]
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

    def _show_dry_run_result(self, report: object) -> None:
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
        self._set_busy(False)
        self._refresh_status()
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
        restored = self._restore_session() if self.settings.restore_session else 0
        if paths:
            self.import_paths(paths)
        if restored:
            self.start_scan()

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
        missing = len(session.archives) - len(present)
        if missing:
            self._startup_note = (
                f"{missing} archive(s) from last time could not be found."
            )
        return len(present)

    def _session_changed(self) -> None:
        """Save soon, coalescing a burst of changes (a keyboard review) into one write."""
        self._session_timer.start()

    @Slot()
    def save_session(self) -> None:
        self._session_timer.stop()
        if not self.settings.restore_session:
            return
        decisions = dict(self._restored)  # not regrouped yet, so still worth keeping
        for group in self.groups:
            if group.decision is not Decision.UNDECIDED or group.kept:
                decisions[group.gid] = SavedDecision(group.decision, set(group.kept))
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

    def _restyle(self) -> None:
        """Re-apply colours that are not driven by the palette."""
        self.welcome.refresh()
        warning = colour("warning").name()
        self.tools_banner.setStyleSheet(
            f"#toolsBanner {{ border: 1px solid {warning}; border-radius: 6px; }}"
        )
        self.tools_banner_text.setStyleSheet(f"color: {warning};")
        self.detail_warning.setStyleSheet(f"color: {colour('warning').name()};")
        self.detail_hint.setStyleSheet(f"color: {colour('muted').name()};")
        self.library_summary.setStyleSheet(f"color: {colour('muted').name()};")
        for row in range(self.group_list.count()):
            item = self.group_list.item(row)
            group = next((g for g in self.groups if g.gid == item.data(ROLE_GID)), None)
            if group is not None:
                item.setForeground(QBrush(_decision_colour(group.decision)))

    # -- misc --------------------------------------------------------------
    def _set_busy(self, busy: bool, verb: str = "", *, lock_views: bool = False) -> None:
        # While archives are being rewritten the review panels stay untouched:
        # browsing a group makes the thumbnail cache open the very files the
        # removal is about to swap, and on Windows an open file cannot be replaced.
        self.centralWidget().setEnabled(not (busy and lock_views))
        self.progress.setVisible(busy)
        self.btn_cancel.setVisible(busy)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("Cancel")
        if busy:
            self.progress.setValue(0)
            self.status_label.setText(f"{verb}...")
        for action in (
            self.act_add_files,
            self.act_add_folder,
            self.act_remove,
            self.act_apply,
            self.act_clean_backups,
            # Its thumbnails open archives, which must stay closed during a removal.
            self.act_ignored,
            self.act_settings,
        ):
            action.setEnabled(not busy)
        if not busy:
            self._refresh_status()

    def _refresh_status(self) -> None:
        stats = summarise(self.groups)
        # Marking groups mid-scan used to re-enable this while the groups on screen
        # were about to be replaced by the scan's results.
        idle = self._scan_worker is None and self._removal_worker is None
        self.act_apply.setEnabled(stats["marked_pages"] > 0 and idle)
        if not self.groups:
            if any(a.pages for a in self.archives.values()):
                self.status_label.setText("No duplicate pages found with these settings.")
            return
        self.status_label.setText(
            f"{stats['groups']} group(s), {stats['pages']} duplicate page(s). "
            f"Marked: {stats['marked_pages']} page(s), "
            f"{human_bytes(stats['recoverable'])} recoverable."
        )

    def closeEvent(self, event: object) -> None:
        self._closing = True
        for worker in (self._scan_worker, self._removal_worker):
            if worker is not None:
                worker.cancel()
                worker.wait(4000)
        self.save_session()
        self.thumbs.shutdown()
        self.cache.close()
        super().closeEvent(event)


class _ConfirmDialog(QDialog):
    """Last stop before anything on disk is touched."""

    def __init__(self, plans: list, settings: AppSettings, parent: QWidget) -> None:
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

        listing = QListWidget()
        for plan in plans:
            listing.addItem(
                f"{plan.archive.name}  —  removing {len(plan.remove_names)}, "
                f"{plan.remaining_pages} page(s) left"
            )
        listing.setMaximumHeight(240)
        layout.addWidget(listing)

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


# Archive kinds that need an external tool to be read at all.
_NEEDS_TOOL = (ArchiveKind.RAR, ArchiveKind.SEVENZIP)


def _decision_colour(decision: Decision) -> QColor:
    """Row colour for a group. Undecided uses the palette so it follows the theme."""
    if decision is Decision.UNDECIDED:
        app = QApplication.instance()
        if app is not None:
            return app.palette().color(QPalette.ColorRole.Text)
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
