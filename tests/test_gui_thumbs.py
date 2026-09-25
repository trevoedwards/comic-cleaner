"""The thumbnail thread: one job per page, and nothing left behind by a job."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from comiccleaner.core.model import PageEntry  # noqa: E402
from comiccleaner.gui import thumbs  # noqa: E402
from comiccleaner.gui.thumbs import ThumbnailCache  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def cache(qapp, monkeypatch):
    """A cache whose jobs are recorded, never run."""
    cache = ThumbnailCache()
    started: list[object] = []
    monkeypatch.setattr(cache._threads, "start", started.append)
    cache.started = started
    yield cache
    cache._threads.clear()
    cache.deleteLater()


def _page(name: str = "p1.jpg") -> PageEntry:
    return PageEntry(
        archive=Path("book.cbz"), name=name, index=0, size=1, width=1, height=1,
        content_sha="x", dhash=0, flat=False,
    )


def _flush_deletes() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _signal_objects(cache: ThumbnailCache) -> list:
    kinds = (thumbs._ThumbSignals, thumbs._PageSignals, thumbs._TaskSignals)
    return [child for child in cache.children() if isinstance(child, kinds)]


def test_a_page_already_queued_is_not_queued_again(cache):
    page = _page()

    cache.request_page(page)
    cache.request_page(page)
    assert len(cache.started) == 1

    cache._on_page_loaded(cache.key_for(page), None, "")
    cache.request_page(page)
    assert len(cache.started) == 2


def test_every_listener_hears_the_one_answer(cache):
    heard: list[str] = []
    cache.page_loaded.connect(lambda key, _image, _error: heard.append(key))
    page = _page()

    cache.request_page(page)
    cache.request_page(page)
    cache._on_page_loaded(cache.key_for(page), None, "")

    assert heard == [cache.key_for(page)]


def test_job_signals_belong_to_the_cache_and_go_when_answered(cache):
    thumb, full = _page("a.jpg"), _page("b.jpg")

    cache.get(thumb)
    cache.request_page(full)
    cache.run_task("diff", lambda: 1)
    assert len(_signal_objects(cache)) == 3

    cache._on_failed(cache.key_for(thumb), "unreadable")
    cache._on_page_loaded(cache.key_for(full), None, "")
    cache._on_task_done("diff", 1, "")
    _flush_deletes()

    assert _signal_objects(cache) == []


def test_releasing_archives_disposes_of_jobs_that_will_never_run(cache):
    cache.get(_page("a.jpg"))
    cache.request_page(_page("b.jpg"))
    assert len(_signal_objects(cache)) == 2

    cache.release_archives()
    _flush_deletes()

    assert _signal_objects(cache) == []
    cache.request_page(_page("b.jpg"))  # no longer counted as queued
    assert len(cache.started) == 3


def test_a_task_runs_on_the_thumbnail_thread_and_reports_back(qapp):
    cache = ThumbnailCache()
    answers: list[tuple] = []
    cache.task_done.connect(lambda key, result, error: answers.append((key, result, error)))

    cache.run_task("sum", lambda: 1 + 2)
    cache.run_task("boom", lambda: 1 / 0)
    cache._threads.waitForDone(5000)
    for _ in range(50):
        QCoreApplication.processEvents()
        if len(answers) == 2:
            break

    assert answers[0] == ("sum", 3, "")
    assert answers[1][0] == "boom" and answers[1][1] is None and "division" in answers[1][2]
    cache.shutdown()
