"""Document (PDF) library page: register folders, extract text, search.

Phase 1: text-only. Each PDF page's text is indexed and searchable by literal
match (Chinese/English), with hits locating back to ``{path, page_no}``.
Page-image vectors and VLM captions arrive in Phase 2.

The page mirrors the image library's structure (folders on top, search + results
below) but is far lighter: literal search needs no CLIP warm-up, so it runs
inline on a worker thread without any model dependency.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ui.widgets.button_roles import set_button_role
from ui.widgets.scaffold import PageScaffold, VSCard

_STRINGS = {
    "zh": {
        "title": "文档库",
        "subtitle": "给 PDF 建立文字索引，按内容搜到具体页。",
        "library_card": "PDF 目录",
        "search_card": "检索",
        "add_root": "添加文件夹",
        "remove_root": "移除选中",
        "open_root": "打开目录",
        "rescan": "重新扫描",
        "start_index": "开始索引",
        "stop": "停止",
        "roots_empty": "还没有登记目录。点「添加文件夹」选一个放 PDF 的目录。",
        "status_idle": "尚未索引",
        "status_summary": "已索引 {documents} 份文档 · {pages} 页",
        "scan_running": "正在扫描 PDF…",
        "scanned": "发现 {count} 份 PDF",
        "index_running": "{done}/{total} · {name}",
        "index_done": "索引完成：{added} 份、{pages} 页、失败 {failed}",
        "index_failed": "索引失败：{error}",
        "search_placeholder": "输入要搜的文字，例如「资产负债表」",
        "search": "搜索",
        "searching": "检索中…",
        "results_n": "{count} 条结果",
        "results_empty": "没有命中。换个说法，或先确认已索引。",
        "search_failed": "检索失败：{error}",
        "no_files": "没有发现可索引的 PDF。",
        "confirm_big_title": "索引量大",
        "confirm_big_body": "本次将处理 {count} 份 PDF。继续吗？",
        "detail_empty": "选中一条结果，右侧查看该页预览",
        "preview_title": "页面预览",
        "prev_page": "上一页",
        "next_page": "下一页",
        "page_label": "第 {page} / {total} 页",
        "open_pdf": "打开 PDF",
        "preview_loading": "正在渲染…",
        "preview_failed": "渲染失败：{error}",
        "menu_reveal": "在访达中显示",
        "menu_copy_path": "复制文件路径",
        "menu_open": "用默认应用打开",
        "menu_delete_index": "从索引移除",
        "menu_delete_file": "删除并移入废纸篓",
        "delete_confirm_title": "删除 PDF",
        "delete_confirm_body": "将把「{name}」移入废纸篓，并从索引移除。\n\n此操作可从废纸篓找回，确定继续吗？",
        "delete_done": "已删除 {count} 份文档",
        "delete_failed": "删除失败：{error}",
    },
    "en": {
        "title": "Documents",
        "subtitle": "Build a text index over PDFs and search down to the page.",
        "library_card": "PDF folders",
        "search_card": "Search",
        "add_root": "Add folder",
        "remove_root": "Remove selected",
        "open_root": "Open folder",
        "rescan": "Rescan",
        "start_index": "Start indexing",
        "stop": "Stop",
        "roots_empty": "No folders yet. Click “Add folder” and pick a PDF directory.",
        "status_idle": "Not indexed yet",
        "status_summary": "{documents} documents indexed · {pages} pages",
        "scan_running": "Scanning PDFs…",
        "scanned": "{count} PDFs found",
        "index_running": "{done}/{total} · {name}",
        "index_done": "Done: {added} documents, {pages} pages, {failed} failed",
        "index_failed": "Indexing failed: {error}",
        "search_placeholder": "Type text to find, e.g. “balance sheet”",
        "search": "Search",
        "searching": "Searching…",
        "results_n": "{count} results",
        "results_empty": "No hits. Try another wording, or confirm the index has content.",
        "search_failed": "Search failed: {error}",
        "no_files": "No indexable PDFs found.",
        "confirm_big_title": "Large batch",
        "confirm_big_body": "About to index {count} PDFs. Continue?",
        "detail_empty": "Select a result to preview that page on the right",
        "preview_title": "Page preview",
        "prev_page": "Previous",
        "next_page": "Next",
        "page_label": "Page {page} / {total}",
        "open_pdf": "Open PDF",
        "preview_loading": "Rendering…",
        "preview_failed": "Render failed: {error}",
        "menu_reveal": "Reveal in Finder",
        "menu_copy_path": "Copy file path",
        "menu_open": "Open with default app",
        "menu_delete_index": "Remove from index",
        "menu_delete_file": "Delete & move to Trash",
        "delete_confirm_title": "Delete PDF",
        "delete_confirm_body": "Move “{name}” to the Trash and remove it from the index.\n\nYou can restore it from the Trash. Continue?",
        "delete_done": "Deleted {count} document(s)",
        "delete_failed": "Delete failed: {error}",
    },
}


class _IndexWorker(QThread):
    progressed = Signal(int, int, str)
    indexed = Signal(dict)
    failed = Signal(str)

    def __init__(self, paths, *, parent=None):
        super().__init__(parent)
        self._paths = list(paths or [])
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self):
        try:
            from src.services import pdf_service as service

            def _progress(done, total, path):
                self.progressed.emit(int(done), int(total), str(path))

            result = service.index_documents(
                self._paths,
                progress=_progress,
                should_stop=lambda: self._stop,
            )
            self.indexed.emit(result)
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(str(exc))


class _SearchWorker(QThread):
    results = Signal(list)
    failed = Signal(str)

    def __init__(self, query, *, top_k, parent=None):
        super().__init__(parent)
        self._query = str(query or "")
        self._top_k = int(top_k)

    def run(self):
        try:
            from src.services import pdf_service as service

            self.results.emit(service.search_by_text(self._query, top_k=self._top_k))
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(str(exc))


class _RenderWorker(QThread):
    """Render every page of a PDF to QPixmaps (fit-width, one per page)."""

    rendered = Signal(int, list)  # start_page_no, [QPixmap, ...]
    failed = Signal(str)

    def __init__(self, pdf_path: str, *, start_page: int = 1, parent=None):
        super().__init__(parent)
        self._path = str(pdf_path or "")
        self._start_page = int(start_page or 1)

    def run(self):
        try:
            import pymupdf
        except ImportError:  # pragma: no cover - older installs
            import fitz as pymupdf  # type: ignore
        try:
            from PySide6.QtGui import QImage, QPixmap

            pixmaps: list = []
            with pymupdf.open(self._path) as doc:
                for page in doc:
                    pix = page.get_pixmap(dpi=160)
                    image = QImage(
                        pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
                    )
                    pixmaps.append(QPixmap.fromImage(image.copy()))
            self.rendered.emit(self._start_page, pixmaps)
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(str(exc))


class _PagePreview(QWidget):
    """Continuous, scrollable PDF preview: every page stacked vertically.

    Pages are scaled to fit the preview width; scrolling replaces prev/next
    buttons. The requested start page is scrolled into view after render.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QScrollArea

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        title = QLabel("")
        title.setObjectName("SectionTitle")
        self.lbl_title = title
        root.addWidget(title)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("PdfPreviewScroll")
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.canvas = QWidget()
        self.canvas.setObjectName("PdfPreviewCanvas")
        self.canvas_layout = QVBoxLayout(self.canvas)
        self.canvas_layout.setContentsMargins(8, 8, 8, 8)
        self.canvas_layout.setSpacing(12)
        self.canvas_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.canvas)
        root.addWidget(self.scroll, 1)

        self._page_labels: list[QLabel] = []
        self._start_page = 1
        self._pending_scroll = False

    def _clear(self) -> None:
        while self.canvas_layout.count():
            item = self.canvas_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._page_labels = []

    def set_pages(self, pixmaps: list, start_page: int) -> None:
        self._clear()
        self._start_page = max(1, int(start_page or 1))
        self._pending_scroll = True
        if not pixmaps:
            self.set_placeholder("")
            return
        for page_no, pixmap in enumerate(pixmaps, start=1):
            label = QLabel()
            label.setObjectName("PdfPreviewPage")
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            label.setMinimumWidth(1)
            label.setPixmap(pixmap)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            label.setProperty("pageNo", page_no)
            self._page_labels.append(label)
            self.canvas_layout.addWidget(label)
        self._fit_width()

    def _fit_width(self) -> None:
        viewport_w = self.scroll.viewport().width() - 16
        for label in self._page_labels:
            pixmap = label.pixmap()
            if pixmap is None or pixmap.isNull() or viewport_w <= 0:
                continue
            scaled = pixmap.scaledToWidth(
                viewport_w, Qt.TransformationMode.SmoothTransformation
            )
            label.setPixmap(scaled)
        # Scroll to the requested start page after the layout settles.
        if self._pending_scroll:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self._scroll_to_start)

    def _scroll_to_start(self) -> None:
        self._pending_scroll = False
        target_index = max(0, self._start_page - 1)
        if target_index < len(self._page_labels):
            label = self._page_labels[target_index]
            y = label.mapTo(self.canvas, label.rect().topLeft()).y()
            self.scroll.verticalScrollBar().setValue(max(0, y - 8))

    def set_placeholder(self, text: str) -> None:
        self._clear()
        placeholder = QLabel(text or "")
        placeholder.setObjectName("Hint")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setWordWrap(True)
        self.canvas_layout.addWidget(placeholder)

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self._page_labels:
            self._fit_width()


