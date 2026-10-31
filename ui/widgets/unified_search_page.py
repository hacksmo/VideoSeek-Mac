"""Unified search page: one query box over video moments and photos.

Design notes
------------
* The page owns no search logic: ``src.services.unified_search`` runs both
  indexes and returns a plain dict. That keeps the widget testable and lets the
  service be reused by the CLI later.
* Both sides draw through a ``QAbstractListModel`` + delegate rather than
  ``QListWidget`` items. Icon-mode ``QListWidget`` ignores ``setGridSize`` here
  (Qt 6 sizes every item from the text), which collapses a cell to one 30 px
  line and clips the thumbnail; a delegate's ``sizeHint`` is authoritative.
* Both sides load off the GUI thread. Search is one ``QThread``; thumbnails are
  a second, queue-fed thread, because a 12-hit strip is twelve keyframe/ffmpeg
  reads and the frame cache is shared with the search page.
* Video hits keep the Agent API's timecodes so double-clicking can hand the clip
  straight to the player; photo hits reuse the photo library's grid delegate, so
  the two pages cannot drift apart.
"""

from __future__ import annotations

import queue
import weakref

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QRect,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
)

from ui.widgets.button_roles import set_button_role
from ui import file_actions
from ui.widgets.image_library_page import ImageGridDelegate, ImageGridModel, _draw_cover
from ui.widgets.scaffold import PageScaffold, VSCard

# Video strip cells. The thumbnail is 16:9, the same shape the search page shows
# a hit in, so a strip frame and a result frame are directly comparable.
VIDEO_THUMB_WIDTH = 188
VIDEO_THUMB_HEIGHT = 106
VIDEO_CELL_WIDTH = 196
VIDEO_CELL_HEIGHT = 160

HIT_ROLE = Qt.ItemDataRole.UserRole + 1
THUMB_ROLE = Qt.ItemDataRole.UserRole + 2

_STRINGS = {
    "zh": {
        "title": "统一搜索",
        "subtitle": "一个框同时查视频片段和图片。视频命中双击就跳到播放器定位；图片双击用系统看图打开。",
        "card": "搜索",
        "placeholder": "描述画面，或写属性词：浅色衣服 / 肚子胖的人 / 穿得花哨的人",
        "scope_all": "全部",
        "scope_video": "只搜视频",
        "scope_image": "只搜图片",
        "search": "搜索",
        "searching": "检索中…",
        "idle": "输入一句话再搜。视频按画面语义找片段；图片支持属性词（颜色 / 体型 / 风格）和关键词。",
        "video_section": "视频片段",
        "image_section": "图片",
        "video_count": "{count} 条",
        "image_count": "{count} 张",
        "no_videos": "视频没有命中。",
        "no_images": "图片没有命中。",
        "skipped_video": "这次只查了图片，没有搜视频。",
        "skipped_image": "这次只查了视频，没有搜图片。",
        "video_idle": "还没有搜索",
        "image_idle": "还没有搜索",
        "video_error_index": "视频索引还没建好：先去「视频库」建库或同步，再回来搜。",
        "video_error": "视频检索失败：{error}",
        "image_error": "图片检索失败：{error}",
        "status": "视频 {videos} 条 · 图片 {images} 张 · 用时 {elapsed}",
        "status_attr": "属性：{attrs}",
        "status_vector_only": "图片是按向量近似的，没有文字匹配",
        "video_tip": "双击：在搜索页播放这一段",
        "image_tip": "双击：用系统看图打开",
        "drag_hint": "把图片直接拖到剪映等应用即可导入；右键：在访达中显示 / 复制路径。",
        "menu_reveal": "在访达中显示",
        "menu_copy_path": "复制文件路径",
        "menu_open": "用默认应用打开",
        "menu_missing": "原文件当前不可达（硬盘没挂载？）",
        "scope_hint": "「全部」两侧都查；只查一侧时会跳过另一侧的索引，快一些。",
    },
    "en": {
        "title": "Unified search",
        "subtitle": "One box over video moments and photos. Double-click a video hit to play it; double-click a photo to open it.",
        "card": "Search",
        "placeholder": "Describe the shot, or use attribute words: light clothes / heavy build / flashy outfit",
        "scope_all": "Everything",
        "scope_video": "Videos only",
        "scope_image": "Photos only",
        "search": "Search",
        "searching": "Searching…",
        "idle": "Type a query first. Videos are matched by picture content; photos also take attribute words (colour / build / style) and keywords.",
        "video_section": "Video moments",
        "image_section": "Photos",
        "video_count": "{count} hits",
        "image_count": "{count} photos",
        "no_videos": "No video hits.",
        "no_images": "No photo hits.",
        "skipped_video": "Only photos were searched.",
        "skipped_image": "Only videos were searched.",
        "video_idle": "Nothing searched yet",
        "image_idle": "Nothing searched yet",
        "video_error_index": "The video index is not built yet. Sync it on the Video library page first.",
        "video_error": "Video search failed: {error}",
        "image_error": "Photo search failed: {error}",
        "status": "{videos} video hits · {images} photos · {elapsed}",
        "status_attr": "Attributes: {attrs}",
        "status_vector_only": "photos are vector approximations, no literal match",
        "video_tip": "Double-click to play this moment",
        "image_tip": "Double-click to open with the system viewer",
        "drag_hint": "Drag a photo straight into another app to import it; right-click to reveal or copy its path.",
        "menu_reveal": "Reveal in Finder",
        "menu_copy_path": "Copy file path",
        "menu_open": "Open with default app",
        "menu_missing": "The source file is unreachable right now (external drive unmounted?)",
        "scope_hint": "Everything searches both sides; a single side skips the other index and is faster.",
    },
}


