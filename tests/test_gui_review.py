"""The review loop: keyboard triage, preview, ignore manager, sessions, cancelling."""

from __future__ import annotations

import io
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QCoreApplication, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from comiccleaner.core import remover  # noqa: E402
from comiccleaner.core.archive import is_page_name  # noqa: E402
from comiccleaner.core.model import Decision, MatchKind  # noqa: E402
from comiccleaner.gui.main_window import ROLE_GID, MainWindow  # noqa: E402
from comiccleaner.gui.preview import (  # noqa: E402
    PagePreviewDialog,
    difference_image,
    page_distance,
)
from comiccleaner.gui.remembered import RememberedDialog  # noqa: E402
from comiccleaner.gui.session import SESSION_FILE, load_session  # noqa: E402

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
        story = [make_page(seed=number * 100 + i) for i in range(3)]
        write_archive(root / f"Book {number}.cbz", [story[0], *ads, *story[1:]])
    return root


def _current_gid(window: MainWindow) -> str:
    return window.group_list.currentItem().data(ROLE_GID)


def _page_count(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(1 for n in zf.namelist() if is_page_name(n))


# -- keyboard triage -------------------------------------------------------


def test_decision_keys_are_single_letters(window):
    assert window.btn_delete.shortcut().toString() == "D"
    assert window.btn_keep.shortcut().toString() == "K"
    assert window.btn_ignore.shortcut().toString() == "I"


def test_pressing_d_marks_the_group_and_moves_on(window, tmp_path):
    window.import_paths([_three_ad_library(tmp_path / "lib")])
    scan_and_wait(window)
    window.show()
    window.windowHandle().requestActivate()
    assert QTest.qWaitForWindowActive(window, 5000)
    window.group_list.setCurrentRow(0)
    first = _current_gid(window)

    QTest.keyClick(window, Qt.Key.Key_D)

    assert pump_until(lambda: window.groups[0].decision is Decision.DELETE, 5)
    assert _current_gid(window) != first
    assert window.group_list.currentRow() == 1


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

        dialog.chk_diff.setChecked(True)
        assert "of pixels differ" in dialog.copy_pane.caption.text()

        page = dialog.current_page()
        dialog.chk_remove.setChecked(False)
        assert page.key in group.kept
        dialog.chk_remove.setChecked(True)
        assert page.key not in group.kept

        dialog.step(len(window._shown_pages))  # a full lap lands back where it began
        assert dialog.current_page() is page
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
    assert all(window.archives[p].page_count == 3 for p in scanned[0])


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
