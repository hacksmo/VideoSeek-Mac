"""Unified search: one query over video moments and photos.

The service half is pure data (no Qt, no engine), so it is tested with the two
searches patched out. The page half only checks wiring: counts land in the two
lists, an empty side shows its reason, and a double-clicked hit carries the
fields the player needs.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch


def _qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _video_hit(**overrides):
    payload = {
        "rank": 1,
        "video_path": "/videos/a.mp4",
        "start_sec": 12.0,
        "end_sec": 18.0,
        "score": 0.83,
        "start_timecode": "00:12",
        "end_timecode": "00:18",
        "match_kind": "chunk",
    }
    payload.update(overrides)
    return payload


def _image_hit(match="attribute", **overrides):
    payload = {
        "image_id": "a" * 32,
        "image_path": "/photos/a.jpg",
        "rel_path": "a.jpg",
        "caption": "一张照片",
        "people": [],
        "people_count": 1,
        "score": 0.91,
        "match": match,
    }
    payload.update(overrides)
    return payload


class ScopeTests(unittest.TestCase):
    def test_unknown_scope_falls_back_to_all(self):
        from src.services import unified_search

        self.assertEqual(unified_search.normalize_scope(None), "all")
        self.assertEqual(unified_search.normalize_scope(""), "all")
        self.assertEqual(unified_search.normalize_scope("everything"), "all")
        self.assertEqual(unified_search.normalize_scope("VIDEO"), "video")
        self.assertEqual(unified_search.normalize_scope("image"), "image")


class VideoRowTests(unittest.TestCase):
    def test_rows_keep_only_what_the_strip_shows(self):
        from src.services import unified_search

        rows = unified_search.video_rows({"hits": [_video_hit()]})

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["file_name"], "a.mp4")
        self.assertEqual(row["start_sec"], 12.0)
        self.assertEqual(row["start_timecode"], "00:12")
        self.assertNotIn("clip_window", row)

    def test_hits_without_a_path_are_dropped(self):
        from src.services import unified_search

        rows = unified_search.video_rows({"hits": [_video_hit(video_path=""), _video_hit()]})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video_path"], "/videos/a.mp4")

    def test_end_before_start_is_clamped(self):
        from src.services import unified_search

        rows = unified_search.video_rows({"hits": [_video_hit(start_sec=20.0, end_sec=10.0)]})

        self.assertEqual(rows[0]["end_sec"], 20.0)

    def test_limit_is_honoured(self):
        from src.services import unified_search

        hits = [_video_hit(video_path=f"/videos/{i}.mp4") for i in range(5)]
        rows = unified_search.video_rows({"hits": hits}, limit=2)

        self.assertEqual(len(rows), 2)


class ChannelTests(unittest.TestCase):
    def test_channel_follows_the_first_hit(self):
        from src.services import unified_search

        self.assertEqual(unified_search.image_channel([_image_hit("attribute")]), "attribute")
        self.assertEqual(unified_search.image_channel([_image_hit("keyword")]), "literal")
        self.assertEqual(unified_search.image_channel([_image_hit("vector")]), "vector")
        self.assertEqual(unified_search.image_channel([]), "")
        self.assertEqual(unified_search.image_channel([{"match": "browse"}]), "")


class CombinedSearchTests(unittest.TestCase):
    def test_both_sides_are_merged(self):
        from src.services import unified_search

        with (
            patch.object(unified_search, "search_videos", return_value=[{"video_path": "/v.mp4"}]),
            patch.object(unified_search, "search_images", return_value=[_image_hit()]),
            patch.object(unified_search, "_attribute_label", return_value="衣服颜色=黑色"),
        ):
            result = unified_search.search("黑色衣服")

        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["image_channel"], "attribute")
        self.assertEqual(result["image_attributes"], "衣服颜色=黑色")
        self.assertEqual(result["video_error"], "")
        self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_video_failure_keeps_the_photos(self):
        from src.services import unified_search

        with (
            patch.object(unified_search, "search_videos", side_effect=RuntimeError("boom")),
            patch.object(unified_search, "search_images", return_value=[_image_hit()]),
        ):
            result = unified_search.search("黑色衣服")

        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["video_error"], "boom")

    def test_missing_video_index_is_marked_not_quoted(self):
        from src.services import unified_search
        from src.web.agent_api.errors import IndexNotReadyError

        with (
            patch.object(
                unified_search,
                "search_videos",
                side_effect=IndexNotReadyError("Search index is not ready. Sync the library."),
            ),
            patch.object(unified_search, "search_images", return_value=[]),
        ):
            result = unified_search.search("黑色衣服")

        self.assertEqual(result["video_error"], "index_not_ready")

    def test_image_failure_keeps_the_videos(self):
        from src.services import unified_search

        with (
            patch.object(unified_search, "search_videos", return_value=[{"video_path": "/v.mp4"}]),
            patch.object(unified_search, "search_images", side_effect=RuntimeError("no index")),
        ):
            result = unified_search.search("a red car")

        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(result["image_error"], "no index")

    def test_a_single_scope_skips_the_other_index(self):
        from src.services import unified_search

        with (
            patch.object(unified_search, "search_videos", return_value=[]) as videos,
            patch.object(unified_search, "search_images", return_value=[]) as images,
        ):
            unified_search.search("x", scope="video")
            self.assertEqual(images.call_count, 0)
            self.assertEqual(videos.call_count, 1)

            videos.reset_mock()
            unified_search.search("x", scope="image")
            self.assertEqual(videos.call_count, 0)
            self.assertEqual(images.call_count, 1)

    def test_empty_query_does_no_work(self):
        from src.services import unified_search

        with (
            patch.object(unified_search, "search_videos") as videos,
            patch.object(unified_search, "search_images") as images,
        ):
            result = unified_search.search("   ")

        self.assertEqual(videos.call_count, 0)
        self.assertEqual(images.call_count, 0)
        self.assertEqual(result["videos"], [])


class PageTests(unittest.TestCase):
    def setUp(self):
        _qt_app()
        from ui.widgets.unified_search_page import UnifiedSearchPage

        self.page = UnifiedSearchPage()
        self.page.apply_texts("zh")

    def tearDown(self):
        self.page.shutdown()
        self.page.deleteLater()

    def _payload(self, videos=(), images=(), **overrides):
        payload = {
            "query": "黑色衣服",
            "scope": "all",
            "videos": list(videos),
            "images": list(images),
            "video_error": "",
            "image_error": "",
            "image_channel": "attribute",
            "image_attributes": "衣服颜色=黑色",
            "elapsed_ms": 340,
        }
        payload.update(overrides)
        return payload

    def test_results_fill_both_sides_and_the_status_line(self):
        from src.services import unified_search as service

        videos = service.video_rows({"hits": [_video_hit()]})
        self.page._on_results(self._payload(videos=videos, images=[_image_hit()]))

        self.assertEqual(self.page._video_model.rowCount(), 1)
        self.assertEqual(self.page._image_model.rowCount(), 1)
        self.assertIn("视频 1 条", self.page.lbl_status.text())
        self.assertIn("图片 1 张", self.page.lbl_status.text())
        self.assertIn("衣服颜色=黑色", self.page.lbl_status.text())
        self.assertIs(self.page.video_stack.currentWidget(), self.page.video_grid)

    def test_video_hit_carries_what_the_player_needs(self):
        from src.services import unified_search as service

        videos = service.video_rows({"hits": [_video_hit()]})
        self.page._on_results(self._payload(videos=videos))

        hit = self.page._video_model.hit_at(0)
        self.assertEqual(hit["video_path"], "/videos/a.mp4")
        self.assertEqual(hit["start_sec"], 12.0)
        self.assertEqual(hit["end_sec"], 18.0)

        seen = []
        self.page.video_activated.connect(seen.append)
        self.page._on_video_activated(self.page._video_model.index(0, 0))
        self.assertEqual(seen[0]["video_path"], "/videos/a.mp4")

    def test_strip_cell_is_tall_enough_for_a_frame(self):
        # QListWidget ignored setGridSize and collapsed a cell to one text line;
        # the delegate's sizeHint is what actually sizes the strip now.
        from ui.widgets.unified_search_page import VIDEO_CELL_HEIGHT, VIDEO_THUMB_HEIGHT

        self.assertGreater(VIDEO_CELL_HEIGHT, VIDEO_THUMB_HEIGHT)

    def test_missing_index_shows_a_sentence_not_an_exception(self):
        self.page._on_results(self._payload(video_error="index_not_ready"))

        self.assertIs(self.page.video_stack.currentWidget(), self.page.video_hint)
        self.assertIn("视频库", self.page.video_hint.text())

    def test_empty_photo_side_says_so(self):
        self.page._on_results(self._payload())

        self.assertIs(self.page.image_stack.currentWidget(), self.page.image_hint)
        self.assertEqual(self.page.image_hint.text(), "图片没有命中。")

    def test_single_scope_says_the_other_side_was_skipped(self):
        # "没有命中" would be a lie when that index was never queried.
        self.page._on_results(self._payload(scope="image", images=[_image_hit()]))

        self.assertIn("只查了图片", self.page.video_hint.text())

    def test_language_switch_rewrites_visible_text(self):
        self.page._on_results(self._payload(images=[_image_hit()]))
        self.page.apply_texts("en")

        self.assertEqual(self.page.header.title.text(), "Unified search")
        self.assertEqual(self.page.btn_search.text(), "Search")
        self.assertIn("photos", self.page.lbl_status.text())
        self.assertEqual(self.page.scope_combo.itemText(0), "Everything")

    def test_thumb_pump_fills_missing_cells_then_idles(self):
        # ImageThumbLoader drops requests above its in-flight cap and waits for a
        # repaint; a still page never repaints, so the pump has to re-ask itself.
        from PySide6.QtGui import QImage, QPixmap
        from ui.image_thumbs import THUMB_SIZE_GRID, ImageThumbLoader

        image = _image_hit()
        self.page._on_results(self._payload(images=[image]))
        asked = []
        self.page.request_thumb = lambda hit: asked.append(hit.get("image_id"))

        self.page._pump_ticks = 0
        self.page._pump_image_thumbs()
        self.assertEqual(asked, [image["image_id"]])

        key = ImageThumbLoader.make_key(image["image_id"], THUMB_SIZE_GRID)
        self.page._image_model.set_thumb(key, QPixmap(QImage(4, 4, QImage.Format_RGB888)))
        self.page._pump_image_thumbs()

        self.assertFalse(self.page._thumb_pump.isActive())
        self.assertEqual(len(asked), 1)


if __name__ == "__main__":
    unittest.main()