def _format_elapsed(ms: int) -> str:
    value = max(0, int(ms))
    if value < 1000:
        return f"{value} ms"
    return f"{value / 1000:.1f} s"


class VideoHitModel(QAbstractListModel):
    """Holds video hits and the decoded frame thumbnails."""

    # Dragging a video hit hands over the source file; there is no way to drag a
    # time range, so playing from here stays the precise action.
    DRAG_FIELD = "video_path"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hits: list[dict] = []
        self._thumbs: dict[int, QPixmap] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._hits)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._hits)):
            return None
        hit = self._hits[index.row()]
        if role == HIT_ROLE:
            return hit
        if role == THUMB_ROLE:
            return self._thumbs.get(index.row())
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbs.get(index.row())
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{hit.get('file_name', '')}\n{hit.get('start_timecode', '')}"
        return None

    def mimeData(self, indexes):  # noqa: N802 - Qt API
        from ui.file_actions import file_mime_data

        paths = []
        for index in indexes or ():
            if index.isValid() and 0 <= index.row() < len(self._hits):
                paths.append(self._hits[index.row()].get(self.DRAG_FIELD) or "")
        return file_mime_data(paths)

    def mimeTypes(self) -> list[str]:  # noqa: N802 - Qt API
        return ["text/uri-list", "text/plain"]

    def supportedDragActions(self) -> Qt.DropAction:  # noqa: N802 - Qt API
        return Qt.DropAction.CopyAction

    def set_hits(self, hits) -> None:
        self.beginResetModel()
        self._hits = list(hits or [])
        self._thumbs = {}
        self.endResetModel()

    def hit_at(self, row: int) -> dict:
        if 0 <= row < len(self._hits):
            return self._hits[row]
        return {}

    def has_thumb(self, row: int) -> bool:
        return row in self._thumbs

    def set_thumb(self, row: int, pixmap: QPixmap) -> None:
        if not (0 <= row < len(self._hits)) or pixmap is None or pixmap.isNull():
            return
        self._thumbs[row] = pixmap
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [THUMB_ROLE])


