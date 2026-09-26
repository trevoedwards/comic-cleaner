"""The review tools added on top of the basic loop: defer, multi-select, undo, edges,
scoped applies, the confirm dialog's detail, and the dialogs around them."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox  # noqa: E402

from comiccleaner.core.archive import is_page_name  # noqa: E402
from comiccleaner.core.grouping import WARN_MID_BOOK  # noqa: E402
from comiccleaner.core.model import Decision  # noqa: E402
from comiccleaner.core.pack import export_pack  # noqa: E402
from comiccleaner.core.remover import RemovalPlan  # noqa: E402
from comiccleaner.gui import main_window  # noqa: E402
from comiccleaner.gui.history import HistoryDialog  # noqa: E402
from comiccleaner.gui.main_window import (  # noqa: E402
    ROLE_GID,
    ROLE_HEADER,
    MainWindow,
    _ConfirmDialog,
)
from comiccleaner.gui.preview import PagePreviewDialog  # noqa: E402
from comiccleaner.gui.remembered import RememberedDialog  # noqa: E402
from comiccleaner.gui.settings import AppSettings, SettingsDialog  # noqa: E402
from comiccleaner.gui.theme import Theme, apply_theme, colour  # noqa: E402

from .conftest import make_page, write_archive  # noqa: E402
from .test_gui import apply_and_wait, pump_until, scan_and_wait  # noqa: E402

USER_ROLE = Qt.ItemDataRole.UserRole


@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_surprise_boxes(monkeypatch):
    """A modal box nobody expected would hang the run; fail loudly instead.

    Tests that expect one patch these again.
    """
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, _unexpected_box)


@pytest.fixture
def make_window(qapp, tmp_path, monkeypatch):
    """Windows sharing one isolated data folder, as successive launches would."""
    monkeypatch.setattr(QSettings, "value", lambda self, key, default=None: default)
    monkeypatch.setattr(QSettings, "setValue", lambda self, key, value: None)
    monkeypatch.setattr(QSettings, "sync", lambda self: None)
    data = tmp_path / "data"
    monkeypatch.setattr(
        "comiccleaner.gui.main_window.cache_path", lambda: data / "cache.sqlite"
    )
    made: list[MainWindow] = []

    def factory() -> MainWindow:
        win = MainWindow()
        made.append(win)
        return win

    yield factory
    for win in made:
        win.close()


def _unexpected_box(*args, **kwargs):
    raise AssertionError(f"unexpected message box: {args[2] if len(args) > 2 else args}")


@pytest.fixture
def window(make_window):
    return make_window()


def _three_ads(root: Path, story_seed: int = 0) -> Path:
    """Three books of twelve pages, each carrying the same three adverts up front."""
    ads = [make_page(seed=9000 + i) for i in range(3)]
    for number in range(3):
        story = [make_page(seed=story_seed + number * 100 + i) for i in range(9)]
        write_archive(root / f"Book {number}.cbz", [story[0], *ads, *story[1:]])
    return root


def _mixed(root: Path) -> Path:
    """Three books: an advert at the back of each, and a page repeated mid-book."""
    back, middle = make_page(seed=8100), make_page(seed=8200)
    for name in ("A", "B", "C"):
        story = [make_page(seed=ord(name) * 100 + i) for i in range(12)]
        story.insert(6, middle)
        story.append(back)
        write_archive(root / f"{name}.cbz", story)
    return root


def _scanned(window: MainWindow, library: Path) -> MainWindow:
    window.import_paths([library])
    scan_and_wait(window)
    return window


def _pages(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(1 for n in zf.namelist() if is_page_name(n))


def _confirming(monkeypatch, *, dry_run: bool = False) -> list[_ConfirmDialog]:
    shown: list[_ConfirmDialog] = []

    def accept(self):
        shown.append(self)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(_ConfirmDialog, "exec", accept)
    monkeypatch.setattr(_ConfirmDialog, "dry_run", lambda self: dry_run)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)
    return shown


def _wait_for_removal(window: MainWindow) -> None:
    assert pump_until(lambda: window._removal_worker is None), "removal did not finish"
    assert pump_until(lambda: window._scan_worker is None), "rescan did not finish"


# -- defer and the status line ---------------------------------------------


def test_defer_moves_on_and_is_neither_kept_nor_ignored(window, tmp_path):
    _scanned(window, _three_ads(tmp_path / "lib"))
    window.group_list.setCurrentRow(0)
    first = window._current_group()

    window.btn_defer.click()

    assert first.decision is Decision.DEFER
    assert window.group_list.currentRow() == 1
    assert first in window.groups and window.cache.ignored_hashes() == set()
    assert first.pages_to_remove() == []
    assert window.group_list.item(0).text().startswith("[later] ")

    window.btn_keep.click()
    window.btn_keep.click()  # the last undecided group; the deferred one is skipped
    assert "Every group has a decision" in window.status_label.text()

    window.filter_combo.setCurrentIndex(window.filter_combo.findData("deferred"))
    assert [g.gid for g in window._shown_groups] == [first.gid]


def test_the_status_line_counts_undecided_groups(window, tmp_path):
    _scanned(window, _three_ads(tmp_path / "lib"))
    assert window.status_label.text().startswith("3 undecided of 3 group(s)")
    window.group_list.setCurrentRow(0)
    window.btn_keep.click()
    assert window.status_label.text().startswith("2 undecided of 3 group(s)")


# -- several groups at once ------------------------------------------------


def _select_rows(window: MainWindow, *rows: int) -> None:
    window.group_list.setCurrentRow(rows[0])
    for row in rows[1:]:
        window.group_list.item(row).setSelected(True)


def test_decisions_apply_to_every_selected_group(window, tmp_path):
    _scanned(window, _three_ads(tmp_path / "lib"))
    _select_rows(window, 0, 2)
    chosen = [window._shown_groups[0].gid, window._shown_groups[2].gid]

    window.btn_keep.click()

    decisions = {g.gid: g.decision for g in window.groups}
    assert [decisions[gid] for gid in chosen] == [Decision.KEEP, Decision.KEEP]
    assert list(decisions.values()).count(Decision.UNDECIDED) == 1


def test_mark_all_and_ignore_act_on_a_multi_selection(window, tmp_path):
    _scanned(window, _three_ads(tmp_path / "lib"))
    _select_rows(window, 0, 1)
    window.btn_mark_all.click()
    assert [g.decision for g in window._shown_groups].count(Decision.DELETE) == 2

    _select_rows(window, 0, 1)
    window.btn_ignore.click()
    assert len(window.groups) == 1


# -- undo ------------------------------------------------------------------


def test_undo_takes_back_decisions_ticks_and_ignores(window, tmp_path):
    _scanned(window, _three_ads(tmp_path / "lib"))
    assert not window.act_undo.isEnabled()
    window.group_list.setCurrentRow(0)
    gid = window._current_group().gid
    window.btn_delete.click()
    window.undo()
    group = next(g for g in window.groups if g.gid == gid)
    assert group.decision is Decision.UNDECIDED
    assert window._current_group().gid == gid

    item = window.page_list.item(0)
    item.setCheckState(Qt.CheckState.Unchecked)
    assert len(group.kept) == 1
    window.undo()
    assert next(g for g in window.groups if g.gid == gid).kept == set()

    window.group_list.setCurrentRow(0)
    window.btn_ignore.click()
    assert gid not in {g.gid for g in window.groups}
    window.undo()
    assert gid in {g.gid for g in window.groups}
    assert window.cache.ignored_hashes() == set()

    window.undo()
    assert "Nothing to undo" in window.status_label.text()


def test_undo_stops_at_a_removal(window, tmp_path, monkeypatch):
    _scanned(window, _three_ads(tmp_path / "lib"))
    window.group_list.setCurrentRow(0)
    window.btn_delete.click()
    assert window._undo
    apply_and_wait(window, monkeypatch)
    assert not window._undo and not window.act_undo.isEnabled()


def test_a_dry_run_keeps_the_undo_history(window, tmp_path, monkeypatch):
    _scanned(window, _three_ads(tmp_path / "lib"))
    window.group_list.setCurrentRow(0)
    window.btn_delete.click()
    apply_and_wait(window, monkeypatch, dry_run=True)
    assert window._undo


# -- warnings --------------------------------------------------------------


def test_next_warning_finds_the_mid_book_group_and_explains_it(window, tmp_path):
    _scanned(window, _mixed(tmp_path / "lib"))
    window.group_list.setCurrentRow(0)
    window.select_next_warning()
    group = window._current_group()
    assert group.edge_share == 0
    assert WARN_MID_BOOK in window.detail_warning.text()
    assert "story content that recurs" in window.detail_warning.toolTip()

    window.filter_combo.setCurrentIndex(window.filter_combo.findData("midbook"))
    assert [g.gid for g in window._shown_groups] == [group.gid]


def test_the_new_sort_orders_are_offered(window):
    keys = {window.sort_combo.itemData(i) for i in range(window.sort_combo.count())}
    assert {"warning", "edge", "distance"} <= keys


# -- edges only ------------------------------------------------------------


def test_edges_only_unticks_mid_book_copies_and_gives_them_back(window, tmp_path):
    _scanned(window, _mixed(tmp_path / "lib"))
    middle = next(g for g in window.groups if g.edge_share == 0)
    back = next(g for g in window.groups if g.edge_share == 1)

    window.chk_edges.setChecked(True)
    assert window.act_edges_only.isChecked()
    window._set_all_decisions(Decision.DELETE)
    assert middle.pages_to_remove() == [] and len(back.pages_to_remove()) == 3

    window.save_session()
    saved = json.loads(window._session_file.read_text(encoding="utf-8"))["decisions"]
    assert saved[middle.gid]["kept"] == []  # the filter's doing, not a decision

    window.act_edges_only.setChecked(False)
    assert not window.chk_edges.isChecked()
    assert len(middle.pages_to_remove()) == 3


# -- applying to part of the library ---------------------------------------


def test_apply_to_selected_books_leaves_the_rest_alone(window, tmp_path, monkeypatch):
    library = _scanned(window, _three_ads(tmp_path / "lib")) and tmp_path / "lib"
    window._set_all_decisions(Decision.DELETE)
    assert not window.act_apply_selected.isEnabled()
    item = next(
        window.archive_list.item(r) for r in range(window.archive_list.count())
        if Path(window.archive_list.item(r).data(USER_ROLE)).name == "Book 1.cbz"
    )
    item.setSelected(True)
    assert window.act_apply_selected.isEnabled()
    shown = _confirming(monkeypatch)

    window.apply_to_selected()
    _wait_for_removal(window)

    [dialog] = shown
    assert "Only the 1 book(s) selected" in dialog.scope_note.text()
    assert {p.name: _pages(p) for p in library.glob("*.cbz")} == {
        "Book 0.cbz": 12, "Book 1.cbz": 9, "Book 2.cbz": 12,
    }


def test_known_junk_only_applies_just_the_known_groups(window, tmp_path, monkeypatch):
    library = tmp_path / "lib"
    _scanned(window, _three_ads(library))
    known = window.groups[0]
    window.cache.remember(known.gid, {p.dhash for p in known.pages}, note="an advert")
    window.rebuild_groups()
    others = [g for g in window.groups if g.gid != known.gid]
    others[0].decision = Decision.DELETE  # marked, but not known: stays out of this run
    shown = _confirming(monkeypatch)

    window.apply_known_junk()
    _wait_for_removal(window)

    assert "Only known junk" in shown[0].scope_note.text()
    assert all(_pages(p) == 11 for p in library.glob("*.cbz"))


def test_known_junk_only_says_when_there_is_none(window, tmp_path, monkeypatch):
    _scanned(window, _three_ads(tmp_path / "lib"))
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    window.apply_known_junk()
    assert "None of the groups is known junk" in told[0]
    assert window._removal_worker is None


def test_an_imported_signature_is_asked_about_once(window, tmp_path, monkeypatch):
    library = tmp_path / "lib"
    _scanned(window, _three_ads(library))
    known = window.groups[0]
    window.cache.remember(
        known.gid, {p.dhash for p in known.pages}, note="shared", source="imported"
    )
    window.rebuild_groups()
    _confirming(monkeypatch)
    asked: list[str] = []
    answer = [QMessageBox.StandardButton.No]
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: (asked.append(a[2]), answer[0])[1]
    )

    window.apply_known_junk()
    assert window._removal_worker is None
    assert "1 signature(s) from an imported known-junk list" in asked[0]
    assert all(_pages(p) == 12 for p in library.glob("*.cbz"))

    answer[0] = QMessageBox.StandardButton.Yes
    window.apply_known_junk()
    _wait_for_removal(window)
    assert all(_pages(p) == 11 for p in library.glob("*.cbz"))
    assert window._imported_trusted and len(asked) == 2


def test_protected_folders_are_left_out_and_named(window, tmp_path, monkeypatch):
    library = tmp_path / "lib"
    _scanned(window, _three_ads(library / "open"))
    safe = _three_ads(library / "safe", story_seed=50_000)
    window.import_paths([safe])
    scan_and_wait(window)
    window.settings.protected_folders = str(safe)
    window._set_all_decisions(Decision.DELETE)
    shown = _confirming(monkeypatch)

    window.apply_removals()
    _wait_for_removal(window)

    assert "Leaving out 3 book(s)" in shown[0].protected_note.text()
    assert all(_pages(p) == 12 for p in safe.glob("*.cbz"))
    assert all(_pages(p) == 9 for p in (library / "open").glob("*.cbz"))


def test_quarantine_from_settings_keeps_copies(window, tmp_path, monkeypatch):
    _scanned(window, _three_ads(tmp_path / "lib"))
    window.settings.quarantine_dir = str(tmp_path / "q")
    window._set_all_decisions(Decision.DELETE)
    shown = _confirming(monkeypatch)
    window.apply_removals()
    _wait_for_removal(window)
    assert "copied to" in " ".join(
        label.text() for label in shown[0].findChildren(main_window.QLabel)
    )
    assert len(list((tmp_path / "q").rglob("*.jpg"))) == 9


# -- the confirm dialog ----------------------------------------------------


def test_the_confirm_dialog_spells_out_each_book(qapp, tmp_path, monkeypatch):
    settings = AppSettings()
    plans = [
        RemovalPlan(tmp_path / "A.cbz", {"01.jpg", "02.jpg", "03.jpg", "04.jpg"}, 16,
                    indices={"01.jpg": 0, "02.jpg": 1, "03.jpg": 2, "04.jpg": 3}),
        RemovalPlan(tmp_path / "B.cbz", {"12.jpg"}, 12, indices={"12.jpg": 11}),
    ]
    skipped = [RemovalPlan(tmp_path / "C.cbz", {"x", "y"}, 4)]
    protected = [RemovalPlan(tmp_path / "keep" / "D.cbz", {"x"}, 9)]
    dialog = _ConfirmDialog(plans, skipped, settings, None, protected=protected, scope="Scoped.")
    try:
        rows = [dialog.listing.item(i).text() for i in range(dialog.listing.count())]
        assert rows[0] == (
            "A.cbz  —  removing 4 of 16 (25%), 12 left: 01.jpg, 02.jpg, 03.jpg and 1 more"
        )
        assert rows[1] == "B.cbz  —  removing 1 of 12 (8%), 11 left: 12.jpg"
        edges = [dialog.edge_listing.item(i).text() for i in range(dialog.edge_listing.count())]
        assert edges == ["A.cbz  —  its first page", "B.cbz  —  its last page"]
        assert dialog.scope_note.text() == "Scoped."
        assert "D.cbz" in dialog.protected_note.text()
        assert dialog.convert_note is None

        told: list[str] = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
        saved = dialog.save_plan(tmp_path / "plan")
        assert "Nothing was changed" in told[0]
        assert saved is not None
        json_path, csv_path = saved
        assert json_path.name == "plan.json" and csv_path.name == "plan.csv"
        books = json.loads(json_path.read_text(encoding="utf-8"))["books"]
        assert [(Path(b["archive"]).name, b["status"]) for b in books] == [
            ("A.cbz", "planned"), ("B.cbz", "planned"), ("C.cbz", "skipped"),
            ("D.cbz", "skipped"),
        ]
    finally:
        dialog.close()


def test_a_cbr_in_the_plan_is_said_to_become_a_cbz(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(RemovalPlan, "converts", property(lambda self: True))
    dialog = _ConfirmDialog(
        [RemovalPlan(tmp_path / "A.cbr", {"x"}, 8, indices={"x": 3})], [], AppSettings(), None
    )
    try:
        assert "kept as the backup" in dialog.convert_note.text()
    finally:
        dialog.close()


# -- library ---------------------------------------------------------------


def test_the_library_can_be_grouped_and_folded(window, tmp_path):
    for folder in ("Alpha", "Beta"):
        for number in range(2):
            write_archive(
                tmp_path / folder / f"{folder} {number}.cbz", [make_page(number)],
                comicinfo=False,
            )
    window.import_paths([tmp_path / "Alpha", tmp_path / "Beta"])
    window.act_group_library.setChecked(True)

    headers = [
        window.archive_list.item(r) for r in range(window.archive_list.count())
        if window.archive_list.item(r).data(ROLE_HEADER) is not None
    ]
    assert [h.data(ROLE_HEADER) for h in headers] == ["Alpha", "Beta"]
    assert not headers[0].flags() & Qt.ItemFlag.ItemIsSelectable

    book = window.archive_list.item(1)
    book.setSelected(True)
    assert window._library_selection
    window._on_library_item_clicked(headers[0])
    assert book.isHidden() and not window._library_selection
    window._on_library_item_clicked(headers[0])
    assert not book.isHidden()

    window.remove_selected_archives()  # a header in the list must not trip anything up
    window._update_library_texts()


def _click(window: MainWindow, item) -> None:
    from PySide6.QtTest import QTest

    rect = window.archive_list.visualItemRect(item)
    QTest.mouseClick(
        window.archive_list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )


def test_clicking_a_heading_folds_it_and_keeps_other_books_selected(window, tmp_path):
    """A click on a heading used to clear the whole selection, not only the
    books it hid, so the groups snapped back to showing every book."""
    for folder in ("Alpha", "Beta"):
        for number in range(2):
            write_archive(
                tmp_path / folder / f"{folder} {number}.cbz", [make_page(number)],
                comicinfo=False,
            )
    window.import_paths([tmp_path / "Alpha", tmp_path / "Beta"])
    window.act_group_library.setChecked(True)
    window.show()
    rows = [window.archive_list.item(r) for r in range(window.archive_list.count())]
    alpha, beta = (r for r in rows if r.data(ROLE_HEADER) is not None)
    alpha_book, beta_book = rows[1], rows[4]
    alpha_book.setSelected(True)
    beta_book.setSelected(True)
    assert len(window._library_selection) == 2

    _click(window, alpha)

    assert alpha_book.isHidden() and not alpha_book.isSelected()
    assert beta_book.isSelected()
    assert window._library_selection == {Path(beta_book.data(USER_ROLE))}
    assert not alpha.isSelected()

    _click(window, alpha)
    assert not alpha_book.isHidden()
    assert beta_book.isSelected()


def test_a_pdf_is_named_as_unsupported(window, tmp_path, monkeypatch):
    pdf = tmp_path / "comic.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    window.import_paths([pdf])
    assert "PDF is not supported" in told[0]
    assert not window.archives


def test_a_folder_import_counts_what_it_skipped(window, tmp_path):
    library = _three_ads(tmp_path / "lib")
    (library / "manual.pdf").write_bytes(b"%PDF-1.7")
    (library / "setup.rar").write_bytes(b"Rar!")
    (library / "Book 0 sample.cbz").write_bytes((library / "Book 0.cbz").read_bytes())
    window.settings.exclude_globs = "*sample*"

    window.import_paths([library])

    text = window.status_label.text()
    assert "Imported 3 new archive(s)" in text
    assert "Skipped 1 PDF file(s)" in text
    assert "1 plain .rar/.7z file(s)" in text
    assert "Left out 1 file(s) matching your exclude patterns" in text


def test_missing_books_can_be_found_in_a_new_folder(make_window, tmp_path):
    old = _three_ads(tmp_path / "old")
    first = make_window()
    _scanned(first, old)
    first.group_list.setCurrentRow(0)
    first.btn_keep.click()
    kept_gid = first.groups[0].gid
    first.close()
    new = tmp_path / "new"
    shutil.copytree(old, new, copy_function=shutil.copy2)  # keeps size and mtime
    shutil.rmtree(old)

    second = make_window()
    second.open_startup()
    assert pump_until(lambda: second._scan_worker is None)
    assert len(second._missing) == 3

    shown: list[str] = []
    original = QMessageBox.information
    QMessageBox.information = lambda *a, **k: shown.append(a[2])  # type: ignore[method-assign]
    try:
        second.relocate_missing(new)
    finally:
        QMessageBox.information = original  # type: ignore[method-assign]
    assert "Found 3 of the missing book(s)." in shown[0]
    assert not second._missing and not second.missing_banner.isVisibleTo(second)
    assert pump_until(lambda: second._scan_worker is None)
    assert {p.parent for p in second.archives} == {new.resolve()}
    assert all(a.cached for a in second.archives.values())  # adopted, not hashed again
    assert next(g for g in second.groups if g.gid == kept_gid).decision is Decision.KEEP


def test_books_that_are_one_issue_twice_get_a_banner(window, tmp_path):
    story = [make_page(seed=600 + i) for i in range(6)]
    write_archive(tmp_path / "lib" / "Issue 1.cbz", story)
    write_archive(tmp_path / "lib" / "Issue 1 (2).cbz", story)
    _scanned(window, tmp_path / "lib")

    assert window.duplicates_banner.isVisibleTo(window)
    assert "same issue twice" in window.duplicates_banner_text.text()
    assert all(g.decision is Decision.UNDECIDED for g in window.groups)
    window._dismiss_duplicates_banner()
    window.rebuild_groups()
    assert not window.duplicates_banner.isVisibleTo(window)


def test_scan_progress_shows_pages_hashed_and_from_cache(window, tmp_path):
    _scanned(window, _three_ads(tmp_path / "lib"))
    assert "36 pages hashed, 0 from cache" in window._scan_detail
    window._on_scan_progress(1, 3, "Book 0.cbz")
    assert "pages hashed" in window.status_label.text()
    scan_and_wait(window)
    assert window._scan_detail.startswith("0 pages hashed, 36 from cache")


# -- menus, context menus, notifications -----------------------------------


def test_menus_carry_the_actions_and_lists_have_accessible_names(window):
    assert set(window.menus) == {"file", "review", "view", "help"}
    review = window.menus["review"].actions()
    for action in (
        window.act_scan, window.act_apply, window.act_apply_selected, window.act_apply_known,
        window.act_undo, window.act_defer, window.act_next_warning, window.act_history,
        window.act_clean_backups, window.act_remembered,
    ):
        assert action in review
    assert window.act_about in window.menus["help"].actions()
    assert window.act_settings in window.menus["file"].actions()
    for widget in (
        window.archive_list, window.group_list, window.page_list, window.library_search,
        window.filter_combo,
    ):
        assert widget.accessibleName()


def test_the_group_menu_copies_ids(window, tmp_path, monkeypatch):
    _scanned(window, _three_ads(tmp_path / "lib"))
    _select_rows(window, 0, 1)
    chosen: list[QMenu] = []

    def pick(menu: QMenu, where) -> None:
        chosen.append(menu)
        next(a for a in menu.actions() if a.text().startswith("Copy 2 Group IDs")).trigger()

    monkeypatch.setattr(window, "_popup", pick)
    rect = window.group_list.visualItemRect(window.group_list.item(0))
    window._group_menu(rect.center())

    assert chosen
    copied = QApplication.clipboard().text().splitlines()
    assert copied == [window._shown_groups[0].gid, window._shown_groups[1].gid]
    assert window.group_list.item(0).data(ROLE_GID) == copied[0]


def test_a_finished_run_is_announced_when_the_window_is_behind(window, monkeypatch):
    from comiccleaner.gui.main_window import QSystemTrayIcon

    messages: list[str] = []
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    monkeypatch.setattr(
        QSystemTrayIcon, "showMessage", lambda self, title, text, *a: messages.append(text)
    )
    window._notify_finished("Removed 3 page(s) from 2 book(s).")
    assert messages == ["Removed 3 page(s) from 2 book(s)."]
    assert window._tray is not None
    window._drop_tray()
    assert window._tray is None

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    window._notify_finished("again")
    assert window._tray is None and messages == ["Removed 3 page(s) from 2 book(s)."]


# -- review packs ----------------------------------------------------------


def test_a_review_packs_settings_are_only_used_when_agreed(window, tmp_path, monkeypatch):
    pack = tmp_path / "pack.json"
    window.cache.remember("0000000000000abc", {0xABC}, note="shared advert")
    export_pack(window.cache, pack, {"threshold": 6, "min_archives": 1})
    window.cache.clear_known()
    asked: list[str] = []
    answer = [QMessageBox.StandardButton.No]
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: (asked.append(a[2]), answer[0])[1]
    )

    window.import_review_pack(pack)
    assert window.cache.known_hashes() == {0xABC}
    assert window.settings.threshold == 0
    assert "threshold: 0 -> 6" in asked[0] and "min_archives: 2 -> 1" in asked[0]

    answer[0] = QMessageBox.StandardButton.Yes
    window.import_review_pack(pack)
    assert (window.settings.threshold, window.settings.min_archives) == (6, 1)

    out = tmp_path / "out.json"
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.export_review_pack(out)
    assert json.loads(out.read_text(encoding="utf-8"))["settings"]["threshold"] == 6


# -- dialogs ---------------------------------------------------------------


def test_presets_fill_the_fields_and_editing_makes_it_custom(qapp):
    dialog = SettingsDialog(AppSettings())
    try:
        assert dialog.preset.currentData() == "strict"  # the defaults are that preset
        dialog.preset.setCurrentIndex(dialog.preset.findData("ads"))
        assert dialog.threshold.value() == 6 and dialog.preset.currentData() == "ads"
        dialog.min_pages.setValue(5)
        assert dialog.preset.currentData() == "custom"
        result = dialog.result_settings()
        assert result.preset == "custom" and result.threshold == 6 and result.min_pages == 5
    finally:
        dialog.close()


def test_the_edge_window_setting_reaches_grouping(qapp):
    settings = AppSettings(edge_pages=5)
    assert settings.grouping().edge_pages == 5


def test_high_contrast_is_black_on_white_or_white_on_black(qapp):
    labels = [mode.label for mode in Theme]
    assert "High contrast" in labels and "Follow system" in labels
    try:
        apply_theme(Theme.CONTRAST)
        palette = QApplication.palette()
        window, text = (palette.color(QPalette.ColorRole.Window),
                        palette.color(QPalette.ColorRole.WindowText))
        assert {window.name(), text.name()} == {"#ffffff", "#000000"}
        assert colour("delete").name() in ("#c0392b", "#ff7b6b")
    finally:
        apply_theme(Theme.LIGHT)


def test_thumbnail_size_can_change(window, tmp_path):
    _scanned(window, _three_ads(tmp_path / "lib"))
    window.group_list.setCurrentRow(0)
    page = window._shown_pages[0]
    thumbs = window.thumbs

    def longest_side() -> int:
        pixmap = thumbs.get(page)
        if pixmap is thumbs._placeholder:
            return 0  # still on its way
        return max(pixmap.width(), pixmap.height())

    assert pump_until(lambda: longest_side() == 180, 15)
    thumbs._cache.clear()
    thumbs.get(page)  # queued at 180, and answered only after the resize below

    thumbs.set_size(120)
    window._size_page_grid()
    assert window.page_list.iconSize().width() == 120
    assert pump_until(lambda: longest_side() == 120, 15)
    assert thumbs._placeholder.width() == 120


def test_the_preview_zooms_and_can_compare_any_copy(window, tmp_path):
    _scanned(window, _three_ads(tmp_path / "lib"))
    window.group_list.setCurrentRow(0)
    group = window._current_group()
    dialog = PagePreviewDialog(group, window._shown_pages, 0, window.thumbs, {}, window)
    try:
        assert pump_until(lambda: all(dialog._images.values()) and len(dialog._images) == 3, 15)
        assert not dialog.reference_pane.isVisibleTo(dialog)  # identical: nothing to compare
        view = dialog.copy_pane.view
        assert view.fitted

        dialog.actual_size()
        assert not view.fitted
        assert abs(view.scale() - 1 / view.devicePixelRatioF()) < 1e-9
        view.zoom_by(2.0)
        assert abs(view.scale() - 2 / view.devicePixelRatioF()) < 1e-9
        dialog.fit()
        assert view.fitted

        dialog.step(1)
        dialog.pin_current()
        assert dialog.reference is dialog.current_page()
        assert dialog.reference_pane.isVisibleTo(dialog)
        assert dialog.chk_diff.isVisibleTo(dialog)
        dialog.step(1)
        assert "0 bit(s) from the reference" in dialog.copy_pane.caption.text()
    finally:
        dialog.done(QDialog.DialogCode.Accepted)
    dialog._on_page_loaded("late", None, "")  # a late answer after closing is ignored


def test_remembered_pages_can_be_searched_and_described(window):
    window.cache.remember("0000000000000001", {1}, note="Group X credits")
    window.cache.remember("0000000000000002", {2}, note="shop advert")
    dialog = RememberedDialog(window.cache, window.thumbs, window)
    try:
        listing = dialog.known.listing
        dialog.search.setText("credits")
        visible = [listing.item(r) for r in range(listing.count())
                   if not listing.item(r).isHidden()]
        assert len(visible) == 1

        visible[0].setSelected(True)
        dialog.tags_edit.setText("credits, group x")
        dialog.save_description()
        [entry] = [e for e in window.cache.known_entries() if e.sid == "0000000000000001"]
        assert entry.tags == "credits, group x"

        dialog.search.setText("group x")
        assert sum(not listing.item(r).isHidden() for r in range(listing.count())) == 1
        dialog.search.setText("0000000000000002")
        assert sum(not listing.item(r).isHidden() for r in range(listing.count())) == 1
    finally:
        dialog.done(QDialog.DialogCode.Accepted)


# -- history and backups ---------------------------------------------------


def _one_run(window: MainWindow, tmp_path: Path, monkeypatch) -> Path:
    library = tmp_path / "lib"
    _scanned(window, _three_ads(library))
    window.settings.remember_junk = False
    window._set_all_decisions(Decision.DELETE)
    apply_and_wait(window, monkeypatch)
    return library


def test_a_pinned_run_keeps_its_backups(window, tmp_path, monkeypatch):
    library = _one_run(window, tmp_path, monkeypatch)
    stray = library / "Old.cbz.bak"
    stray.write_bytes(b"from long ago")
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))

    dialog = HistoryDialog(window.cache, window)
    try:
        dialog.tree.topLevelItem(0).setSelected(True)
        assert dialog.btn_pin.text() == "Pin"
        dialog.toggle_pin()
        assert "pinned" in dialog.tree.topLevelItem(0).text(0)
        assert dialog.btn_pin.text() == "Unpin"
        dialog.delete_backups()
        assert "Unpin it first" in told[-1]
    finally:
        dialog.done(QDialog.DialogCode.Accepted)

    texts: list[str] = []
    real_set_text = QMessageBox.setText
    monkeypatch.setattr(
        QMessageBox, "setText", lambda self, text: (texts.append(text), real_set_text(self, text))
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
    window.clean_up_backups()

    assert "4 file(s)" in texts[-1] and "3 belong to pinned runs" in texts[-1]
    assert not stray.exists()
    assert len(list(library.glob("*.cbz.bak"))) == 3
    assert "Skipped 3 backup(s) of pinned runs" in told[-1]


def test_one_runs_backups_can_be_deleted_from_history(window, tmp_path, monkeypatch):
    library = _one_run(window, tmp_path, monkeypatch)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dialog = HistoryDialog(window.cache, window)
    try:
        dialog.tree.topLevelItem(0).child(0).setSelected(True)  # a book stands for its run
        assert dialog.btn_delete_backups.isEnabled()
        dialog.delete_backups(confirm=False)
    finally:
        dialog.done(QDialog.DialogCode.Accepted)
    assert not list(library.glob("*.bak"))


def test_restoring_shows_the_first_page_of_the_backup(window, tmp_path, monkeypatch):
    _one_run(window, tmp_path, monkeypatch)
    boxes: list[QMessageBox] = []

    def refuse(self):
        boxes.append(self)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "exec", refuse)
    dialog = HistoryDialog(window.cache, window)
    try:
        dialog.tree.topLevelItem(0).setSelected(True)
        dialog.restore_selected()
        assert not dialog.restored  # said no
    finally:
        dialog.done(QDialog.DialogCode.Accepted)
    [box] = boxes
    assert not box.iconPixmap().isNull()
    assert "First page of" in box.informativeText()
