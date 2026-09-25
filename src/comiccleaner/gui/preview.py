"""Full-size page preview, with a side-by-side compare for similar groups."""

from __future__ import annotations

from functools import partial
from typing import cast

import numpy as np
from PIL import Image
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QImage, QKeyEvent, QKeySequence, QPixmap, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.hashing import hamming
from ..core.model import DuplicateGroup, MatchKind, PageEntry
from .thumbs import ThumbnailCache

# Pixels whose grey levels differ by less than this are JPEG noise, not a change.
_DIFF_NOISE = 16
# ...and from here on a difference is painted at full strength.
_DIFF_FULL = 80


def page_distance(page: PageEntry, group: DuplicateGroup) -> int:
    """Bits between a copy's perceptual hash and the group's reference image."""
    return hamming(page.dhash, group.representative.dhash)


def pil_to_pixmap(image: Image.Image) -> QPixmap:
    rgb = image.convert("RGB")
    data = rgb.tobytes()
    qimage = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    # copy() detaches the QImage from `data`, which Python is about to free.
    return QPixmap.fromImage(qimage.copy())


def difference_image(reference: Image.Image, copy: Image.Image) -> tuple[Image.Image, float]:
    """The copy, faded, with whatever differs from the reference painted red.

    Returns the image and the share of pixels that differ. The copy is resized to
    the reference first, since a rescale is exactly what a similar page has had.
    """
    size = reference.size
    ref = np.asarray(reference.convert("L"), dtype=np.float32)
    cur_img = copy.convert("L")
    if cur_img.size != size:
        cur_img = cur_img.resize(size, Image.Resampling.BILINEAR)
    cur = np.asarray(cur_img, dtype=np.float32)

    diff = np.abs(ref - cur)
    strength = np.clip((diff - _DIFF_NOISE) / (_DIFF_FULL - _DIFF_NOISE), 0.0, 1.0)[..., None]
    faded = (170 + cur * (85 / 255))[..., None].repeat(3, axis=2)
    red = np.array([220, 30, 30], dtype=np.float32)
    out = faded * (1 - strength) + red * strength
    changed = float((diff > _DIFF_NOISE).mean())
    return Image.fromarray(out.astype(np.uint8), "RGB"), changed


