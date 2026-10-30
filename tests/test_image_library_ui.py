"""Tests for the image library page, thumbnail cache and browse API.

Qt widgets are built without showing them, so no window or event loop is needed
beyond a ``QApplication`` instance. The Lance-backed tests reuse the temporary
directory trick from ``tests/test_image_library.py``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import numpy as np


def _qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# Thumbnail cache
# ---------------------------------------------------------------------------


class ThumbCacheTests(unittest.TestCase):
    def setUp(self):
        import ui.image_thumbs as thumbs

        _qt_app()
        self.thumbs = thumbs
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = thumbs.thumb_cache_dir
        thumbs.thumb_cache_dir = lambda: os.path.join(self._tmp.name, "thumbs")  # type: ignore[assignment]

    def tearDown(self):
        self.thumbs.thumb_cache_dir = self._orig_dir  # type: ignore[assignment]
        self._tmp.cleanup()

    def _write_source(self, width=1200, height=800):
        import cv2

        image = np.zeros((height, width, 3), dtype=np.uint8)
        image[:, : width // 2] = (30, 40, 200)
        path = os.path.join(self._tmp.name, "source.jpg")
        cv2.imwrite(path, image)
        return path

    def test_build_thumb_writes_a_smaller_jpeg_and_reuses_it(self):
        source = self._write_source()
        cached = self.thumbs.build_thumb(source, image_id="a" * 32, size=128)
        self.assertTrue(os.path.isfile(cached))
        self.assertTrue(cached.endswith(".jpg"))

        import cv2

        thumb = cv2.imread(cached)
        self.assertIsNotNone(thumb)
        self.assertLessEqual(max(thumb.shape[:2]), 128)
        self.assertEqual(thumb.shape[0], 85)  # 1200x800 scaled to 128 -> 128x85

        first_mtime = os.path.getmtime(cached)
        again = self.thumbs.build_thumb(source, image_id="a" * 32, size=128)
        self.assertEqual(again, cached)
        self.assertEqual(os.path.getmtime(cached), first_mtime)

    def test_thumb_path_is_namespaced_by_size_and_id(self):
        small = self.thumbs.thumb_path("b" * 32, 256)
        large = self.thumbs.thumb_path("b" * 32, 1024)
        self.assertNotEqual(small, large)
        self.assertIn("b" * 32, os.path.basename(small))

    def test_qimage_from_bgr_keeps_dimensions(self):
        image = np.zeros((10, 20, 3), dtype=np.uint8)
        qimage = self.thumbs.qimage_from_bgr(image)
        self.assertEqual(qimage.width(), 20)
        self.assertEqual(qimage.height(), 10)

    def test_missing_source_raises(self):
        with self.assertRaises(OSError):
            self.thumbs.build_thumb(
                os.path.join(self._tmp.name, "nope.jpg"), image_id="c" * 32, size=64
            )

    def test_loader_key_includes_size(self):
        loader = self.thumbs.ImageThumbLoader(workers=1)
        try:
            self.assertEqual(loader.make_key("x", 256), "x:256")
            self.assertNotEqual(loader.make_key("x", 256), loader.make_key("x", 1024))
        finally:
            loader.shutdown()


# ---------------------------------------------------------------------------
# Grid model / delegate helpers
# ---------------------------------------------------------------------------


class ImageGridModelTests(unittest.TestCase):
    def setUp(self):
        _qt_app()
        from ui.widgets.image_library_page import ImageGridModel

        self.model = ImageGridModel()
        self.hits = [
            {"image_id": "a" * 32, "caption": "海边日落", "rel_path": "a.jpg", "people_count": 0},
            {"image_id": "b" * 32, "caption": "", "rel_path": "b.jpg", "people_count": 2},
        ]
        self.model.set_hits(self.hits, ["a" * 32 + ":256", "b" * 32 + ":256"])

    def test_rows_and_hit_role(self):
        from ui.widgets.image_library_page import HIT_ROLE

        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.data(self.model.index(0), HIT_ROLE)["caption"], "海边日落")
        self.assertEqual(self.model.hit_at(1)["image_id"], "b" * 32)
        self.assertEqual(self.model.hit_at(99), {})

    def test_display_role_prefers_caption_then_path(self):
        from PySide6.QtCore import Qt

        self.assertEqual(
            self.model.data(self.model.index(0), Qt.ItemDataRole.DisplayRole), "海边日落"
        )
        self.assertEqual(
            self.model.data(self.model.index(1), Qt.ItemDataRole.DisplayRole), "b.jpg"
        )

    def test_set_thumb_notifies_the_matching_rows(self):
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QPixmap

        from ui.widgets.image_library_page import THUMB_ROLE

        received = []
        self.model.dataChanged.connect(lambda first, last, roles=None: received.append(first.row()))

        pixmap = QPixmap(8, 8)
        self.model.set_thumb("b" * 32 + ":256", pixmap)
        self.assertEqual(received, [1])
        self.assertIsInstance(self.model.data(self.model.index(1), THUMB_ROLE), QPixmap)
        self.assertIsNone(self.model.data(self.model.index(0), THUMB_ROLE))
        self.assertTrue(_Qt.ItemDataRole.DisplayRole is not None)

    def test_reset_clears_previous_hits(self):
        self.model.set_hits([], [])
        self.assertEqual(self.model.rowCount(), 0)


class DetailPreviewTests(unittest.TestCase):
    def setUp(self):
        _qt_app()
        from ui.widgets.image_library_page import _DetailPreview

        self.preview = _DetailPreview("placeholder")

    def test_large_pixmap_does_not_drive_the_layout(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        source = QPixmap(4000, 3000)
        source.fill()
        self.preview.set_source(source)

        # An explicit minimum beats QLabel's pixmap-sized minimumSizeHint.
        self.assertEqual(self.preview.minimumSize().width(), 1)
        self.assertEqual(self.preview.minimumSize().height(), 1)

        # ... which is what stops the caption being pushed out of the card.
        container = QWidget()
        container.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        layout = QVBoxLayout(container)
        caption = QLabel("caption")
        layout.addWidget(self.preview, 1)
        layout.addWidget(caption)
        container.resize(320, 300)
        container.show()
        layout.activate()

        self.assertLessEqual(self.preview.height(), 300)
        self.assertGreaterEqual(caption.y(), self.preview.geometry().bottom())

    def test_placeholder_returns_after_clearing(self):
        from PySide6.QtGui import QPixmap

        pixmap = QPixmap(20, 20)
        pixmap.fill()
        self.preview.set_source(pixmap)
        self.assertEqual(self.preview.text(), "")
        self.preview.clear_source()
        self.assertEqual(self.preview.text(), "placeholder")
        self.assertTrue(self.preview.pixmap().isNull())

    def test_placeholder_updates_for_language_switch(self):
        self.preview.set_placeholder("选择一张图片")
        self.assertEqual(self.preview.text(), "选择一张图片")


# ---------------------------------------------------------------------------
# Page construction
# ---------------------------------------------------------------------------


class ImageLibraryPageTests(unittest.TestCase):
    def setUp(self):
        _qt_app()
        from ui.widgets.image_library_page import ImageLibraryPage

        self.page = ImageLibraryPage()

    def tearDown(self):
        self.page.shutdown()
        self.page.setParent(None)
        self.page.deleteLater()
        _qt_app().processEvents()

    def test_page_exposes_the_expected_controls(self):
        page = self.page
        self.assertEqual(page.mode_combo.count(), 3)
        self.assertEqual(page.grid.model().rowCount(), 0)
        self.assertFalse(page.btn_stop.isEnabled())
        self.assertFalse(page.detail_image.pixmap() and not page.detail_image.pixmap().isNull())

    def test_apply_texts_switches_language(self):
        self.page.apply_texts("en")
        self.assertEqual(self.page.header.title.text(), "Photo Library")
        self.assertEqual(self.page.btn_index.text(), "Start indexing")
        self.page.apply_texts("zh")
        self.assertEqual(self.page.header.title.text(), "图片库")

    def test_vector_only_results_get_a_caveat(self):
        page = self.page
        vector_hits = [{"image_id": "a" * 32, "match": "vector", "people_count": 0}]
        page._last_query = "海边日落"
        self.assertTrue(page._is_vector_only_guess(vector_hits))

        # A literal hit means the query really matched file text: no caveat.
        mixed = vector_hits + [{"image_id": "b" * 32, "match": "keyword", "people_count": 0}]
        self.assertFalse(page._is_vector_only_guess(mixed))

        # English queries legitimately live on the vector path.
        page._last_query = "a sunset by the sea"
        self.assertFalse(page._is_vector_only_guess(vector_hits))

    def test_people_filter_drops_results_below_the_threshold(self):
        hits = [
            {"image_id": "a" * 32, "people_count": 0},
            {"image_id": "b" * 32, "people_count": 2},
        ]
        self.page.spin_people.setValue(1)
        self.page._on_results(hits, "browse")
        self.assertEqual(self.page._model.rowCount(), 1)
        self.assertEqual(self.page._model.hit_at(0)["image_id"], "b" * 32)

    def test_prune_note_is_not_overwritten_by_the_status_summary(self):
        page = self.page
        page.refresh_status = lambda: None  # no worker in a unit test
        page._run_search = lambda mode, query: None
        page._on_pruned({"missing": 3, "removed": 3})
        page._on_status_ready(
            {"indexed": 42, "described": 0, "dimension": 512, "vlm_ready": True}
        )
        text = page.lbl_status.text()
        self.assertIn("42", text)
        self.assertIn("3", text)  # the prune outcome must survive


# ---------------------------------------------------------------------------
# Browse / prune service API (real temporary Lance table)
# ---------------------------------------------------------------------------


class BrowseServiceTests(unittest.TestCase):
    def setUp(self):
        from src.storage import image_lance_store as store

        self.store = store
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = store.get_image_lance_dir
        store.get_image_lance_dir = lambda config=None: os.path.join(  # type: ignore[assignment]
            self._tmp.name, "lance"
        )

    def tearDown(self):
        self.store.get_image_lance_dir = self._orig  # type: ignore[assignment]
        self._tmp.cleanup()

    def _row(self, image_id, mtime, caption="", path=None):
        return {
            "image_id": image_id,
            "library_root": "/tmp/root",
            "image_path": path or f"/tmp/root/{image_id}.jpg",
            "rel_path": f"{image_id}.jpg",
            "mtime": float(mtime),
            "width": 640,
            "height": 480,
            "bytes": 1000,
            "caption": caption,
            "people_json": json.dumps([], ensure_ascii=False),
            "tags_json": json.dumps([], ensure_ascii=False),
            "describer": "test",
            "vector": [1.0, 0.0, 0.0, 0.0],
        }

    def test_list_indexed_images_sorts_recent_first_and_pages(self):
        from src.services import image_index_service as service

        self.store.upsert_image_rows(
            [self._row("1" * 32, 100), self._row("2" * 32, 300), self._row("3" * 32, 200)]
        )
        recent = service.list_indexed_images()
        self.assertEqual([hit["image_id"] for hit in recent], ["2" * 32, "3" * 32, "1" * 32])
        self.assertEqual(recent[0]["match"], "browse")

        oldest = service.list_indexed_images(order="oldest", limit=1)
        self.assertEqual([hit["image_id"] for hit in oldest], ["1" * 32])

        paged = service.list_indexed_images(limit=1, offset=1)
        self.assertEqual([hit["image_id"] for hit in paged], ["3" * 32])

    def test_list_indexed_images_carries_file_metadata(self):
        from src.services import image_index_service as service

        self.store.upsert_image_rows([self._row("4" * 32, 150, caption="山间小路")])
        hit = service.list_indexed_images()[0]
        self.assertEqual(hit["caption"], "山间小路")
        self.assertEqual(hit["width"], 640)
        self.assertEqual(hit["bytes"], 1000)
        self.assertIn("mtime", hit)

    def test_prune_missing_images_dry_run_then_delete(self):
        from src.services import image_index_service as service

        real = os.path.join(self._tmp.name, "real.jpg")
        with open(real, "wb") as handle:
            handle.write(b"x")
        self.store.upsert_image_rows(
            [self._row("5" * 32, 100, path=real), self._row("6" * 32, 100, path="/tmp/gone.jpg")]
        )

        dry = service.prune_missing_images(dry_run=True)
        self.assertEqual(dry["missing"], 1)
        self.assertEqual(dry["removed"], 0)
        self.assertEqual(self.store.image_index_stats()["rows"], 2)

        result = service.prune_missing_images()
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.store.image_index_stats()["rows"], 1)


if __name__ == "__main__":
    unittest.main()