class _ResultDelegate(QStyledItemDelegate):
    """Card-style hit: filename + page badge on top, snippet below, with padding.

    Replaces the default flat text so hits no longer run together. Colors come
    from the theme token map (the app paints with QSS, so the raw palette stays
    light even in dark mode).
    """

    PADDING = 12
    MARGIN = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._colors: dict = {}
        self._dark: bool | None = None

    def _theme(self) -> dict:
        from PySide6.QtWidgets import QApplication

        from ui.widgets.styles import theme_color_map

        app = QApplication.instance()
        dark = bool(app.property("videoseek_is_dark")) if app is not None else True
        if self._dark is not dark or not self._colors:
            self._colors = theme_color_map(dark)
            self._dark = dark
        return self._colors

    def sizeHint(self, option, index):  # noqa: N802 - Qt API
        hit = index.data(Qt.ItemDataRole.UserRole) or {}
        snippet = str(hit.get("snippet") or "").strip()
        text = snippet if snippet else str(hit.get("rel_path") or hit.get("pdf_path") or "")
        width = option.rect.width() - self.PADDING * 2
        metrics = option.fontMetrics
        # Estimate wrapped line count for the snippet at the delegate's font size.
        chars_per_line = max(20, width // max(6, metrics.horizontalAdvance("中")))
        lines = max(1, (len(text) + chars_per_line - 1) // chars_per_line)
        lines = min(lines, 4)  # cap tall snippets; they elide
        height = 22 + 6 + lines * (metrics.height() + 3) + self.PADDING * 2
        return QSize(option.rect.width(), height)

    def paint(self, painter: QPainter, option, index):  # noqa: N802 - Qt API
        hit = index.data(Qt.ItemDataRole.UserRole) or {}
        colors = self._theme()
        rect = option.rect.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Card background.
        base = QColor(str(colors.get("PANEL") or "#2e2e2e"))
        if selected:
            base = QColor(str(colors.get("ACCENT_SOFT") or "#1e3a5f"))
        elif hovered:
            base = base.lighter(112) if base.lightness() < 128 else base.darker(104)
        radius = 8.0
        path = QPainterPath()
        path.addRoundedRect(rect.toRectF(), radius, radius)
        painter.fillPath(path, base)

        if selected:
            pen = QPen(QColor(str(colors.get("ACCENT") or "#60a5fa")))
            pen.setWidth(1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        text_color = QColor(str(colors.get("TEXT") or "#e5e5e5"))
        muted = QColor(str(colors.get("MUTED") or "#a3a3a3"))
        accent = QColor(str(colors.get("ACCENT") or "#60a5fa"))

        inner = rect.adjusted(self.PADDING, self.PADDING, -self.PADDING, -self.PADDING)

        # Title line: file name + page badge.
        name = str(hit.get("rel_path") or hit.get("pdf_path") or "")
        name = os.path.basename(name) if name else ""
        page = int(hit.get("page_no") or 1)
        score = float(hit.get("score") or 0.0)
        title_font = painter.font()
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(text_color)
        title_rect = QRect(inner.left(), inner.top(), inner.width() - 70, 20)
        metrics = painter.fontMetrics()
        title_text = metrics.elidedText(name or "(未命名)", Qt.TextElideMode.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title_text)

        # Page badge (right side of title line).
        badge_text = f"P{page}"
        badge_font = painter.font()
        badge_font.setBold(False)
        badge_font.setPointSizeF(max(7.5, badge_font.pointSizeF() - 1))
        painter.setFont(badge_font)
        painter.setPen(accent)
        badge_rect = QRect(inner.right() - 60, inner.top(), 60, 20)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, badge_text)

        # Score + snippet below.
        snippet = str(hit.get("snippet") or "").strip()
        body_font = painter.font()
        body_font.setBold(False)
        body_font.setPointSizeF(max(9.0, body_font.pointSizeF()))
        painter.setFont(body_font)
        body_rect = QRect(inner.left(), inner.top() + 26, inner.width(), inner.height() - 26)
        if snippet:
            painter.setPen(muted)
            painter.drawText(
                body_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                f"{score:.2f}  {snippet}",
            )
        else:
            painter.setPen(muted)
            painter.drawText(body_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{score:.2f}")

        painter.restore()


class PdfLibraryPage(PageScaffold):
    """Document (PDF) library: folders on top, search + hits below."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PdfLibraryPage")
        self._language = "zh"
        self._t = _STRINGS["zh"]
        self._roots: list[str] = []
        self._scan_files: list[str] = []
        self._index_worker = None
        self._search_worker = None
        self._render_worker = None
        self._index_after_scan = False
        self._current_pdf_path = ""
        self._current_page_no = 1

        self.header.title.setText(self._t["title"])
        self.header.subtitle.setText(self._t["subtitle"])
        self.header.subtitle.hide()

        # 「PDF 目录」控制面板：交给主窗口挂到左侧全局侧边栏（与图片库一致）。
        # 页面主体只保留检索 + 结果列表。
        self.library_card = self._build_library_card()
        self.library_card.setMinimumWidth(0)
        self.library_card.setMaximumWidth(16777215)
        self.library_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        self._build_search_card()
        self._decorate()

    # -- construction ---------------------------------------------------
    def _build_library_card(self) -> QWidget:
        # 窄侧栏：卡片高度自适应内容，宽度由侧边栏决定。
        card = VSCard("panel", margins=(14, 12, 14, 12), spacing=10)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        column = card.content_layout

        title = QLabel(self._t["library_card"])
        title.setObjectName("SectionTitle")
        column.addWidget(title)

        # 添加文件夹占整行，移除 / 打开目录 并排。
        self.btn_add_root = QPushButton(self._t["add_root"])
        set_button_role(self.btn_add_root, "PrimaryButton")
        self.btn_add_root.setCursor(Qt.CursorShape.PointingHandCursor)
        column.addWidget(self.btn_add_root)

        root_actions = QHBoxLayout()
        root_actions.setSpacing(8)
        self.btn_remove_root = QPushButton(self._t["remove_root"])
        set_button_role(self.btn_remove_root, "GhostButton")
        self.btn_open_root = QPushButton(self._t["open_root"])
        set_button_role(self.btn_open_root, "GhostButton")
        for button in (self.btn_remove_root, self.btn_open_root):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            root_actions.addWidget(button)
        column.addLayout(root_actions)

        self.roots_list = QListWidget()
        self.roots_list.setObjectName("PdfRootsList")
        self.roots_list.setMinimumHeight(96)
        self.roots_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        column.addWidget(self.roots_list)

        self.roots_hint = QLabel(self._t["roots_empty"])
        self.roots_hint.setObjectName("Hint")
        self.roots_hint.setWordWrap(True)
        column.addWidget(self.roots_hint)

        # 重新扫描 / 开始索引 / 停止。
        index_row = QHBoxLayout()
        index_row.setSpacing(8)
        self.btn_rescan = QPushButton(self._t["rescan"])
        set_button_role(self.btn_rescan, "GhostButton")
        self.btn_index = QPushButton(self._t["start_index"])
        set_button_role(self.btn_index, "AccentGhostButton")
        self.btn_stop = QPushButton(self._t["stop"])
        set_button_role(self.btn_stop, "DangerGhostButton")
        self.btn_stop.setEnabled(False)
        for button in (self.btn_rescan, self.btn_index, self.btn_stop):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            index_row.addWidget(button)
        column.addLayout(index_row)

        self.lbl_status = QLabel(self._t["status_idle"])
        self.lbl_status.setObjectName("Hint")
        self.lbl_status.setWordWrap(True)
        column.addWidget(self.lbl_status)

        column.addStretch(1)
        return card

    def _build_search_card(self) -> None:
        card = VSCard("panel", margins=(16, 14, 16, 14), spacing=10)
        column = card.content_layout

        title = QLabel(self._t["search_card"])
        title.setObjectName("SectionTitle")
        column.addWidget(title)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchInput")
        self.search_input.setPlaceholderText(self._t["search_placeholder"])
        self.search_input.setClearButtonEnabled(True)
        self.btn_search = QPushButton(self._t["search"])
        set_button_role(self.btn_search, "PrimaryButton")
        self.btn_search.setCursor(Qt.CursorShape.PointingHandCursor)
        controls.addWidget(self.search_input, 1)
        controls.addWidget(self.btn_search)
        column.addLayout(controls)

        self.lbl_search_status = QLabel(self._t["detail_empty"])
        self.lbl_search_status.setObjectName("Hint")
        self.lbl_search_status.setWordWrap(True)
        column.addWidget(self.lbl_search_status)

        # 结果列表：卡片式 delegate，每条有清晰分隔。
        self.results_list = QListWidget()
        self.results_list.setObjectName("PdfResultsList")
        self.results_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results_list.setItemDelegate(_ResultDelegate(self.results_list))
        self.results_list.setSpacing(6)
        self.results_list.setUniformItemSizes(False)
        self.results_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        # 右侧 PDF 连续预览：所有页竖排，滚动即翻页。
        self.preview = _PagePreview()
        self.preview.lbl_title.setText(self._t["preview_title"])
        self.preview.set_placeholder(self._t["detail_empty"])
        self.preview.setMinimumWidth(320)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("PdfResultsSplitter")
        splitter.addWidget(self.results_list)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([600, 400])
        splitter.setChildrenCollapsible(False)
        column.addWidget(splitter, 1)

        self.content_layout.addWidget(card, 1)

    def _decorate(self) -> None:
        self.btn_add_root.clicked.connect(self._on_add_root)
        self.btn_remove_root.clicked.connect(self._on_remove_root)
        self.btn_open_root.clicked.connect(self._on_open_root)
        self.btn_rescan.clicked.connect(self._on_rescan)
        self.btn_index.clicked.connect(self._on_index_clicked)
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        self.btn_search.clicked.connect(self._on_search_clicked)
        self.search_input.returnPressed.connect(self._on_search_clicked)
        self.roots_list.currentRowChanged.connect(self._on_root_selected)
        self.results_list.itemDoubleClicked.connect(self._on_result_activated)
        self.results_list.currentItemChanged.connect(self._on_current_changed)
        self.results_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results_list.customContextMenuRequested.connect(self._on_result_menu)

    # -- language -------------------------------------------------------
    def apply_texts(self, language: str = "") -> None:
        lang = str(language or self._language or "zh")
        self._language = "en" if lang.startswith("en") else "zh"
        self._t = _STRINGS[self._language]
        t = self._t
        self.header.title.setText(t["title"])
        self.header.subtitle.setText(t["subtitle"])
        self.btn_add_root.setText(t["add_root"])
        self.btn_remove_root.setText(t["remove_root"])
        self.btn_open_root.setText(t["open_root"])
        self.btn_rescan.setText(t["rescan"])
        self.btn_index.setText(t["start_index"])
        self.btn_stop.setText(t["stop"])
        self.btn_search.setText(t["search"])
        self.search_input.setPlaceholderText(t["search_placeholder"])
        self.preview.lbl_title.setText(t["preview_title"])
        if not self._roots:
            self.roots_hint.setText(t["roots_empty"])

    # -- folders --------------------------------------------------------
    def _load_roots(self) -> None:
        from src.services import pdf_service as service

        try:
            manifest = service.load_manifest()
        except Exception:
            manifest = {"roots": []}
        self._roots = [
            str(entry.get("root") or "")
            for entry in (manifest.get("roots") or [])
            if isinstance(entry, dict) and entry.get("root")
        ]
        self.roots_list.blockSignals(True)
        self.roots_list.clear()
        for root in self._roots:
            item = QListWidgetItem(root)
            item.setToolTip(root)
            self.roots_list.addItem(item)
        self.roots_list.blockSignals(False)
        self.roots_hint.setVisible(not self._roots)
        self.roots_list.setVisible(bool(self._roots))
        if self._roots:
            self.roots_list.setCurrentRow(0)

    def _on_add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, self._t["add_root"], os.path.expanduser("~"))
        if not folder:
            return
        from src.services import pdf_service as service

        try:
            service.add_library_root(folder)
        except Exception as exc:
            QMessageBox.warning(self, self._t["title"], f"{folder}\n\n{exc}")
            return
        self._load_roots()
        self._start_scan()

    def _on_remove_root(self) -> None:
        item = self.roots_list.currentItem()
        if item is None:
            return
        from src.services import pdf_service as service

        service.remove_library_root(item.text())
        self._load_roots()

    def _on_open_root(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        item = self.roots_list.currentItem()
        if item is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item.text()))

    def _on_root_selected(self, row: int) -> None:
        has = 0 <= row < len(self._roots)
        self.btn_open_root.setEnabled(has)
        self.btn_remove_root.setEnabled(has)

    # -- scanning / indexing -------------------------------------------
    def _start_scan(self) -> None:
        from src.services import pdf_service as service

        self.lbl_status.setText(self._t["scan_running"])
        self._scan_files = service.scan_manifest_roots()
        count = len(self._scan_files)
        if not self._scan_files:
            self.lbl_status.setText(self._t["no_files"])
            self._index_after_scan = False
            return
        self.lbl_status.setText(self._t["scanned"].format(count=count))
        if self._index_after_scan:
            self._index_after_scan = False
            self._start_index()

    def _on_rescan(self) -> None:
        self._scan_files = []
        self._start_scan()

    def _on_index_clicked(self) -> None:
        if self._index_worker is not None and self._index_worker.isRunning():
            return
        if not self._scan_files:
            self._index_after_scan = True
            self._start_scan()
            return
        self._start_index()

    def _start_index(self) -> None:
        paths = list(self._scan_files)
        if not paths:
            self.lbl_status.setText(self._t["no_files"])
            return
        if len(paths) > 500:
            answer = QMessageBox.question(
                self,
                self._t["confirm_big_title"],
                self._t["confirm_big_body"].format(count=len(paths)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.btn_index.setEnabled(False)
        self.btn_rescan.setEnabled(False)
        self.btn_stop.setEnabled(True)

        worker = _IndexWorker(paths, parent=self)
        worker.progressed.connect(self._on_index_progress)
        worker.indexed.connect(self._on_index_finished)
        worker.failed.connect(self._on_index_failed)
        self._index_worker = worker
        worker.start()

    def _on_index_progress(self, done, total, path) -> None:
        name = os.path.basename(path or "")
        self.lbl_status.setText(self._t["index_running"].format(done=done, total=total, name=name))

    def _on_index_finished(self, result: dict) -> None:
        self.btn_index.setEnabled(True)
        self.btn_rescan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        payload = dict(result or {})
        self.lbl_status.setText(
            self._t["index_done"].format(
                added=payload.get("added", 0),
                pages=payload.get("pages", 0),
                failed=payload.get("failed", 0),
            )
        )
        self._refresh_status()

    def _on_index_failed(self, message: str) -> None:
        self.btn_index.setEnabled(True)
        self.btn_rescan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText(self._t["index_failed"].format(error=message))

    def _on_stop_clicked(self) -> None:
        if self._index_worker is not None:
            self._index_worker.stop()

    # -- search ---------------------------------------------------------
    def _on_search_clicked(self) -> None:
        if self._search_worker is not None and self._search_worker.isRunning():
            return
        query = self.search_input.text().strip()
        if not query:
            return
        self.lbl_search_status.setText(self._t["searching"])
        self.btn_search.setEnabled(False)
        worker = _SearchWorker(query, top_k=100, parent=self)
        worker.results.connect(self._on_results)
        worker.failed.connect(self._on_search_failed)
        self._search_worker = worker
        worker.start()

    def _on_results(self, hits: list) -> None:
        self.btn_search.setEnabled(True)
        self.results_list.clear()
        for hit in hits:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, hit)
            path = str(hit.get("rel_path") or hit.get("pdf_path") or "")
            item.setToolTip(str(hit.get("snippet") or path))
            self.results_list.addItem(item)
        self.lbl_search_status.setText(self._t["results_n"].format(count=len(hits)))
        if not hits:
            self.lbl_search_status.setText(self._t["results_empty"])
            self.preview.set_placeholder(self._t["detail_empty"])
        else:
            self.results_list.setCurrentRow(0)

    def _on_search_failed(self, message: str) -> None:
        self.btn_search.setEnabled(True)
        self.lbl_search_status.setText(self._t["search_failed"].format(error=message))

    def _on_result_activated(self, item: QListWidgetItem) -> None:
        self._open_pdf(item.data(Qt.ItemDataRole.UserRole) or {})

    def _on_current_changed(self, current, _previous) -> None:
        if current is None:
            self.preview.set_placeholder(self._t["detail_empty"])
            return
        hit = current.data(Qt.ItemDataRole.UserRole) or {}
        path = str(hit.get("pdf_path") or "")
        page = int(hit.get("page_no") or 1)
        if path:
            self._current_pdf_path = path
            self._render_preview(page)
        else:
            self.preview.set_placeholder(self._t["detail_empty"])

    def _render_preview(self, start_page: int) -> None:
        if not self._current_pdf_path:
            return
        if self._render_worker is not None and self._render_worker.isRunning():
            self._render_worker.requestInterruption()
        worker = _RenderWorker(self._current_pdf_path, start_page=start_page, parent=self)
        worker.rendered.connect(self._on_rendered)
        worker.failed.connect(self._on_render_failed)
        self._render_worker = worker
        worker.start()

    def _on_rendered(self, start_page: int, pixmaps: list) -> None:
        self.preview.set_pages(pixmaps, start_page)

    def _on_render_failed(self, message: str) -> None:
        self.preview.set_placeholder(self._t["preview_failed"].format(error=message))

    @staticmethod
    def _open_pdf(hit: dict) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        path = str(hit.get("pdf_path") or "")
        if path and os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # -- context menu ---------------------------------------------------
    def _on_result_menu(self, pos) -> None:
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QMenu

        item = self.results_list.itemAt(pos)
        if item is None:
            return
        hit = item.data(Qt.ItemDataRole.UserRole) or {}
        pdf_id = str(hit.get("pdf_id") or "")
        path = str(hit.get("pdf_path") or "")

        menu = QMenu(self.results_list)
        act_reveal = QAction(self._t["menu_reveal"], menu)
        act_copy = QAction(self._t["menu_copy_path"], menu)
        act_open = QAction(self._t["menu_open"], menu)
        menu.addAction(act_open)
        menu.addAction(act_reveal)
        menu.addAction(act_copy)
        menu.addSeparator()
        act_delete_index = QAction(self._t["menu_delete_index"], menu)
        act_delete_file = QAction(self._t["menu_delete_file"], menu)
        menu.addAction(act_delete_index)
        menu.addAction(act_delete_file)

        chosen = menu.exec(self.results_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen is act_open:
            self._open_pdf(hit)
        elif chosen is act_reveal:
            from ui import file_actions

            file_actions.reveal_in_file_manager([path])
        elif chosen is act_copy:
            from ui import file_actions

            file_actions.copy_paths_to_clipboard([path])
        elif chosen is act_delete_index:
            if pdf_id:
                self._delete_documents([pdf_id], delete_file=False)
        elif chosen is act_delete_file:
            if pdf_id:
                name = os.path.basename(path) if path else pdf_id
                self._confirm_delete_files([pdf_id], name)

    def _confirm_delete_files(self, pdf_ids: list, name: str) -> None:
        answer = QMessageBox.question(
            self,
            self._t["delete_confirm_title"],
            self._t["delete_confirm_body"].format(name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._delete_documents(pdf_ids, delete_file=True)

    def _delete_documents(self, pdf_ids: list, *, delete_file: bool) -> None:
        from src.services import pdf_service as service

        try:
            removed = 0
            for pdf_id in pdf_ids:
                result = service.delete_document(pdf_id, delete_file=delete_file)
                removed += int(result.get("removed") or 0)
            self.lbl_search_status.setText(self._t["delete_done"].format(count=removed))
            self._refresh_status()
        except Exception as exc:  # pragma: no cover - defensive
            self.lbl_search_status.setText(self._t["delete_failed"].format(error=exc))

    # -- status / lifecycle --------------------------------------------
    def _refresh_status(self) -> None:
        from src.services import pdf_service as service

        try:
            status = service.get_document_library_status()
            self.lbl_status.setText(
                self._t["status_summary"].format(
                    documents=int(status.get("documents") or 0),
                    pages=int(status.get("pages") or 0),
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.lbl_status.setText(str(exc))

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        if not getattr(self, "_loaded_once", False):
            self._loaded_once = True
            self._load_roots()
            self._refresh_status()

    def shutdown(self) -> None:
        for name in ("_index_worker", "_search_worker", "_render_worker"):
            worker = getattr(self, name, None)
            if worker is not None and worker.isRunning():
                stop = getattr(worker, "stop", None)
                if callable(stop):
                    stop()
                else:
                    worker.requestInterruption()
                worker.wait(3000)
            setattr(self, name, None)
