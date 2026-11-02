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

from PySide6.QtCore import Qt, QThread, Signal
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
        "detail_empty": "选中一条结果查看命中位置",
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
        "detail_empty": "Select a result to see where it matched",
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
        self._index_after_scan = False

        self.header.title.setText(self._t["title"])
        self.header.subtitle.setText(self._t["subtitle"])
        self.header.subtitle.hide()

        self._build_library_card()
        self._build_search_card()
        self._decorate()

    # -- construction ---------------------------------------------------
    def _build_library_card(self) -> None:
        card = VSCard("panel", margins=(16, 14, 16, 14), spacing=10)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        column = card.content_layout

        title = QLabel(self._t["library_card"])
        title.setObjectName("SectionTitle")
        column.addWidget(title)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_add_root = QPushButton(self._t["add_root"])
        set_button_role(self.btn_add_root, "PrimaryButton")
        self.btn_remove_root = QPushButton(self._t["remove_root"])
        set_button_role(self.btn_remove_root, "GhostButton")
        self.btn_open_root = QPushButton(self._t["open_root"])
        set_button_role(self.btn_open_root, "GhostButton")
        for button in (self.btn_add_root, self.btn_remove_root, self.btn_open_root):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        actions.addStretch(1)
        column.addLayout(actions)

        self.roots_list = QListWidget()
        self.roots_list.setObjectName("PdfRootsList")
        self.roots_list.setFixedHeight(74)
        self.roots_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        column.addWidget(self.roots_list)

        self.roots_hint = QLabel(self._t["roots_empty"])
        self.roots_hint.setObjectName("Hint")
        self.roots_hint.setWordWrap(True)
        column.addWidget(self.roots_hint)

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
        index_row.addStretch(1)
        column.addLayout(index_row)

        self.lbl_status = QLabel(self._t["status_idle"])
        self.lbl_status.setObjectName("Hint")
        self.lbl_status.setWordWrap(True)
        column.addWidget(self.lbl_status)

        self.content_layout.addWidget(card)

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

        self.results_list = QListWidget()
        self.results_list.setObjectName("PdfResultsList")
        self.results_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        column.addWidget(self.results_list, 1)

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
            path = str(hit.get("rel_path") or hit.get("pdf_path") or "")
            page = int(hit.get("page_no") or 1)
            snippet = str(hit.get("snippet") or "").strip()
            label = f"[{hit.get('score', 0):.2f}] {path} · 第 {page} 页"
            if snippet:
                label = f"{label}\n    {snippet}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, hit)
            item.setToolTip(snippet or path)
            self.results_list.addItem(item)
        self.lbl_search_status.setText(self._t["results_n"].format(count=len(hits)))
        if not hits:
            self.lbl_search_status.setText(self._t["results_empty"])

    def _on_search_failed(self, message: str) -> None:
        self.btn_search.setEnabled(True)
        self.lbl_search_status.setText(self._t["search_failed"].format(error=message))

    def _on_result_activated(self, item: QListWidgetItem) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        hit = item.data(Qt.ItemDataRole.UserRole) or {}
        path = str(hit.get("pdf_path") or "")
        if path and os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

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
        for name in ("_index_worker", "_search_worker"):
            worker = getattr(self, name, None)
            if worker is not None and worker.isRunning():
                stop = getattr(worker, "stop", None)
                if callable(stop):
                    stop()
                worker.wait(3000)
            setattr(self, name, None)