class _ImagePane(QWidget):
    """A caption over an image that scales to fit whatever room it is given."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.caption = QLabel(caption)
        self.caption.setWordWrap(True)
        self.view = QLabel("Loading...")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(200, 200)
        self.view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.caption)
        layout.addWidget(self.view, 1)
        self._pixmap: QPixmap | None = None

    def set_pixmap(self, pixmap: QPixmap | None, fallback: str = "") -> None:
        self._pixmap = pixmap
        if pixmap is None:
            self.view.setPixmap(QPixmap())
            self.view.setText(fallback)
        self._rescale()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        self.view.setPixmap(
            self._pixmap.scaled(
                self.view.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class _LeaveKeysToDialog(QObject):
    """Stops a checkbox acting on keys the dialog's own shortcuts handle.

    A focused checkbox toggles itself on Space. With the dialog's Space shortcut
    toggling it too, one press could flip it twice, changing nothing, depending
    on how the platform routes the key. Here the checkbox never claims these
    keys, so the dialog's shortcut is the one thing that acts on them.
    """

    _EVENTS = (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease)

    def __init__(self, keys: tuple[Qt.Key, ...], parent: QObject) -> None:
        super().__init__(parent)
        self._keys = set(keys)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in self._EVENTS and cast(QKeyEvent, event).key() in self._keys:
            event.ignore()  # not claimed, so the shortcut still gets the key
            return True
        return False


class PagePreviewDialog(QDialog):
    """Step through a group's copies at full size.

    The remove checkbox edits the group directly, so the grid behind it only needs
    refreshing once the dialog closes.
    """

    def __init__(
        self,
        group: DuplicateGroup,
        pages: list[PageEntry],
        start: int,
        thumbs: ThumbnailCache,
        page_counts: dict,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Page preview")
        self.resize(1100, 820)
        self._group = group
        self._pages = pages
        self._index = max(0, min(start, len(pages) - 1))
        self._thumbs = thumbs
        self._page_counts = page_counts
        self._images: dict[str, Image.Image | None] = {}
        self._errors: dict[str, str] = {}
        self._pixmaps: dict[str, QPixmap] = {}
        self._caption = ""
        self._compare = group.kind is MatchKind.SIMILAR
        self._reference = group.representative
        # The difference image is worked out on the thumbnail thread, not here: at
        # full size it takes long enough to stall the window. The one asked for
        # last, and the last one that came back (key, image or None, share changed).
        self._diff_wanted: str | None = None
        self._diff: tuple[str, QPixmap | None, float] | None = None

        layout = QVBoxLayout(self)
        panes = QHBoxLayout()
        self.reference_pane = _ImagePane(
            f"<b>Reference</b> — {self._describe(self._reference)}"
        )
        self.copy_pane = _ImagePane("")
        if self._compare:
            panes.addWidget(self.reference_pane, 1)
        else:
            self.reference_pane.hide()
        panes.addWidget(self.copy_pane, 1)
        layout.addLayout(panes, 1)

        controls = QHBoxLayout()
        self.btn_prev = QPushButton("< Previous")
        self.btn_prev.setToolTip("Previous copy (Left)")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next = QPushButton("Next >")
        self.btn_next.setToolTip("Next copy (Right)")
        self.btn_next.clicked.connect(lambda: self.step(1))
        self.position = QLabel()
        self.chk_remove = QCheckBox("Remove this copy")
        self.chk_remove.setToolTip("Space")
        self.chk_remove.toggled.connect(self._on_remove_toggled)
        self.chk_diff = QCheckBox("Highlight differences")
        self.chk_diff.setToolTip("Paint what differs from the reference in red (H)")
        self.chk_diff.toggled.connect(self._show_current)
        self.chk_diff.setVisible(self._compare)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_next)
        controls.addWidget(self.position)
        controls.addSpacing(16)
        controls.addWidget(self.chk_remove)
        controls.addWidget(self.chk_diff)
        controls.addStretch(1)
        controls.addWidget(close)
        layout.addLayout(controls)

        # Dialog-wide, so they work whichever control has focus, and the only
        # thing that handles these keys: see _LeaveKeysToDialog.
        for keys, handler in (
            (Qt.Key.Key_Left, lambda: self.step(-1)),
            (Qt.Key.Key_Right, lambda: self.step(1)),
            (Qt.Key.Key_Space, self.chk_remove.toggle),
            (Qt.Key.Key_H, self.chk_diff.toggle),
        ):
            QShortcut(QKeySequence(keys), self, handler)
        self._key_guard = _LeaveKeysToDialog((Qt.Key.Key_Space, Qt.Key.Key_H), self)
        for box in (self.chk_remove, self.chk_diff):
            box.installEventFilter(self._key_guard)

        thumbs.page_loaded.connect(self._on_page_loaded)
        thumbs.task_done.connect(self._on_diff_done)
        self.finished.connect(self._disconnect)
        if self._compare:
            self._request(self._reference)
        self._show_current()

    def _disconnect(self) -> None:
        self._thumbs.page_loaded.disconnect(self._on_page_loaded)
        self._thumbs.task_done.disconnect(self._on_diff_done)

    # -- navigation --------------------------------------------------------
    def current_page(self) -> PageEntry:
        return self._pages[self._index]

    def step(self, delta: int) -> None:
        self._index = (self._index + delta) % len(self._pages)
        self._show_current()

    def _describe(self, page: PageEntry) -> str:
        total = self._page_counts.get(page.archive, 0)
        where = f"page {page.index + 1} of {total}" if total else f"page {page.index + 1}"
        return f"{page.archive.name}, {where}, {page.width}x{page.height}"

    def _show_current(self) -> None:
        page = self.current_page()
        caption = f"<b>This copy</b> — {self._describe(page)}"
        if self._compare and page is self._reference:
            caption += " (this is the reference)"
        elif self._compare:
            caption += f", {page_distance(page, self._group)} bit(s) from the reference"
        self._caption = caption
        self.position.setText(f"{self._index + 1} / {len(self._pages)}")
        self.chk_remove.blockSignals(True)
        self.chk_remove.setChecked(page.key not in self._group.kept)
        self.chk_remove.blockSignals(False)
        self._request(page)
        self._render()

    # -- images ------------------------------------------------------------
    def _request(self, page: PageEntry) -> None:
        key = ThumbnailCache.key_for(page)
        if key not in self._images:
            self._images[key] = None
            self._thumbs.request_page(page)

    def _on_page_loaded(self, key: str, image: object, error: str) -> None:
        if key not in self._images:
            return  # someone else's request
        self._images[key] = image if isinstance(image, Image.Image) else None
        if error:
            self._errors[key] = error
        self._render()

    def _image(self, page: PageEntry) -> tuple[Image.Image | None, str]:
        key = ThumbnailCache.key_for(page)
        if key in self._errors:
            return None, f"Could not load this page:\n{self._errors[key]}"
        return self._images.get(key), "Loading..."

    def _pixmap(self, page: PageEntry, image: Image.Image) -> QPixmap:
        key = ThumbnailCache.key_for(page)
        pixmap = self._pixmaps.get(key)
        if pixmap is None:
            pixmap = self._pixmaps[key] = pil_to_pixmap(image)
        return pixmap

    def _render(self) -> None:
        page = self.current_page()
        current, current_note = self._image(page)
        reference, reference_note = self._image(self._reference)
        if self._compare:
            self.reference_pane.set_pixmap(
                self._pixmap(self._reference, reference) if reference else None,
                reference_note,
            )
        self.copy_pane.caption.setText(self._caption)
        if current is None:
            self.copy_pane.set_pixmap(None, current_note)
            return
        # Diffing the reference against itself would only ever report 0%.
        show_diff = self._compare and self.chk_diff.isChecked() and page is not self._reference
        if show_diff and reference is not None:
            self._render_diff(page, reference, current)
        else:
            self.copy_pane.set_pixmap(self._pixmap(page, current))

    def _diff_key(self, page: PageEntry) -> str:
        reference = ThumbnailCache.key_for(self._reference)
        return f"diff|{reference}|{ThumbnailCache.key_for(page)}"

    def _render_diff(self, page: PageEntry, reference: Image.Image, current: Image.Image) -> None:
        key = self._diff_key(page)
        if self._diff is not None and self._diff[0] == key:
            _, pixmap, changed = self._diff
            if pixmap is None:
                self.copy_pane.caption.setText(f"{self._caption} — could not compare")
                self.copy_pane.set_pixmap(self._pixmap(page, current))
            else:
                self.copy_pane.caption.setText(
                    f"{self._caption} — {changed:.1%} of pixels differ"
                )
                self.copy_pane.set_pixmap(pixmap)
            return
        # The plain copy until the highlight is ready.
        self.copy_pane.caption.setText(f"{self._caption} — comparing...")
        self.copy_pane.set_pixmap(self._pixmap(page, current))
        if self._diff_wanted != key:
            self._diff_wanted = key
            self._thumbs.run_task(key, partial(difference_image, reference, current))

    def _on_diff_done(self, key: str, result: object, error: str) -> None:
        if key != self._diff_wanted:
            return  # someone else's task, or a page the user has since left
        self._diff_wanted = None
        page = self.current_page()
        showing = self._compare and self.chk_diff.isChecked() and page is not self._reference
        if not showing or key != self._diff_key(page):
            return  # the highlight was turned off, or this page left, meanwhile
        if error or not isinstance(result, tuple):
            self._diff = (key, None, 0.0)
        else:
            diff, changed = result
            # Made here, on the UI thread: pixmaps may not be made anywhere else.
            self._diff = (key, pil_to_pixmap(diff), changed)
        self._render()

    # -- decisions ---------------------------------------------------------
    def _on_remove_toggled(self, checked: bool) -> None:
        key = self.current_page().key
        if checked:
            self._group.kept.discard(key)
        else:
            self._group.kept.add(key)