class VideoHitDelegate(QStyledItemDelegate):
    """Strip cell: 16:9 frame, the matched subtitle (or file name), timecode + score."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        # Weak: page -> view -> delegate -> page would be a Qt/Python cycle that
        # crashes on collection (Qt deletes the child, then Python deletes again).
        self._page_ref = weakref.ref(page)
        self._colors: dict = {}
        self._colors_dark: bool | None = None

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt API
        return QSize(VIDEO_CELL_WIDTH, VIDEO_CELL_HEIGHT)

    def _theme(self) -> dict:
        from PySide6.QtWidgets import QApplication

        from ui.widgets.styles import theme_color_map

        app = QApplication.instance()
        dark = bool(app.property("videoseek_is_dark")) if app is not None else True
        if self._colors_dark is not dark or not self._colors:
            self._colors = theme_color_map(dark)
            self._colors_dark = dark
        return self._colors

    def paint(self, painter: QPainter, option, index):  # noqa: N802 - Qt API
        hit = index.data(HIT_ROLE) or {}
        rect = option.rect.adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        colors = self._theme()

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        radius = 8.0
        card = QPainterPath()
        card.addRoundedRect(rect.toRectF(), radius, radius)
        base = QColor(str(colors.get("PANEL") or "#2e2e2e"))
        if hovered:
            base = base.lighter(115) if base.lightness() < 128 else base.darker(104)
        painter.fillPath(card, base)

        thumb_rect = QRect(rect.left(), rect.top(), rect.width(), VIDEO_THUMB_HEIGHT)
        thumb_clip = QPainterPath()
        thumb_clip.addRoundedRect(thumb_rect.toRectF(), radius, radius)
        thumb_clip.addRect(
            QRect(
                thumb_rect.left(),
                thumb_rect.bottom() - int(radius),
                thumb_rect.width(),
                int(radius),
            )
        )
        painter.save()
        painter.setClipPath(thumb_clip, Qt.ClipOperation.IntersectClip)
        pixmap = index.data(THUMB_ROLE)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            _draw_cover(painter, thumb_rect, pixmap)
        else:
            painter.fillRect(thumb_rect, QColor(str(colors.get("FIELD") or "#242424")))
            painter.setPen(QColor(str(colors.get("MUTED") or "#a3a3a3")))
            painter.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter, "…")
            page = self._page_ref()
            if page is not None:
                page.ensure_video_thumb(index.row())
        painter.restore()

        if selected:
            pen = painter.pen()
            pen.setColor(QColor(str(colors.get("ACCENT") or "#60a5fa")))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(card)

        text_color = QColor(str(colors.get("TEXT") or "#f5f5f5"))
        muted = QColor(str(colors.get("MUTED") or "#a3a3a3"))
        metrics = painter.fontMetrics()

        title = str(hit.get("matched_text") or hit.get("file_name") or "").strip()
        title_rect = QRect(rect.left() + 8, thumb_rect.bottom() + 6, rect.width() - 16, 16)
        painter.setPen(text_color)
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )

        meta_bits = [
            str(hit.get("start_timecode") or ""),
            f"{float(hit.get('score') or 0.0):.2f}",
        ]
        meta_rect = QRect(rect.left() + 8, title_rect.bottom(), rect.width() - 16, 14)
        font = painter.font()
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
        painter.setFont(font)
        painter.setPen(muted)
        painter.drawText(
            meta_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(
                " · ".join(bit for bit in meta_bits if bit),
                Qt.TextElideMode.ElideRight,
                meta_rect.width(),
            ),
        )
        painter.restore()


class _SearchWorker(QThread):
    """Runs ``unified_search.search`` off the GUI thread."""

    done = Signal(dict)

    def __init__(self, query: str, scope: str, *, page=None):
        super().__init__(page)
        self._query = str(query or "")
        self._scope = str(scope or "all")

    def run(self):
        from src.services import unified_search

        try:
            payload = unified_search.search(self._query, scope=self._scope)
        except Exception as exc:  # noqa: BLE001 - surface, never crash the thread
            payload = {
                "query": self._query,
                "scope": self._scope,
                "videos": [],
                "images": [],
                "video_error": "",
                "image_error": str(exc) or exc.__class__.__name__,
                "image_channel": "",
                "image_attributes": "",
                "elapsed_ms": 0,
            }
        self.done.emit(payload)


class _VideoThumbWorker(QThread):
    """Queue-fed frame decoder, reusing the search page's frame cache.

    A queue rather than one thread per hit: twelve parallel ``ffmpeg``/keyframe
    reads compete for the same disk and the same decoder, and the delegate may
    ask for a row long after the strip was filled.
    """

    ready = Signal(int, int, object)  # (generation, row, QImage | None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs: queue.Queue = queue.Queue()
        self._stopping = False

    def enqueue(self, generation: int, row: int, path: str, start: float, end: float) -> None:
        if not self._stopping:
            self._jobs.put((int(generation), int(row), str(path), float(start), float(end)))

    def stop(self) -> None:
        self._stopping = True
        self.requestInterruption()
        self._jobs.put(None)

    def run(self):
        from src.media.thumbnail import preview_max_drift_sec, preview_sample_time
        from src.utils import get_single_thumbnail
        from ui.thumb_cache import get_thumb_cache, result_thumb_key

        cache = get_thumb_cache()
        while not self._stopping:
            job = self._jobs.get()
            if job is None or self.isInterruptionRequested():
                break
            generation, row, path, start, end = job
            key = result_thumb_key(path, start, end, VIDEO_THUMB_WIDTH, VIDEO_THUMB_HEIGHT)
            cached = cache.get(key)
            if cached is not None:
                self.ready.emit(generation, row, cached)
                continue
            try:
                frame = get_single_thumbnail(
                    path,
                    preview_sample_time(start, end),
                    max_drift_sec=preview_max_drift_sec(start, end),
                    segment_start=start,
                    segment_end=end,
                )
            except Exception:  # noqa: BLE001 - a broken file must not stop the strip
                frame = None
            if frame is None:
                self.ready.emit(generation, row, None)
                continue
            try:
                import cv2

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                height, width, _ = rgb.shape
                # QImage is safe off the GUI thread; QPixmap is not.
                image = QImage(rgb.data, width, height, width * 3, QImage.Format_RGB888).copy()
                scaled = image.scaled(
                    VIDEO_THUMB_WIDTH,
                    VIDEO_THUMB_HEIGHT,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            except Exception:  # noqa: BLE001
                self.ready.emit(generation, row, None)
                continue
            cache.put(key, scaled)
            self.ready.emit(generation, row, scaled)


class UnifiedSearchPage(PageScaffold):
    """One query, both indexes."""

    video_activated = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("UnifiedSearchPage")
        self._language = "zh"
        self._t = _STRINGS["zh"]
        self._worker = None
        self._thumb_worker = None
        self._image_loader = None
        self._video_model = VideoHitModel(self)
        self._image_model = ImageGridModel(self)
        self._thumb_generation = 0
        self._thumb_requested: set[int] = set()
        self._last_payload: dict = {}

        self._pump_ticks = 0
        self._thumb_pump = QTimer(self)
        self._thumb_pump.setInterval(200)
        self._thumb_pump.timeout.connect(self._pump_image_thumbs)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.header.title.setText(self._t["title"])
        self.header.subtitle.setText(self._t["subtitle"])
        self.header.subtitle.show()

        self._build_search_card()
        self._build_results()

    # -- construction ---------------------------------------------------
    def _build_search_card(self) -> None:
        card = VSCard("panel", margins=(16, 14, 16, 14), spacing=10)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        column = card.content_layout

        title = QLabel(self._t["card"])
        title.setObjectName("SectionTitle")
        self.lbl_card_title = title
        column.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.scope_combo = QComboBox()
        for key in ("scope_all", "scope_video", "scope_image"):
            self.scope_combo.addItem(self._t[key], key.replace("scope_", ""))
        self.scope_combo.setMinimumWidth(112)
        self.scope_combo.setToolTip(self._t["scope_hint"])

        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchInput")
        self.search_input.setPlaceholderText(self._t["placeholder"])
        self.search_input.setClearButtonEnabled(True)

        self.btn_search = QPushButton(self._t["search"])
        set_button_role(self.btn_search, "PrimaryButton")
        self.btn_search.setCursor(Qt.CursorShape.PointingHandCursor)

        row.addWidget(self.scope_combo)
        row.addWidget(self.search_input, 1)
        row.addWidget(self.btn_search)
        column.addLayout(row)

        self.lbl_status = QLabel(self._t["idle"])
        self.lbl_status.setObjectName("Hint")
        self.lbl_status.setWordWrap(True)
        column.addWidget(self.lbl_status)

        # Persistent hint: nothing else ever writes here, so it survives a
        # results update and a language switch (apply_texts refreshes it).
        self.lbl_drag_hint = QLabel(self._t["drag_hint"])
        self.lbl_drag_hint.setObjectName("Hint")
        self.lbl_drag_hint.setWordWrap(True)
        column.addWidget(self.lbl_drag_hint)

        self.search_input.returnPressed.connect(self.start_search)
        self.btn_search.clicked.connect(self.start_search)
        self.content_layout.addWidget(card)

    def _build_results(self) -> None:
        card = VSCard("panel", margins=(16, 14, 16, 14), spacing=8)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        column = card.content_layout

        # -- video strip -------------------------------------------------
        self.lbl_video_title = QLabel(self._t["video_section"])
        self.lbl_video_title.setObjectName("SectionLabel")
        column.addWidget(self.lbl_video_title)

        self.video_grid = QListView()
        self.video_grid.setObjectName("UnifiedVideoStrip")
        self.video_grid.setViewMode(QListView.ViewMode.IconMode)
        self.video_grid.setFlow(QListView.Flow.LeftToRight)
        self.video_grid.setWrapping(False)
        self.video_grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.video_grid.setModel(self._video_model)
        self.video_grid.setItemDelegate(VideoHitDelegate(self, self.video_grid))
        self.video_grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.video_grid.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.video_grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.video_grid.setMouseTracking(True)
        self.video_grid.setStyleSheet(
            "QListView#UnifiedVideoStrip{background:transparent;border:none;outline:none;}"
        )
        self.video_grid.doubleClicked.connect(self._on_video_activated)
        file_actions.enable_file_drag(self.video_grid)
        file_actions.attach_result_menu(
            self.video_grid,
            self._video_model,
            field=VideoHitModel.DRAG_FIELD,
            labels=lambda: self._t,
        )

        self.video_hint = QLabel(self._t["video_idle"])
        self.video_hint.setObjectName("Hint")
        self.video_hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.video_hint.setContentsMargins(4, 10, 4, 10)

        self.video_stack = QStackedWidget()
        # One row of 160 px cells plus the horizontal scrollbar; the strip never
        # wraps, so its height is fixed and the photo wall gets the rest.
        self.video_stack.setFixedHeight(VIDEO_CELL_HEIGHT + 18)
        self.video_stack.addWidget(self.video_grid)
        self.video_stack.addWidget(self.video_hint)
        column.addWidget(self.video_stack)

        # -- photo grid --------------------------------------------------
        self.lbl_image_title = QLabel(self._t["image_section"])
        self.lbl_image_title.setObjectName("SectionLabel")
        column.addWidget(self.lbl_image_title)

        self.image_grid = QListView()
        self.image_grid.setObjectName("ImageGrid")
        self.image_grid.setViewMode(QListView.ViewMode.IconMode)
        self.image_grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.image_grid.setMovement(QListView.Movement.Static)
        self.image_grid.setUniformItemSizes(True)
        self.image_grid.setSpacing(4)
        self.image_grid.setWordWrap(False)
        self.image_grid.setModel(self._image_model)
        self.image_grid.setItemDelegate(ImageGridDelegate(self, self.image_grid))
        self.image_grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.image_grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.image_grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.image_grid.setMouseTracking(True)
        self.image_grid.setStyleSheet(
            "QListView#ImageGrid{background:transparent;border:none;outline:none;}"
        )
        self.image_grid.doubleClicked.connect(self._on_image_activated)
        file_actions.enable_file_drag(self.image_grid)
        file_actions.attach_result_menu(
            self.image_grid,
            self._image_model,
            field=ImageGridModel.DRAG_FIELD,
            labels=lambda: self._t,
        )

        self.image_hint = QLabel(self._t["image_idle"])
        self.image_hint.setObjectName("Hint")
        self.image_hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.image_hint.setContentsMargins(4, 10, 4, 10)

        self.image_stack = QStackedWidget()
        self.image_stack.setMinimumHeight(260)
        self.image_stack.addWidget(self.image_grid)
        self.image_stack.addWidget(self.image_hint)
        column.addWidget(self.image_stack, 1)

        self.content_layout.addWidget(card, 1)

    # -- video thumbnails -----------------------------------------------
    def _ensure_thumb_worker(self):
        if self._thumb_worker is None:
            worker = _VideoThumbWorker(self)
            worker.ready.connect(self._on_video_thumb)
            self._thumb_worker = worker
            worker.start()
        return self._thumb_worker

    def ensure_video_thumb(self, row: int) -> None:
        """Ask for one row's frame. Called by the delegate and by ``_fill_videos``."""
        hit = self._video_model.hit_at(row)
        path = str((hit or {}).get("video_path") or "")
        if not path or row in self._thumb_requested:
            return
        self._thumb_requested.add(row)
        self._ensure_thumb_worker().enqueue(
            self._thumb_generation,
            row,
            path,
            float(hit.get("start_sec") or 0.0),
            float(hit.get("end_sec") or 0.0),
        )

    def _on_video_thumb(self, generation: int, row: int, image) -> None:
        if generation != self._thumb_generation:
            # A newer search replaced the strip; this frame belongs to a dead row.
            return
        if image is None:
            return
        pixmap = image if isinstance(image, QPixmap) else QPixmap.fromImage(image)
        self._video_model.set_thumb(row, pixmap)

    # -- image thumbnails ------------------------------------------------
    def _loader(self):
        if self._image_loader is None:
            from ui.image_thumbs import ImageThumbLoader

            loader = ImageThumbLoader(self)
            loader.loaded.connect(self._on_image_thumb)
            self._image_loader = loader
        return self._image_loader

    def request_thumb(self, hit: dict) -> None:
        """Called by ``ImageGridDelegate`` when a cell has no pixmap yet."""
        from ui.image_thumbs import THUMB_SIZE_GRID

        image_id = str((hit or {}).get("image_id") or "")
        path = str((hit or {}).get("image_path") or "")
        if not image_id or not path:
            return
        loader = self._loader()
        key = loader.make_key(image_id, THUMB_SIZE_GRID)
        cached = loader.peek(key)
        if cached is not None:
            self._apply_image_thumb(key, cached)
            return
        loader.request(image_id, path, THUMB_SIZE_GRID)

    def _on_image_thumb(self, key: str, image) -> None:
        self._apply_image_thumb(key, image)

    def _apply_image_thumb(self, key: str, image) -> None:
        if image is None:
            return
        pixmap = image if isinstance(image, QPixmap) else QPixmap.fromImage(image)
        if not pixmap.isNull():
            self._image_model.set_thumb(key, pixmap)

    # -- search ---------------------------------------------------------
    def start_search(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            self.lbl_status.setText(self._t["idle"])
            return
        if self._worker is not None and self._worker.isRunning():
            return
        scope = str(self.scope_combo.currentData() or "all")
        self.btn_search.setEnabled(False)
        self.lbl_status.setText(self._t["searching"])
        worker = _SearchWorker(query, scope, page=self)
        worker.done.connect(self._on_results)
        worker.finished.connect(lambda: self.btn_search.setEnabled(True))
        self._worker = worker
        worker.start()

    def _on_results(self, payload: dict) -> None:
        self._last_payload = dict(payload or {})
        videos = list(payload.get("videos") or [])
        images = list(payload.get("images") or [])
        self._fill_videos(videos)
        self._fill_images(images)
        self._render_status()

    def _fill_videos(self, videos: list) -> None:
        self._thumb_generation += 1
        self._thumb_requested = set()
        self._video_model.set_hits(videos)
        if not videos:
            self._set_video_hint(self._t["no_videos"])
            return
        self.video_stack.setCurrentWidget(self.video_grid)
        self.video_grid.scrollToTop()
        for row in range(len(videos)):
            self.ensure_video_thumb(row)

    def _fill_images(self, images: list) -> None:
        from ui.image_thumbs import THUMB_SIZE_GRID, ImageThumbLoader

        keys = [
            ImageThumbLoader.make_key(str(hit.get("image_id") or ""), THUMB_SIZE_GRID)
            for hit in images
        ]
        self._image_model.set_hits(images, keys)
        if images:
            self.image_stack.setCurrentWidget(self.image_grid)
            self.image_grid.scrollToTop()
            self._start_thumb_pump()
        else:
            self._thumb_pump.stop()
            self._set_image_hint(self._t["no_images"])

    def _start_thumb_pump(self) -> None:
        # ImageThumbLoader drops requests beyond its in-flight cap and relies on a
        # repaint to ask again. A results page can sit still while the user reads
        # it, so cells would keep the "…" placeholder indefinitely; this pump asks
        # again every 200 ms until every visible result has a pixmap.
        self._pump_ticks = 0
        self._thumb_pump.start()

    def _pump_image_thumbs(self) -> None:
        model = self._image_model
        # Only the first screens: a 48-hit search fits, and a huge result set
        # would otherwise decode thumbnails for cells nobody scrolls to.
        limit = min(model.rowCount(), 120)
        missing = [row for row in range(limit) if not model.has_thumb(row)]
        if not missing or self._pump_ticks >= 75:
            self._thumb_pump.stop()
            return
        self._pump_ticks += 1
        for row in missing[:8]:
            self.request_thumb(model.hit_at(row))

    def _render_status(self) -> None:
        payload = self._last_payload
        videos = list(payload.get("videos") or [])
        images = list(payload.get("images") or [])
        self.lbl_video_title.setText(
            f"{self._t['video_section']}（{self._t['video_count'].format(count=len(videos))}）"
        )
        self.lbl_image_title.setText(
            f"{self._t['image_section']}（{self._t['image_count'].format(count=len(images))}）"
        )
        scope = str(payload.get("scope") or "all")
        if not videos:
            self._set_video_hint(self._video_hint_text(payload.get("video_error") or "", scope))
        if not images:
            self._set_image_hint(self._image_hint_text(payload.get("image_error") or "", scope))

        bits = [
            self._t["status"].format(
                videos=len(videos),
                images=len(images),
                elapsed=_format_elapsed(payload.get("elapsed_ms") or 0),
            )
        ]
        attrs = str(payload.get("image_attributes") or "")
        if attrs:
            bits.append(self._t["status_attr"].format(attrs=attrs))
        if images and str(payload.get("image_channel") or "") == "vector":
            bits.append(self._t["status_vector_only"])
        self.lbl_status.setText(" · ".join(bits))

    # -- helpers --------------------------------------------------------
    def _video_hint_text(self, error: str, scope: str = "all") -> str:
        if error == "index_not_ready":
            return self._t["video_error_index"]
        if error:
            return self._t["video_error"].format(error=error)
        if scope == "image":
            return self._t["skipped_video"]
        return self._t["no_videos"]

    def _image_hint_text(self, error: str, scope: str = "all") -> str:
        if error:
            return self._t["image_error"].format(error=error)
        if scope == "video":
            return self._t["skipped_image"]
        return self._t["no_images"]

    def _set_video_hint(self, text: str) -> None:
        self.video_hint.setText(text)
        self.video_stack.setCurrentWidget(self.video_hint)

    def _set_image_hint(self, text: str) -> None:
        self.image_hint.setText(text)
        self.image_stack.setCurrentWidget(self.image_hint)

    # -- activation -----------------------------------------------------
    def _on_video_activated(self, index) -> None:
        hit = self._video_model.hit_at(index.row()) if index.isValid() else {}
        if hit:
            self.video_activated.emit(hit)

    def _on_image_activated(self, index) -> None:
        hit = self._image_model.hit_at(index.row()) if index.isValid() else {}
        path = str((hit or {}).get("image_path") or "")
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # -- i18n -----------------------------------------------------------
    def apply_texts(self, language: str = "") -> None:
        text = str(language or "").strip().lower()
        self._language = text if text in _STRINGS else "zh"
        self._t = _STRINGS[self._language]
        self.header.title.setText(self._t["title"])
        self.header.subtitle.setText(self._t["subtitle"])
        self.lbl_card_title.setText(self._t["card"])
        self.search_input.setPlaceholderText(self._t["placeholder"])
        self.btn_search.setText(self._t["search"])
        self.lbl_drag_hint.setText(self._t["drag_hint"])
        self.scope_combo.setToolTip(self._t["scope_hint"])
        for index, key in enumerate(("scope_all", "scope_video", "scope_image")):
            self.scope_combo.setItemText(index, self._t[key])
        if self._last_payload:
            self._render_status()
        else:
            self.lbl_status.setText(self._t["idle"])
            self._set_video_hint(self._t["video_idle"])
            self._set_image_hint(self._t["image_idle"])

    # -- teardown -------------------------------------------------------
    def shutdown(self) -> None:
        self._thumb_pump.stop()
        for worker in (self._worker, self._thumb_worker):
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                if isinstance(worker, _VideoThumbWorker):
                    worker.stop()
                worker.wait(1500)
        self.btn_search.setEnabled(True)
