"""Full-size page preview, with a side-by-side compare against any copy."""

from __future__ import annotations

from functools import partial
from typing import cast

import numpy as np
from PIL import Image
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
    QShortcut,
    QWheelEvent,
)
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


# How far the wheel can zoom, as screen pixels per image pixel.
_MIN_ZOOM = 0.05
_MAX_ZOOM = 16.0
# Each wheel notch zooms by this factor.
_ZOOM_STEP = 1.25


class _ZoomView(QWidget):
    """An image that fits its pane until zoomed; the wheel zooms, dragging pans.

    Starts, and returns with fit(), fitted to the pane. actual_size() shows one
    image pixel per screen pixel, allowing for display scaling.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._pixmap: QPixmap | None = None
        self._text = "Loading..."
        self._fitted = True
        self._scale = 1.0  # widget pixels per image pixel, when not fitted
        self._origin = QPointF(0, 0)  # the image's top-left, when not fitted
        self._drag: QPointF | None = None

    @property
    def fitted(self) -> bool:
        return self._fitted

    def pixmap(self) -> QPixmap | None:
        return self._pixmap

    def text(self) -> str:
        return "" if self._pixmap is not None else self._text

    def set_pixmap(self, pixmap: QPixmap | None, text: str = "") -> None:
        resized = (
            pixmap is not None and self._pixmap is not None
            and pixmap.size() != self._pixmap.size()
        )
        self._pixmap = pixmap
        self._text = text
        if resized and not self._fitted:
            self._centre()  # another copy, at another size: keep the zoom, recentre
        self.update()

    def fit(self) -> None:
        self._fitted = True
        self.update()

    def actual_size(self) -> None:
        self._fitted = False
        self._scale = 1.0 / max(self.devicePixelRatioF(), 0.01)
        self._centre()
        self.update()

    def scale(self) -> float:
        return self._fit_scale() if self._fitted else self._scale

    def _fit_scale(self) -> float:
        if self._pixmap is None or self._pixmap.isNull():
            return 1.0
        size = self._pixmap.deviceIndependentSize()
        return min(self.width() / max(size.width(), 1), self.height() / max(size.height(), 1))

    def _centre(self) -> None:
        if self._pixmap is None:
            return
        size = self._pixmap.deviceIndependentSize() * self._scale
        self._origin = QPointF(
            (self.width() - size.width()) / 2, (self.height() - size.height()) / 2
        )

    def image_rect(self) -> QRectF:
        if self._pixmap is None:
            return QRectF()
        size = self._pixmap.deviceIndependentSize() * self.scale()
        if self._fitted:
            return QRectF(
                (self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                size.width(), size.height(),
            )
        return QRectF(self._origin, size)

    def zoom_by(self, factor: float, around: QPointF | None = None) -> None:
        """Zoom, keeping the image point under `around` (default: the middle) still."""
        if self._pixmap is None:
            return
        point = around if around is not None else QPointF(self.width() / 2, self.height() / 2)
        rect = self.image_rect()
        old = self.scale()
        new = max(_MIN_ZOOM, min(_MAX_ZOOM, old * factor))
        on_image = (point - rect.topLeft()) / old
        self._fitted = False
        self._scale = new
        self._origin = point - on_image * new
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        notches = event.angleDelta().y() / 120
        if notches:
            self.zoom_by(_ZOOM_STEP ** notches, event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.LeftButton and self._pixmap is not None:
            if self._fitted:  # start panning from where the image is now
                self._origin = self.image_rect().topLeft()
                self._scale = self.scale()
                self._fitted = False
            self._drag = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is not None:
            self._origin += event.position() - self._drag
            self._drag = event.position()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        if self._pixmap is None:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(self.image_rect(), self._pixmap, QRectF(self._pixmap.rect()))


class _ImagePane(QWidget):
    """A caption over an image that fits whatever room it is given until zoomed."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.caption = QLabel(caption)
        self.caption.setWordWrap(True)
        self.view = _ZoomView()
        layout.addWidget(self.caption)
        layout.addWidget(self.view, 1)

    def set_pixmap(self, pixmap: QPixmap | None, fallback: str = "") -> None:
        self.view.set_pixmap(pixmap, fallback)


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
        # Similar groups compare against the reference image from the start; any
        # group can compare against a copy pinned with "Compare with this".
        self._compare = group.kind is MatchKind.SIMILAR
        self._reference = group.representative
        # Once closed, answers still on their way are for nobody.
        self._closed = False
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
        self.btn_pin = QPushButton("Compare with this")
        self.btn_pin.setToolTip(
            "Make the copy on screen the reference the others are compared with (P)"
        )
        self.btn_pin.clicked.connect(self.pin_current)
        self.btn_fit = QPushButton("Fit")
        self.btn_fit.setToolTip("Fit the whole page in the window (F)")
        self.btn_fit.clicked.connect(self.fit)
        self.btn_actual = QPushButton("1:1")
        self.btn_actual.setToolTip(
            "One image pixel per screen pixel (1). The wheel zooms; drag to pan."
        )
        self.btn_actual.clicked.connect(self.actual_size)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_next)
        controls.addWidget(self.position)
        controls.addSpacing(16)
        controls.addWidget(self.chk_remove)
        controls.addWidget(self.chk_diff)
        controls.addWidget(self.btn_pin)
        controls.addStretch(1)
        controls.addWidget(self.btn_fit)
        controls.addWidget(self.btn_actual)
        controls.addWidget(close)
        layout.addLayout(controls)

        # Dialog-wide, so they work whichever control has focus, and the only
        # thing that handles these keys: see _LeaveKeysToDialog.
        for keys, handler in (
            (Qt.Key.Key_Left, lambda: self.step(-1)),
            (Qt.Key.Key_Right, lambda: self.step(1)),
            (Qt.Key.Key_Space, self.chk_remove.toggle),
            (Qt.Key.Key_H, self._toggle_diff),
            (Qt.Key.Key_P, self.pin_current),
            (Qt.Key.Key_F, self.fit),
            (Qt.Key.Key_1, self.actual_size),
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
        self._closed = True
        self._thumbs.page_loaded.disconnect(self._on_page_loaded)
        self._thumbs.task_done.disconnect(self._on_diff_done)

    # -- zoom and compare --------------------------------------------------
    def _views(self) -> list[_ZoomView]:
        panes = [self.copy_pane]
        if self._compare:
            panes.insert(0, self.reference_pane)
        return [pane.view for pane in panes]

    def fit(self) -> None:
        for view in self._views():
            view.fit()

    def actual_size(self) -> None:
        for view in self._views():
            view.actual_size()

    def _toggle_diff(self) -> None:
        if self._compare:
            self.chk_diff.toggle()

    def pin_current(self) -> None:
        """Compare every other copy with the one on screen."""
        page = self.current_page()
        self._reference = page
        self._diff = None
        self._diff_wanted = None
        if not self._compare:
            self._compare = True
            self.reference_pane.show()
            self.chk_diff.setVisible(True)
        self.reference_pane.caption.setText(
            f"<b>Reference</b> — {self._describe(page)}"
        )
        self._request(page)
        self._show_current()

    @property
    def reference(self) -> PageEntry:
        return self._reference

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
            bits = hamming(page.dhash, self._reference.dhash)
            caption += f", {bits} bit(s) from the reference"
        self._caption = caption
        self.position.setText(f"{self._index + 1} / {len(self._pages)}")
        self.chk_remove.blockSignals(True)
        self.chk_remove.setChecked(page.key not in self._group.kept)
        self.chk_remove.blockSignals(False)
        self._request(page)
        # The copies either side are next, so they load while this one is looked at.
        if len(self._pages) > 1:
            for delta in (1, -1):
                self._request(self._pages[(self._index + delta) % len(self._pages)])
        self._render()

    # -- images ------------------------------------------------------------
    def _request(self, page: PageEntry) -> None:
        key = ThumbnailCache.key_for(page)
        if key not in self._images:
            self._images[key] = None
            self._thumbs.request_page(page)

    def _on_page_loaded(self, key: str, image: object, error: str) -> None:
        if self._closed or key not in self._images:
            return  # someone else's request, or a late answer after closing
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
        if self._closed or key != self._diff_wanted:
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
