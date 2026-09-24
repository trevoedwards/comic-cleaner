"""Narrowing the review: by book, by kind of group, and marking only the safe ones."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from comiccleaner.core.model import Decision, MatchKind  # noqa: E402
from comiccleaner.gui.main_window import MainWindow  # noqa: E402

from .conftest import make_page, write_archive  # noqa: E402
from .test_gui import scan_and_wait  # noqa: E402

USER_ROLE = Qt.ItemDataRole.UserRole


@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(QSettings, "value", lambda self, key, default=None: default)
    monkeypatch.setattr(QSettings, "setValue", lambda self, key, value: None)
    monkeypatch.setattr(QSettings, "sync", lambda self: None)
    monkeypatch.setattr(
        "comiccleaner.gui.main_window.cache_path", lambda: tmp_path / "data" / "c.sqlite"
    )
    win = MainWindow()
    yield win
    win.close()


def _two_ad_library(root: Path) -> Path:
    """One advert in all three books (at the back), another in A and B only (mid-book)."""
    everywhere, some = make_page(seed=8100), make_page(seed=8200)
    for name in ("A", "B", "C"):
        story = [make_page(seed=ord(name) * 100 + i) for i in range(10)]
        if name != "C":
            story.insert(5, some)
        story.append(everywhere)
        write_archive(root / f"{name}.cbz", story)
    return root


def _select_books(window: MainWindow, *names: str) -> None:
    window.archive_list.clearSelection()
    for row in range(window.archive_list.count()):
        item = window.archive_list.item(row)
        if Path(item.data(USER_ROLE)).stem in names:
            item.setSelected(True)


def _scanned(window: MainWindow, root: Path) -> MainWindow:
    window.import_paths([_two_ad_library(root)])
    scan_and_wait(window)
    assert len(window.groups) == 2
    return window


# -- by book ---------------------------------------------------------------


def test_selecting_a_book_shows_only_its_groups(window, tmp_path):
    _scanned(window, tmp_path / "lib")

    _select_books(window, "C")

    assert [g.archive_count for g in window._shown_groups] == [3]
    assert window.group_list.count() == 1
    assert "1 of 2" in window.groups_title.text()
    assert "1 selected" in window.library_summary.text()
    assert window.btn_show_all.isVisibleTo(window)


def test_show_all_clears_every_filter(window, tmp_path):
    _scanned(window, tmp_path / "lib")
    _select_books(window, "C")
    window.filter_combo.setCurrentIndex(window.filter_combo.findData("similar"))

    window.show_all_groups()

    assert window.group_list.count() == 2
    assert window.archive_list.selectedItems() == []
    assert "Duplicate groups" in window.groups_title.text()
    assert not window.btn_show_all.isVisibleTo(window)


def test_the_book_selection_survives_a_regroup(window, tmp_path):
    _scanned(window, tmp_path / "lib")
    _select_books(window, "C")

    window.rebuild_groups()
    window._refresh_archive_list()

    assert window.group_list.count() == 1
    assert [Path(i.data(USER_ROLE)).stem for i in window.archive_list.selectedItems()] == ["C"]


def test_each_book_says_how_many_of_its_pages_repeat(window, tmp_path):
    _scanned(window, tmp_path / "lib")
    texts = {window.archive_list.item(r).text() for r in range(window.archive_list.count())}
    assert any(t.startswith("A.cbz") and "2 repeated" in t for t in texts)
    assert any(t.startswith("C.cbz") and "1 repeated" in t for t in texts)

    window._set_all_decisions(Decision.DELETE)

    texts = {window.archive_list.item(r).text() for r in range(window.archive_list.count())}
    assert any(t.startswith("A.cbz") and "2 to remove" in t for t in texts)


def test_the_search_box_hides_books_that_do_not_match(window, tmp_path):
    _scanned(window, tmp_path / "lib")

    window.library_search.setText("b.cbz")

    shown = [
        window.archive_list.item(r).text() for r in range(window.archive_list.count())
        if not window.archive_list.item(r).isHidden()
    ]
    assert len(shown) == 1 and shown[0].startswith("B.cbz")


# -- by kind ---------------------------------------------------------------


def test_the_undecided_view_drops_a_group_once_decided(window, tmp_path):
    _scanned(window, tmp_path / "lib")
    window.filter_combo.setCurrentIndex(window.filter_combo.findData("undecided"))
    window.group_list.setCurrentRow(0)
    first = window._current_group()

    window.btn_keep.click()

    assert first not in window._shown_groups
    assert window.group_list.count() == 1
    assert window._current_group() is not first


def test_the_warnings_view_shows_the_mid_book_match(window, tmp_path):
    _scanned(window, tmp_path / "lib")

    window.filter_combo.setCurrentIndex(window.filter_combo.findData("warnings"))

    [group] = window._shown_groups
    assert group.edge_share == 0


def test_mark_all_only_touches_what_is_shown(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window.filter_combo.setCurrentIndex(window.filter_combo.findData("identical"))
    assert window._shown_groups == []  # the only group is a similar one

    window._set_all_decisions(Decision.DELETE)

    assert window.groups[0].kind is MatchKind.SIMILAR
    assert window.groups[0].decision is Decision.UNDECIDED


# -- mark safe -------------------------------------------------------------


def test_mark_safe_marks_the_clean_match_and_leaves_the_doubtful_one(window, tmp_path):
    _scanned(window, tmp_path / "lib")

    window.btn_mark_safe.click()

    by_spread = {g.archive_count: g for g in window.groups}
    assert by_spread[3].decision is Decision.DELETE  # at the back of every book
    assert by_spread[2].decision is Decision.UNDECIDED  # mid-book: needs a look
    assert "Marked 1 safe group(s)" in window.status_label.text()
    assert "1 still need a look" in window.status_label.text()


def test_mark_safe_does_not_overrule_a_decision(window, tmp_path):
    _scanned(window, tmp_path / "lib")
    safe = next(g for g in window.groups if g.archive_count == 3)
    safe.decision = Decision.KEEP

    window.mark_safe_groups()

    assert safe.decision is Decision.KEEP


def test_mark_safe_respects_the_book_minimum(window, tmp_path):
    _scanned(window, tmp_path / "lib")
    window.settings.min_archives = 4

    window.mark_safe_groups()

    assert all(g.decision is Decision.UNDECIDED for g in window.groups)
