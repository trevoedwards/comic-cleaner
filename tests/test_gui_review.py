"""The review loop: keyboard triage, preview, ignore manager, sessions, cancelling."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from comiccleaner.core import remover  # noqa: E402
from comiccleaner.core.archive import is_page_name  # noqa: E402
from comiccleaner.core.model import Decision, MatchKind  # noqa: E402
from comiccleaner.gui import main_window  # noqa: E402
from comiccleaner.gui.main_window import ROLE_GID, MainWindow  # noqa: E402
from comiccleaner.gui.preview import (  # noqa: E402
    PagePreviewDialog,
    difference_image,
    page_distance,
)
from comiccleaner.gui.remembered import RememberedDialog  # noqa: E402
from comiccleaner.gui.session import (  # noqa: E402
    SESSION_FILE,
    SavedDecision,
    carry_decisions,
    load_session,
)

from .conftest import make_page, write_archive  # noqa: E402
from .test_gui import apply_and_wait, pump_until, scan_and_wait  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


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


@pytest.fixture
def window(make_window):
    return make_window()


def _three_ad_library(root: Path) -> Path:
    ads = [make_page(seed=9000 + i) for i in range(3)]
    for number in range(3):
        # Twelve pages, so the three adverts are exactly the quarter a book may lose.
        story = [make_page(seed=number * 100 + i) for i in range(9)]
        write_archive(root / f"Book {number}.cbz", [story[0], *ads, *story[1:]])
    return root


def _current_gid(window: MainWindow) -> str:
    return window.group_list.currentItem().data(ROLE_GID)


def _page_count(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(1 for n in zf.namelist() if is_page_name(n))


# -- keyboard triage -------------------------------------------------------


def _decision_shortcuts(window: MainWindow) -> list[QShortcut]:
    return [s for s in window.findChildren(QShortcut) if s.key().toString() in ("D", "K", "I")]


def test_decision_keys_are_single_letters_scoped_to_the_review(window):
    shortcuts = _decision_shortcuts(window)
    bound = {(s.key().toString(), s.parent()) for s in shortcuts}

    for key, button in window.decision_keys.items():
        assert button.shortcut().isEmpty()  # not a window-wide button mnemonic
        for widget in (window.group_list, window.page_list, button):
            assert (key, widget) in bound
    # Like Delete on the library: never window-wide.
    assert all(s.context() is Qt.ShortcutContext.WidgetShortcut for s in shortcuts)
    assert not any(s.parent() in (window, window.library_search) for s in shortcuts)


def _active(window: MainWindow) -> None:
    window.show()
    window.windowHandle().requestActivate()
    assert QTest.qWaitForWindowActive(window, 5000)


def test_pressing_d_marks_the_group_and_moves_on(window, tmp_path):
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    _active(window)
    window.group_list.setCurrentRow(0)
    window.group_list.setFocus()
    first = _current_gid(window)

    QTest.keyClick(window.group_list, Qt.Key.Key_D)

    assert pump_until(lambda: window.groups[0].decision is Decision.DELETE, 5)
    assert _current_gid(window) != first
    assert window.group_list.currentRow() == 1


def test_decision_keys_do_nothing_while_typing_or_choosing(window, tmp_path):
    """Typing "identical" in Filter books used to be able to mark, keep or ignore."""
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    _active(window)
    window.group_list.setCurrentRow(0)
    gids = {g.gid for g in window.groups}

    window.library_search.setFocus()
    QTest.keyClicks(window.library_search, "dki")
    assert window.library_search.text() == "dki"
    window.library_search.clear()
    for combo in (window.sort_combo, window.filter_combo):
        combo.setFocus()
        for key in (Qt.Key.Key_D, Qt.Key.Key_K, Qt.Key.Key_I):
            QTest.keyClick(combo, key)
    QCoreApplication.processEvents()

    assert {g.gid for g in window.groups} == gids  # nothing ignored
    assert all(g.decision is Decision.UNDECIDED for g in window.groups)

    # ...while the review lists still take them.
    window.filter_combo.setCurrentIndex(0)
    window.group_list.setCurrentRow(0)
    window.group_list.setFocus()
    QTest.keyClick(window.group_list, Qt.Key.Key_K)
    assert pump_until(lambda: window.groups[0].decision is Decision.KEEP, 5)
    window.group_list.setCurrentRow(1)
    window.page_list.setFocus()
    QTest.keyClick(window.page_list, Qt.Key.Key_D)
    assert pump_until(lambda: window.groups[1].decision is Decision.DELETE, 5)


def test_deciding_skips_groups_that_already_have_a_decision(window, tmp_path):
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    window.groups[1].decision = Decision.KEEP
    window.group_list.setCurrentRow(0)

    window.btn_delete.click()

    assert window.group_list.currentRow() == 2


def test_deciding_wraps_round_and_says_when_everything_is_done(window, tmp_path):
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    window.groups[0].decision = Decision.KEEP
    window.group_list.setCurrentRow(2)

    window.btn_keep.click()  # row 2 -> wraps round to row 1, the only one left
    assert window.group_list.currentRow() == 1

    window.btn_delete.click()  # nothing left undecided: stay put and say so
    assert window.group_list.currentRow() == 1
    assert "Every group has a decision" in window.status_label.text()


def test_delete_key_drops_selected_books_from_the_library(window, tmp_path):
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    window.show()
    window.archive_list.setCurrentRow(0)
    window.archive_list.setFocus()
    QCoreApplication.processEvents()

    QTest.keyClick(window.archive_list, Qt.Key.Key_Delete)

    assert len(window.archives) == 2


# -- preview ---------------------------------------------------------------


def test_similar_copies_list_the_furthest_first(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()
    group = window._current_group()
    assert group.kind is MatchKind.SIMILAR

    distances = [page_distance(p, group) for p in window._shown_pages]
    assert distances == sorted(distances, reverse=True)
    assert distances[0] > 0  # the re-encoded copy leads
    text = window.page_list.item(0).text()
    assert "of 5" in text
    assert "bit(s) from reference" in text


def test_preview_loads_both_images_and_edits_the_group(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()
    group = window._current_group()

    dialog = PagePreviewDialog(
        group, window._shown_pages, 0, window.thumbs,
        {p: a.page_count for p, a in window.archives.items()}, window,
    )
    try:
        assert pump_until(
            lambda: len(dialog._images) == 2 and all(dialog._images.values()), 15
        ), "preview images never arrived"
        assert dialog.reference_pane.isVisibleTo(dialog)

        if dialog.current_page() is dialog._reference:
            dialog.step(1)  # the reference is never diffed against itself
        dialog.chk_diff.setChecked(True)
        assert pump_until(
            lambda: "of pixels differ" in dialog.copy_pane.caption.text(), 15
        ), "the difference highlight never arrived"

        page = dialog.current_page()
        dialog.chk_remove.setChecked(False)
        assert page.key in group.kept
        dialog.chk_remove.setChecked(True)
        assert page.key not in group.kept

        dialog.step(len(window._shown_pages))  # a full lap lands back where it began
        assert dialog.current_page() is page
    finally:
        dialog.done(QDialog.DialogCode.Accepted)


def _open_preview(window: MainWindow) -> PagePreviewDialog:
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()
    dialog = PagePreviewDialog(
        window._current_group(), window._shown_pages, 0, window.thumbs, {}, window
    )
    dialog.show()
    dialog.windowHandle().requestActivate()
    assert QTest.qWaitForWindowActive(dialog, 5000)
    return dialog


def test_one_press_of_space_or_h_toggles_exactly_once(window, library):
    """Space went to both the dialog's shortcut and a focused checkbox."""
    window.import_paths([library])
    window.settings.threshold = 8  # similar, so the difference toggle is shown
    scan_and_wait(window)
    dialog = _open_preview(window)
    group = window._current_group()
    try:
        for target in (dialog, dialog.chk_remove, dialog.chk_diff, dialog.btn_next):
            # Shortcuts only fire in the active window, which offscreen Qt can drop.
            dialog.activateWindow()
            assert QTest.qWaitForWindowActive(dialog, 5000)
            target.setFocus()
            QCoreApplication.processEvents()
            assert QApplication.focusWidget() is target
            page = dialog.current_page()
            removing, diff = page.key not in group.kept, dialog.chk_diff.isChecked()

            QTest.keyClick(target, Qt.Key.Key_Space)
            assert (page.key not in group.kept) is not removing, target
            assert dialog.chk_remove.isChecked() is not removing
            QTest.keyClick(target, Qt.Key.Key_H)
            assert dialog.chk_diff.isChecked() is not diff, target

            before = dialog._index
            QTest.keyClick(target, Qt.Key.Key_Right)
            assert dialog._index == (before + 1) % len(dialog._pages)
            QTest.keyClick(target, Qt.Key.Key_Left)
            assert dialog._index == before

        # The double toggle: the platform hands Space to the focused box *and* the
        # dialog's shortcut fires. The box must leave the key to the shortcut.
        [space] = [s for s in dialog.findChildren(QShortcut) if s.key().toString() == "Space"]
        dialog.chk_remove.setFocus()
        checked = dialog.chk_remove.isChecked()
        space.setEnabled(False)  # so the key reaches the box itself
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            event = QKeyEvent(kind, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ")
            QApplication.sendEvent(dialog.chk_remove, event)
        space.setEnabled(True)
        space.activated.emit()
        assert dialog.chk_remove.isChecked() is not checked  # once, not twice
    finally:
        dialog.done(QDialog.DialogCode.Accepted)


def test_exact_groups_preview_without_a_reference_pane(window, library):
    window.import_paths([library])
    scan_and_wait(window)  # threshold 0: only the byte-identical pair groups
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()
    group = window._current_group()
    assert group.kind is MatchKind.EXACT

    dialog = PagePreviewDialog(group, window._shown_pages, 0, window.thumbs, {}, window)
    try:
        assert not dialog.reference_pane.isVisibleTo(dialog)
        assert not dialog.chk_diff.isVisibleTo(dialog)
    finally:
        dialog.done(QDialog.DialogCode.Accepted)


def test_difference_image_finds_nothing_in_an_identical_copy():
    img = Image.open(io.BytesIO(make_page(seed=1))).convert("RGB")
    _, changed = difference_image(img, img.copy())
    assert changed == 0.0

    other = Image.open(io.BytesIO(make_page(seed=2))).convert("RGB")
    _, changed = difference_image(img, other.resize((200, 300)))
    assert changed > 0.2


def test_difference_image_paints_what_changed_red_on_a_faded_copy():
    reference = Image.new("RGB", (4, 4), (255, 255, 255))
    copy = reference.copy()
    copy.putpixel((1, 2), (0, 0, 0))

    diff, changed = difference_image(reference, copy)

    assert changed == 1 / 16
    assert diff.getpixel((1, 2)) == (220, 30, 30)
    assert {diff.getpixel((x, y)) for x in range(4) for y in range(4) if (x, y) != (1, 2)} == {
        (255, 255, 255)
    }


def _preview_ready(window: MainWindow) -> PagePreviewDialog:
    """A preview of a similar group, both images loaded, on a copy that is not the reference."""
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()
    dialog = PagePreviewDialog(
        window._current_group(), window._shown_pages, 0, window.thumbs, {}, window
    )
    if dialog.current_page() is dialog._reference:
        dialog.step(1)
    assert pump_until(
        lambda: len(dialog._images) >= 2 and all(dialog._images.values()), 15
    ), "preview images never arrived"
    return dialog


def test_the_highlight_is_worked_out_off_the_ui_thread(window, library, monkeypatch):
    from comiccleaner.gui import preview

    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    computed: list[int] = []
    real_difference = preview.difference_image

    def counting(reference, copy):
        computed.append(1)
        return real_difference(reference, copy)

    monkeypatch.setattr(preview, "difference_image", counting)
    dialog = _preview_ready(window)
    queued: list[tuple] = []
    monkeypatch.setattr(window.thumbs, "run_task", lambda key, fn: queued.append((key, fn)))
    try:
        dialog.chk_diff.setChecked(True)

        # _render asked for it and moved on: nothing was worked out in the dialog.
        assert computed == []
        assert len(queued) == 1
        assert "comparing" in dialog.copy_pane.caption.text()

        key, fn = queued[0]
        dialog._on_diff_done(key, fn(), "")
        assert computed == [1]
        assert "of pixels differ" in dialog.copy_pane.caption.text()

        # An answer for a page the user has left is dropped, not shown.
        dialog.chk_diff.setChecked(False)
        dialog.chk_diff.setChecked(True)  # cached: no new work
        assert len(queued) == 1
        dialog.step(1)
        if dialog.current_page() is dialog._reference:
            dialog.step(1)
        # Asked for once this copy's image has loaded.
        assert pump_until(lambda: len(queued) == 2, 15), "no highlight was asked for"
        stale_key, stale_fn = queued[1]
        dialog.step(1)
        dialog._on_diff_done(stale_key, stale_fn(), "")
        assert dialog._diff is not None and dialog._diff[0] == key
    finally:
        dialog.done(QDialog.DialogCode.Accepted)


# -- ignore manager --------------------------------------------------------


def test_an_ignored_group_can_be_brought_back(window, tmp_path):
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    window.group_list.setCurrentRow(0)
    hidden = _current_gid(window)
    window._ignore_current()
    assert hidden not in {g.gid for g in window.groups}
    assert "Remembered Pages" in window.status_label.text()

    dialog = RememberedDialog(window.cache, window.thumbs, window, tab="ignored")
    try:
        assert dialog.tabs.currentWidget() is dialog.ignored
        listing = dialog.ignored.listing
        assert listing.count() == 1
        assert listing.item(0).text().startswith("3 copies in 3 book(s)")
        listing.item(0).setSelected(True)
        dialog.restore_selected()
        assert dialog.changed
        assert listing.count() == 0
    finally:
        dialog.done(QDialog.DialogCode.Accepted)

    window.rebuild_groups()
    assert hidden in {g.gid for g in window.groups}


def test_ignoring_at_a_loose_threshold_holds_when_it_is_tightened(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window.group_list.setCurrentRow(0)
    window._ignore_current()

    window.settings.threshold = 0
    window.rebuild_groups()

    # Previously only the piece holding the group's lowest hash stayed hidden.
    assert window.groups == []


# -- sessions --------------------------------------------------------------


def test_library_and_decisions_come_back_next_launch(make_window, tmp_path):
    library = _three_ad_library(tmp_path / "lib")
    first = make_window()
    first.import_paths([library])
    scan_and_wait(first)
    marked = first.groups[0]
    marked.decision = Decision.DELETE
    spared = marked.pages[0].key
    marked.kept.add(spared)
    first.close()

    second = make_window()
    second.open_startup()
    assert pump_until(lambda: second._scan_worker is None), "restore scan did not finish"

    assert set(second.archives) == set(first.archives)
    restored = next(g for g in second.groups if g.gid == marked.gid)
    assert restored.decision is Decision.DELETE
    assert restored.kept == {spared}
    assert second.act_apply.isEnabled()


def test_a_saved_decision_follows_its_pages_when_the_group_id_changes(make_window, tmp_path):
    """A lower similar hash joining a cluster renames it; the decision must follow."""
    library = _three_ad_library(tmp_path / "lib")
    first = make_window()
    first.import_paths([library])
    scan_and_wait(first)
    marked = first.groups[0]
    marked.decision = Decision.DELETE
    spared = marked.pages[0].key
    marked.kept.add(spared)
    first.close()

    # As if the cluster now went by another id: a new, lower hash joined it.
    raw = json.loads(first._session_file.read_text(encoding="utf-8"))
    raw["decisions"]["0" * 16] = raw["decisions"].pop(marked.gid)
    first._session_file.write_text(json.dumps(raw), encoding="utf-8")

    second = make_window()
    second.open_startup()
    assert pump_until(lambda: second._scan_worker is None), "restore scan did not finish"

    restored = next(g for g in second.groups if g.gid == marked.gid)
    assert restored.decision is Decision.DELETE
    assert restored.kept == {spared}
    assert second._restored == {}  # applied, so no longer carried around
    second.save_session()
    saved = load_session(second._session_file)
    assert set(saved.decisions) == {marked.gid}


def test_a_decision_survives_a_similar_group_changing_id_mid_session(window, library):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    [group] = window.groups
    group.decision = Decision.DELETE
    spared = next(p for p in group.pages if "Book 01" in p.label).key
    group.kept.add(spared)
    # Book 03's re-encoded advert now hashes one bit below the old lowest hash.
    lowest = int(group.gid, 16)
    lower = lowest ^ (lowest & -lowest)
    page = next(p for p in window.archives[library.resolve() / "Book 03.cbz"].pages
                if p.dhash == max(q.dhash for q in group.pages if "Book 03" in q.label))
    page.dhash = lower

    window.rebuild_groups()

    [regrouped] = window.groups
    assert regrouped.gid != group.gid
    assert regrouped.decision is Decision.DELETE
    assert regrouped.kept == {spared}


def _saved(decision: Decision, *pages: tuple[str, str]) -> SavedDecision:
    return SavedDecision(decision, set(), set(pages))


def _group(gid: str, *keys: tuple[str, str]):
    from comiccleaner.core.model import DuplicateGroup, PageEntry

    pages = [
        PageEntry(archive=Path(a), name=n, index=1, size=1, width=1, height=1,
                  content_sha="x", dhash=int(gid, 16))
        for a, n in keys
    ]
    return DuplicateGroup(gid=gid, kind=MatchKind.SIMILAR, pages=pages)


def test_carrying_decisions_by_page_overlap():
    a, b, c, d = ("A", "1"), ("B", "1"), ("C", "1"), ("D", "1")
    new = [_group("01", a, b, c), _group("02", d)]

    # Most of the old group's pages are in "01": the decision follows them.
    carried, used = carry_decisions({"99": _saved(Decision.DELETE, a, b)}, new)
    assert carried == {"01": _saved(Decision.DELETE, a, b)} and used == {"99"}

    # Only a minority overlaps: not enough to go on.
    carried, used = carry_decisions({"99": _saved(Decision.KEEP, a, ("X", "1"), ("Y", "1"))}, new)
    assert carried == {} and used == set()

    # Two old groups merged into one: ambiguous, so it stays undecided.
    prior = {"98": _saved(Decision.DELETE, a, b), "99": _saved(Decision.KEEP, c)}
    carried, used = carry_decisions(prior, new)
    assert carried == {} and used == {"98", "99"}

    # A matching id wins over anything that merely overlaps it.
    prior = {"01": _saved(Decision.KEEP, a), "99": _saved(Decision.DELETE, b, c)}
    carried, used = carry_decisions(prior, new)
    assert carried == {"01": prior["01"]} and used == {"01", "99"}


def test_saved_decisions_that_match_nothing_are_dropped(make_window, tmp_path):
    library = _three_ad_library(tmp_path / "lib")
    first = make_window()
    first.import_paths([library])
    scan_and_wait(first)
    first.groups[0].decision = Decision.DELETE
    first.close()
    raw = json.loads(first._session_file.read_text(encoding="utf-8"))
    book = str(next(iter(first.archives)))
    raw["decisions"]["f" * 16] = {  # a group that no longer forms anywhere
        "decision": "keep", "kept": [], "pages": [[book, "gone.jpg"], [book, "x.jpg"]],
    }
    first._session_file.write_text(json.dumps(raw), encoding="utf-8")

    second = make_window()
    second.open_startup()
    assert pump_until(lambda: second._scan_worker is None)

    assert second._restored == {}
    second.save_session()
    assert "f" * 16 not in load_session(second._session_file).decisions


def test_restoring_skips_books_that_have_gone(make_window, tmp_path):
    library = _three_ad_library(tmp_path / "lib")
    first = make_window()
    first.import_paths([library])
    first.close()
    (library / "Book 0.cbz").unlink()

    second = make_window()
    second.open_startup()
    assert pump_until(lambda: second._scan_worker is None)

    assert len(second.archives) == 2
    # Still on screen once the scan's own progress messages are done.
    assert "1 archive(s) from last time could not be found" in second.status_label.text()


def test_startup_paths_are_added_to_the_restored_library(make_window, tmp_path):
    first = make_window()
    first.import_paths([_three_ad_library(tmp_path / "old")])
    first.close()
    extra = write_archive(tmp_path / "new" / "Extra.cbz", [make_page(seed=1)])

    second = make_window()
    second.open_startup([extra])
    assert pump_until(lambda: second._scan_worker is None)

    assert len(second.archives) == 4
    assert extra.resolve() in second.archives


def test_nothing_is_restored_when_the_setting_is_off(make_window, tmp_path):
    first = make_window()
    first.import_paths([_three_ad_library(tmp_path / "lib")])
    first.close()

    second = make_window()
    second.settings.restore_session = False
    second.open_startup()

    assert second.archives == {}
    assert second._scan_worker is None


def test_a_damaged_session_file_is_ignored(make_window, tmp_path):
    window = make_window()
    session_file = window._session_file
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{not json", encoding="utf-8")

    window.open_startup()

    assert window.archives == {}
    assert load_session(session_file) is None


def test_session_is_written_beside_the_hash_cache(window, tmp_path):
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    window.save_session()

    saved = json.loads(window._session_file.read_text(encoding="utf-8"))
    assert window._session_file.name == SESSION_FILE
    assert window._session_file.parent == window.cache.db_path.parent
    assert len(saved["archives"]) == 3


# -- after a removal -------------------------------------------------------


def test_only_the_rewritten_books_are_rescanned(window, tmp_path, monkeypatch):
    library = _three_ad_library(tmp_path / "lib")
    bystander = write_archive(tmp_path / "other" / "Unrelated.cbz", [make_page(seed=5)])
    window.import_paths([library, bystander])
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    scanned: list[list[Path]] = []
    real_run = window._run_scan
    monkeypatch.setattr(window, "_run_scan", lambda paths: (scanned.append(paths), real_run(paths)))
    apply_and_wait(window, monkeypatch)

    assert len(scanned) == 1
    assert sorted(p.name for p in scanned[0]) == ["Book 0.cbz", "Book 1.cbz", "Book 2.cbz"]
    assert all(window.archives[p].page_count == 9 for p in scanned[0])


def test_a_dry_run_is_not_followed_by_a_rescan(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    apply_and_wait(window, monkeypatch, dry_run=True)

    assert window._pending_rescan == []
    assert window._scan_worker is None


def test_a_removal_can_be_cancelled_between_books(window, library, monkeypatch):
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)

    entered, gate = threading.Event(), threading.Event()
    real_apply = remover.apply_plan

    def slow_apply(plan, **kwargs):
        entered.set()
        gate.wait(20)
        return real_apply(plan, **kwargs)

    shown: list[str] = []
    real_set_text = QMessageBox.setText
    monkeypatch.setattr(remover, "apply_plan", slow_apply)
    monkeypatch.setattr(
        "comiccleaner.gui.main_window._ConfirmDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr("comiccleaner.gui.main_window._ConfirmDialog.dry_run", lambda self: False)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(
        QMessageBox, "setText", lambda self, text: (shown.append(text), real_set_text(self, text))
    )

    window.apply_removals()
    try:
        assert pump_until(entered.is_set, 10), "removal never started"
        # Usable even though the review panels are locked.
        assert window.btn_cancel.isVisibleTo(window) and window.btn_cancel.isEnabled()
        window.cancel_work()
        assert not window.btn_cancel.isEnabled()
    finally:
        gate.set()
    assert pump_until(lambda: window._removal_worker is None), "removal did not finish"
    assert pump_until(lambda: window._scan_worker is None), "rescan did not finish"

    counts = sorted(_page_count(library / f"Book 0{n}.cbz") for n in (1, 2, 3))
    assert counts == [4, 5, 5]  # the book in progress finished; the rest are untouched
    assert any("Cancelled: 2 archive(s)" in text for text in shown)
    assert not window.btn_cancel.isVisibleTo(window)


# -- quitting while busy ---------------------------------------------------


def _gated_removal(window: MainWindow, monkeypatch):
    entered, gate = threading.Event(), threading.Event()
    real_apply = remover.apply_plan

    def slow_apply(plan, **kwargs):
        entered.set()
        gate.wait(20)
        return real_apply(plan, **kwargs)

    monkeypatch.setattr(remover, "apply_plan", slow_apply)
    monkeypatch.setattr(
        "comiccleaner.gui.main_window._ConfirmDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr("comiccleaner.gui.main_window._ConfirmDialog.dry_run", lambda self: False)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)
    return entered, gate


def test_closing_mid_removal_finishes_the_book_before_shutting_the_cache(
    window, library, monkeypatch
):
    monkeypatch.setattr(main_window, "CLOSE_WAIT_MS", 50)
    window.import_paths([library])
    window.settings.threshold = 8
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    window.show()
    entered, gate = _gated_removal(window, monkeypatch)
    shut: list[str] = []
    real_shutdown = window.thumbs.shutdown
    monkeypatch.setattr(window.thumbs, "shutdown", lambda: (shut.append("thumbs"), real_shutdown()))

    window.apply_removals()
    try:
        assert pump_until(entered.is_set, 10), "removal never started"
        assert window.close() is False  # refused: the removal is mid-book
        assert window.isVisible()
        assert shut == []
        window.cache.conn.execute("SELECT 1")  # still open for the worker
    finally:
        gate.set()

    # The removal runs to completion, then the window closes by itself.
    assert pump_until(lambda: not window.isVisible(), 30), "window never closed"
    assert shut == ["thumbs"]
    # The book in progress was finished whole; closing cancelled the rest.
    counts = sorted(_page_count(library / f"Book 0{n}.cbz") for n in (1, 2, 3))
    assert counts == [4, 5, 5]
    assert not list(library.glob(".comiccleaner-*"))  # no half-written temp file
    with pytest.raises(sqlite3.ProgrammingError):
        window.cache.conn.execute("SELECT 1")


def test_closing_mid_scan_waits_for_the_books_in_flight(window, tmp_path, monkeypatch):
    monkeypatch.setattr(main_window, "CLOSE_WAIT_MS", 50)
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    window.show()
    entered, gate = threading.Event(), threading.Event()
    from comiccleaner.core import scanner

    real_scan = scanner.scan_archive

    def slow_scan(path, cache=None):
        entered.set()
        gate.wait(20)
        return real_scan(path, cache)  # writes to the cache once it is done

    monkeypatch.setattr(scanner, "scan_archive", slow_scan)
    failures: list[str] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: failures.append(a[2]))

    window.start_scan()
    try:
        assert pump_until(entered.is_set, 10), "scan never started"
        assert window.close() is False
        window.cache.conn.execute("SELECT 1")
    finally:
        gate.set()

    assert pump_until(lambda: not window.isVisible(), 30), "window never closed"
    assert failures == []


def test_a_worker_failure_while_closing_opens_no_dialog(window, monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))

    window._closing = True
    window._on_worker_failed("database is closed")

    assert shown == []


# -- thumbnails ------------------------------------------------------------


def test_thumbnails_arrive_as_real_pixmaps(window, library):
    from comiccleaner.gui.thumbs import THUMB_SIZE

    window.import_paths([library])
    scan_and_wait(window)
    page = window.archives[next(iter(window.archives))].pages[0]
    arrived: dict[str, object] = {}
    window.thumbs.ready.connect(lambda key, pixmap: arrived.setdefault(key, pixmap))

    window.thumbs.get(page)
    key = window.thumbs.key_for(page)

    assert pump_until(lambda: key in arrived, 15), "thumbnail never arrived"
    pixmap = arrived[key]
    assert not pixmap.isNull()
    assert max(pixmap.width(), pixmap.height()) == THUMB_SIZE
    assert window.thumbs.get(page) is not window.thumbs._placeholder  # now cached


def test_a_thumbnail_that_cannot_be_made_falls_back_to_the_placeholder(window, library):
    import dataclasses

    window.import_paths([library])
    scan_and_wait(window)
    real = window.archives[next(iter(window.archives))].pages[0]
    missing = dataclasses.replace(real, name="no-such-page.jpg")
    arrived: dict[str, object] = {}
    window.thumbs.ready.connect(lambda key, pixmap: arrived.setdefault(key, pixmap))

    window.thumbs.get(missing)
    key = window.thumbs.key_for(missing)

    assert pump_until(lambda: key in arrived, 15)
    # A signal hands over a fresh Python wrapper, so compare the Qt pixmap itself.
    assert arrived[key].cacheKey() == window.thumbs._placeholder.cacheKey()
    assert key not in window.thumbs._pending


# -- Space and Enter from the review columns ---------------------------------


def _review_ready(window: MainWindow, tmp_path: Path, monkeypatch) -> list:
    """A scanned library with the first group open; returns previews opened."""
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    _active(window)
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()
    opened: list = []
    monkeypatch.setattr(main_window.PagePreviewDialog, "exec", lambda self: opened.append(self))
    return opened


def _focus(widget) -> None:
    widget.setFocus()
    QCoreApplication.processEvents()
    assert QApplication.focusWidget() is widget


def test_space_ticks_the_selected_copy_exactly_once_from_the_review(
    window, tmp_path, monkeypatch
):
    _review_ready(window, tmp_path, monkeypatch)
    group = window._current_group()
    for target in (window.group_list, window.page_list, window.btn_keep):
        window.page_list.setCurrentRow(0)
        page = window._shown_pages[0]
        removing = page.key not in group.kept
        _focus(target)

        QTest.keyClick(target, Qt.Key.Key_Space)
        QCoreApplication.processEvents()

        # Toggled once: a second handler would have put it straight back.
        assert (page.key not in group.kept) is not removing, target
        ticked = window.page_list.item(0).checkState() is Qt.CheckState.Checked
        assert ticked is not removing, target
    assert all(g.decision is Decision.UNDECIDED for g in window.groups)  # Keep not clicked


def test_enter_opens_the_selected_copy_from_the_review(window, tmp_path, monkeypatch):
    opened = _review_ready(window, tmp_path, monkeypatch)
    for target in (window.group_list, window.btn_delete):
        _focus(target)
        QTest.keyClick(target, Qt.Key.Key_Return)
    QCoreApplication.processEvents()

    assert len(opened) == 2
    assert all(g.decision is Decision.UNDECIDED for g in window.groups)  # Delete not clicked


def test_filter_books_keeps_space_and_enter(window, tmp_path, monkeypatch):
    opened = _review_ready(window, tmp_path, monkeypatch)
    group = window._current_group()
    kept = set(group.kept)
    _focus(window.library_search)

    QTest.keyClicks(window.library_search, "a b")
    QTest.keyClick(window.library_search, Qt.Key.Key_Return)
    QCoreApplication.processEvents()

    assert window.library_search.text() == "a b"
    assert group.kept == kept
    assert opened == []


# -- the toolbar follows one job state ----------------------------------------


def _toolbar(window: MainWindow) -> dict[str, bool]:
    return {
        name: getattr(window, f"act_{name}").isEnabled()
        for name in ("scan", "apply", "history", "remembered", "add_files", "settings")
    }


def test_toolbar_follows_the_job_state(window, tmp_path, monkeypatch):
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    assert all(_toolbar(window).values())
    assert window.act_scan.text() == "Scan"

    monkeypatch.setattr(main_window, "HistoryDialog", None)  # must never be opened
    monkeypatch.setattr(main_window, "RememberedDialog", None)
    window._scan_worker = object()  # stands in for a scan in flight
    try:
        window._job_changed("Scanning")
        state = _toolbar(window)
        assert state["scan"] and window.act_scan.text() == "Cancel Scan"
        assert not any(state[a] for a in ("apply", "history", "remembered", "add_files"))
        window._set_all_decisions(Decision.DELETE)  # marking mid-scan
        assert not window.act_apply.isEnabled()
        window.show_history()
        window.manage_remembered()
    finally:
        window._scan_worker = None

    window._removal_worker = object()
    try:
        window._job_changed("Removing")
        assert not any(_toolbar(window).values())
        assert not window.centralWidget().isEnabled()
        window.show_history()
        window.manage_remembered()
    finally:
        window._removal_worker = None

    window._job_changed()
    assert all(_toolbar(window).values())
    assert window.centralWidget().isEnabled()
    window._set_all_decisions(Decision.UNDECIDED)
    assert not window.act_apply.isEnabled()


# -- small things ----------------------------------------------------------------


def test_importing_books_already_in_the_library_says_so(window, tmp_path):
    library = _three_ad_library(tmp_path / "lib")
    window.import_paths([library])
    assert window.status_label.text().startswith("Imported 3 new archive(s)")

    window.import_paths([library])
    assert window.status_label.text() == (
        "Already in the library: 3 archive(s). Nothing new to import."
    )

    write_archive(library / "Book 9.cbz", [make_page(seed=1)])
    window.import_paths([library])
    assert window.status_label.text().startswith("Imported 1 new archive(s)")


def test_unreadable_books_use_the_themes_delete_colour(window, tmp_path, monkeypatch):
    from comiccleaner.gui.theme import Theme, apply_theme, colour

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    (tmp_path / "Broken.cbz").write_bytes(b"not a zip")
    window.import_paths([tmp_path / "Broken.cbz"])
    scan_and_wait(window)
    try:
        for theme, expected in ((Theme.DARK, "#ff7b6b"), (Theme.LIGHT, "#c0392b")):
            apply_theme(theme)
            window._restyle()
            item = window.archive_list.item(0)
            assert item.foreground().color() == colour("delete"), theme
            assert colour("delete").name() == expected
    finally:
        apply_theme(window.settings.theme_mode())


def test_confirm_warns_when_backups_go_to_another_drive(window, tmp_path, monkeypatch):
    import dataclasses

    from comiccleaner.core.remover import RemovalPlan

    plans = [RemovalPlan(archive=tmp_path / "a.cbz", remove_names={"p"}, original_pages=4)]
    settings = dataclasses.replace(
        window.settings, backup_enabled=True, backup_dir=str(tmp_path / "bak"), output_dir=""
    )
    for same, shown in ((False, True), (True, False)):
        monkeypatch.setattr(main_window, "on_same_volume", lambda a, b, same=same: same)
        dialog = main_window._ConfirmDialog(plans, [], settings, window)
        try:
            assert (dialog.volume_note is not None) is shown
            if shown:
                assert "complete extra copy" in dialog.volume_note.text()
        finally:
            dialog.deleteLater()


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_a_removal_short_of_space_changes_nothing_and_says_so(
    window, tmp_path, monkeypatch, dry_run
):
    library = _three_ad_library(tmp_path / "lib")
    window.import_paths([library])
    scan_and_wait(window)
    window._set_all_decisions(Decision.DELETE)
    before = {p.name: p.read_bytes() for p in library.iterdir()}
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _title, text: warned.append(text))
    monkeypatch.setattr(
        remover.shutil, "disk_usage", lambda _p: type("Usage", (), {"free": 10})()
    )

    apply_and_wait(window, monkeypatch, dry_run=dry_run)

    assert len(warned) == 1 and "Not enough free space" in warned[0]
    assert "Nothing was changed" in warned[0]
    assert {p.name: p.read_bytes() for p in library.iterdir()} == before
    assert all(info.pages for info in window.archives.values())  # nothing to rescan


# -- startup: missing books and interrupted runs ---------------------------------


def test_missing_books_stay_listed_until_dismissed(make_window, tmp_path):
    library = _three_ad_library(tmp_path / "lib")
    first = make_window()
    first.import_paths([library])
    first.close()
    (library / "Book 0.cbz").unlink()

    second = make_window()
    second.open_startup()
    assert pump_until(lambda: second._scan_worker is None)

    assert len(second.archives) == 2
    assert second.missing_banner.isVisibleTo(second)
    assert "Book 0.cbz" in second.missing_banner_text.text()
    assert str(library / "Book 0.cbz") in second.missing_banner.toolTip()
    second.status_label.setText("later messages")
    assert second.missing_banner.isVisibleTo(second)

    [dismiss] = second.missing_banner.findChildren(main_window.QPushButton)
    dismiss.click()
    assert not second.missing_banner.isVisibleTo(second)


def test_startup_puts_back_a_book_an_interrupted_removal_left_as_a_backup(
    make_window, tmp_path, monkeypatch
):
    library = _three_ad_library(tmp_path / "lib")
    first = make_window()
    first.import_paths([library])
    first.close()
    book = library / "Book 0.cbz"
    backup = library / "Book 0.cbz.bak"
    original = book.read_bytes()
    book.rename(backup)  # killed after the move aside, before the swap
    remover._write_marker(book, book, backup, library / ".comiccleaner-x.cbz")
    monkeypatch.setattr(remover, "_process_alive", lambda _pid: False)

    second = make_window()
    second.open_startup()
    assert pump_until(lambda: second._scan_worker is None)

    assert book.read_bytes() == original
    assert not backup.exists()
    assert len(second.archives) == 3
    assert not second.missing_banner.isVisibleTo(second)
    assert "Put back 1 book(s)" in second.status_label.text()


def test_a_late_thumbnail_does_not_undo_a_preview_untick(window, tmp_path):
    """The grid's stale tick used to be read back into the group with each new icon."""
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    window.group_list.setCurrentRow(0)
    QCoreApplication.processEvents()
    group = window._current_group()
    item = window.page_list.item(0)
    assert item.checkState() is Qt.CheckState.Checked
    page = window._shown_pages[0]

    group.kept.add(page.key)  # unticked in the preview; the grid is refreshed on close
    window._on_thumb_ready(item.data(main_window.ROLE_THUMB_KEY), window.thumbs._placeholder)

    assert page.key in group.kept
