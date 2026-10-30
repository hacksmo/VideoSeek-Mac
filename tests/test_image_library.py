"""Image library: format whitelist, description parsing, scanning, Lance storage."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch


from src.media.formats import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    is_supported_image_path,
    is_supported_media_path,
    is_supported_video_path,
)
from src.services.image_index_service import (
    _keyword_haystack,
    _literal_score,
    _row_to_hit,
    file_content_id,
    has_cjk,
    query_terms,
    scan_image_files,
)
from src.services.image_understanding import (
    _json_prefill,
    build_image_prompt,
    parse_image_description,
    people_to_text,
)


class ImageFormatTests(unittest.TestCase):
    def test_image_extensions_are_detected_case_insensitively(self):
        for name in ("a.jpg", "a.JPEG", "a.PNG", "a.webp", "a.HeIc", "a.avif", "a.tiff"):
            with self.subTest(name=name):
                self.assertTrue(is_supported_image_path(name))

    def test_video_containers_are_not_images(self):
        self.assertFalse(is_supported_image_path("movie.mp4"))
        self.assertTrue(is_supported_video_path("movie.mp4"))
        self.assertTrue(is_supported_video_path("movie.MKV"))

    def test_media_helper_covers_both(self):
        self.assertTrue(is_supported_media_path("still.png"))
        self.assertTrue(is_supported_media_path("clip.mov"))
        self.assertFalse(is_supported_media_path("notes.txt"))

    def test_image_extensions_stay_out_of_the_video_whitelist(self):
        # Several call sites treat VIDEO_EXTS as "ffprobe can read a duration".
        overlap = set(IMAGE_EXTS) & set(VIDEO_EXTS)
        self.assertEqual(overlap, set())


class ImageDescriptionParseTests(unittest.TestCase):
    def test_fenced_json_is_parsed(self):
        raw = """```json
        {"caption": "窗边的女人", "people": [{"count": 1, "gender": "女", "clothing": "红色连衣裙",
         "action": "看手机", "position": "画面左侧"}], "tags": ["室内", "红裙", "窗边"]}
        ```"""
        parsed = parse_image_description(raw)
        self.assertTrue(parsed["parse_ok"])
        self.assertEqual(parsed["caption"], "窗边的女人")
        self.assertEqual(len(parsed["people"]), 1)
        self.assertEqual(parsed["people"][0]["clothing"], "红色连衣裙")
        self.assertEqual(parsed["tags"], ["室内", "红裙", "窗边"])

    def test_json_with_surrounding_prose_is_parsed(self):
        raw = '好的，结果如下：{"caption": "两个人在海边", "people": [], "tags": ["海边"]} 以上。'
        parsed = parse_image_description(raw)
        self.assertTrue(parsed["parse_ok"])
        self.assertEqual(parsed["caption"], "两个人在海边")

    def test_prose_only_falls_back_to_caption(self):
        parsed = parse_image_description("一个男人坐在台阶上喝咖啡。")
        self.assertFalse(parsed["parse_ok"])
        self.assertEqual(parsed["caption"], "一个男人坐在台阶上喝咖啡。")
        self.assertEqual(parsed["people"], [])

    def test_empty_reply_is_empty(self):
        parsed = parse_image_description("")
        self.assertEqual(parsed["caption"], "")
        self.assertFalse(parsed["parse_ok"])

    def test_people_as_strings_are_tolerated(self):
        parsed = parse_image_description(
            json.dumps({"caption": "c", "people": ["穿蓝衬衫的男子"], "tags": []}, ensure_ascii=False)
        )
        self.assertEqual(len(parsed["people"]), 1)
        self.assertEqual(parsed["people"][0]["count"], 1)
        self.assertIn("蓝衬衫", parsed["people"][0]["features"])

    def test_count_is_clamped(self):
        parsed = parse_image_description(
            json.dumps({"caption": "c", "people": [{"count": "999", "gender": "男"}]})
        )
        self.assertEqual(parsed["people"][0]["count"], 99)
        parsed = parse_image_description(
            json.dumps({"caption": "c", "people": [{"count": 0, "gender": "男"}]})
        )
        self.assertEqual(parsed["people"][0]["count"], 1)

    def test_empty_person_entries_are_dropped(self):
        parsed = parse_image_description(
            json.dumps({"caption": "c", "people": [{"count": 1}, {"gender": "女"}], "tags": []})
        )
        self.assertEqual(len(parsed["people"]), 1)
        self.assertEqual(parsed["people"][0]["gender"], "女")

    def test_tags_are_deduped_and_capped(self):
        tags = ["红裙", "红裙"] + [f"t{index}" for index in range(30)]
        parsed = parse_image_description(json.dumps({"caption": "c", "tags": tags}))
        self.assertLessEqual(len(parsed["tags"]), 12)
        self.assertEqual(len(parsed["tags"]), len(set(parsed["tags"])))

    def test_prompt_carries_the_no_identity_rule(self):
        self.assertIn("不推测姓名", build_image_prompt("zh"))
        self.assertIn("Never guess names", build_image_prompt("en"))

    def test_truncated_json_is_repaired_not_filed_as_prose(self):
        # A crowded frame runs past the reply budget; without repair the whole
        # fragment became a prose caption and every person in it was lost.
        truncated = (
            '{"caption": "校园内人群聚集", "people": ['
            '{"count": 1, "gender": "男", "age": "青年", "clothing": "黑色制服", '
            '"build": "偏瘦", "style": "正装", "action": "站立观察", "features": '
        )
        parsed = parse_image_description(truncated)
        self.assertTrue(parsed["parse_ok"])
        self.assertEqual(parsed["caption"], "校园内人群聚集")
        self.assertEqual(len(parsed["people"]), 1)
        self.assertEqual(parsed["people"][0]["build"], "偏瘦")

    def test_truncated_json_keeps_only_complete_entries(self):
        truncated = (
            '{"caption": "街拍", "people": ['
            '{"count": 1, "gender": "女", "clothing": "驼色大衣"}, '
            '{"count": 1, "gender": "男", "clothing": "黑衬衫"}, '
            '{"count": 1, "gender": '
        )
        parsed = parse_image_description(truncated)
        self.assertTrue(parsed["parse_ok"])
        self.assertEqual([p["gender"] for p in parsed["people"]], ["女", "男"])

    def test_truncated_after_caption_still_yields_the_caption(self):
        # Cut inside the unclosed people array, before any person finished.
        parsed = parse_image_description('{"caption": "海边日落", "people": [')
        self.assertTrue(parsed["parse_ok"])
        self.assertEqual(parsed["caption"], "海边日落")
        self.assertEqual(parsed["people"], [])

    def test_plain_prose_is_not_mistaken_for_json(self):
        parsed = parse_image_description("这张照片里有两个人站在窗边。")
        self.assertFalse(parsed["parse_ok"])
        self.assertEqual(parsed["people"], [])

    def test_build_and_style_survive_parsing(self):
        parsed = parse_image_description(
            json.dumps(
                {
                    "caption": "两人在街边合影",
                    "people": [
                        {"count": 1, "gender": "女", "clothing": "米色风衣", "build": "偏瘦", "style": "素净"},
                    ],
                    "tags": [],
                },
                ensure_ascii=False,
            )
        )
        person = parsed["people"][0]
        self.assertEqual(person["build"], "偏瘦")
        self.assertEqual(person["style"], "素净")

    def test_prompt_asks_for_build_and_style_neutral(self):
        prompt = build_image_prompt("zh")
        self.assertIn("偏胖|微胖|匀称|偏瘦", prompt)
        self.assertIn("不评判身材好坏", prompt)

    def test_only_ollama_gets_a_json_prefill(self):
        # qwen3-vl on Ollama always emits a think block and ignores think:false,
        # so the assistant turn is seeded with "{". Other providers must not be
        # touched -- a trailing assistant message is not universally accepted.
        self.assertEqual(_json_prefill({"provider_preset": "ollama"}), "{")
        self.assertEqual(_json_prefill({"provider_preset": "lm_studio"}), "")
        self.assertEqual(_json_prefill({}), "")


class PeopleTextTests(unittest.TestCase):
    def test_single_person_is_flattened(self):
        text = people_to_text([{"count": 1, "gender": "女", "clothing": "红裙", "position": "左侧"}])
        self.assertEqual(text, "女 红裙 左侧")

    def test_group_keeps_the_count(self):
        text = people_to_text([{"count": 3, "gender": "男", "action": "跑步"}])
        self.assertEqual(text, "3人 男 跑步")

    def test_build_and_style_are_searchable(self):
        text = people_to_text([{"count": 1, "gender": "男", "build": "偏胖", "style": "花哨"}])
        self.assertIn("偏胖", text)
        self.assertIn("花哨", text)

    def test_empty_entries_are_skipped(self):
        self.assertEqual(people_to_text([{}, {"gender": ""}]), "")


class ScanTests(unittest.TestCase):
    def _make_tree(self, root: str) -> None:
        os.makedirs(os.path.join(root, "nested", "deeper"), exist_ok=True)
        for relative in ("a.jpg", "nested/b.PNG", "nested/deeper/c.webp"):
            with open(os.path.join(root, relative), "wb") as handle:
                handle.write(b"\x89PNG\r\n")
        with open(os.path.join(root, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        with open(os.path.join(root, ".hidden.jpg"), "wb") as handle:
            handle.write(b"x")
        os.makedirs(os.path.join(root, "__MACOSX"), exist_ok=True)
        with open(os.path.join(root, "__MACOSX", "d.jpg"), "wb") as handle:
            handle.write(b"x")

    def test_recursive_scan_skips_hidden_and_non_images(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            self._make_tree(root)
            found = scan_image_files(root)
            names = sorted(os.path.basename(path) for path in found)
            self.assertEqual(names, ["a.jpg", "b.PNG", "c.webp"])

    def test_non_recursive_scan_only_reads_the_top_level(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            self._make_tree(root)
            found = scan_image_files(root, recursive=False)
            self.assertEqual([os.path.basename(path) for path in found], ["a.jpg"])

    def test_missing_folder_returns_empty(self):
        self.assertEqual(scan_image_files("/nonexistent/videoseek/images"), [])


class ContentIdTests(unittest.TestCase):
    def test_identical_content_yields_the_same_id(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            first = os.path.join(root, "one.jpg")
            second = os.path.join(root, "two.jpg")
            for path in (first, second):
                with open(path, "wb") as handle:
                    handle.write(b"same-bytes")
            self.assertEqual(file_content_id(first), file_content_id(second))
            self.assertEqual(len(file_content_id(first)), 32)
            self.assertTrue(all(char in "0123456789abcdef" for char in file_content_id(first)))

    def test_different_content_yields_different_ids(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            first = os.path.join(root, "a.jpg")
            second = os.path.join(root, "b.jpg")
            with open(first, "wb") as handle:
                handle.write(b"one")
            with open(second, "wb") as handle:
                handle.write(b"two")
            self.assertNotEqual(file_content_id(first), file_content_id(second))


class KeywordRowTests(unittest.TestCase):
    def test_haystack_covers_caption_people_and_tags(self):
        row = {
            "caption": "窗边的女人",
            "rel_path": "travel/001.jpg",
            "people_json": json.dumps([{"gender": "女", "clothing": "红裙"}], ensure_ascii=False),
            "tags_json": json.dumps(["室内"], ensure_ascii=False),
        }
        haystack = _keyword_haystack(row)
        for token in ("窗边", "红裙", "室内", "travel"):
            self.assertIn(token.casefold(), haystack)

    def test_row_to_hit_parses_and_counts_people(self):
        row = {
            "image_id": "a" * 32,
            "image_path": "/tmp/a.jpg",
            "rel_path": "a.jpg",
            "library_root": "/tmp",
            "caption": "两个人在海边",
            "people_json": json.dumps([{"count": 2, "gender": "女"}], ensure_ascii=False),
            "tags_json": json.dumps(["海边"], ensure_ascii=False),
        }
        hit = _row_to_hit(row, score=0.5, match="keyword")
        self.assertEqual(hit["people_count"], 2)
        self.assertEqual(hit["tags"], ["海边"])
        self.assertEqual(hit["score"], 0.5)

    def test_broken_json_does_not_raise(self):
        hit = _row_to_hit({"image_id": "b" * 32, "people_json": "not json"}, score=0.1, match="keyword")
        self.assertEqual(hit["people"], [])
        self.assertEqual(hit["people_count"], 0)


class ImageStoreRoundTripTests(unittest.TestCase):
    """Talks to a real (temporary) Lance directory; no video tables involved."""

    def setUp(self):
        import tempfile

        from src.storage import image_lance_store as store

        self.store = store
        self._tmp = tempfile.TemporaryDirectory()
        self._original = store.get_image_lance_dir
        store.get_image_lance_dir = lambda config=None: os.path.join(  # type: ignore[assignment]
            self._tmp.name, "lance"
        )

    def tearDown(self):
        self.store.get_image_lance_dir = self._original  # type: ignore[assignment]
        self._tmp.cleanup()

    def _row(self, image_id: str, vector, caption: str, people=None):
        return {
            "image_id": image_id,
            "library_root": "/tmp/root",
            "image_path": f"/tmp/root/{image_id}.jpg",
            "rel_path": f"{image_id}.jpg",
            "mtime": 1.0,
            "width": 640,
            "height": 480,
            "bytes": 1234,
            "caption": caption,
            "people_json": json.dumps(people or [], ensure_ascii=False),
            "tags_json": json.dumps(["tag"], ensure_ascii=False),
            "describer": "test",
            "vector": [float(value) for value in vector],
        }

    def test_schema_declares_the_vector_width(self):
        schema = self.store.image_table_schema(512)
        self.assertEqual(schema.field("vector").type.list_size, 512)
        for column in ("image_id", "caption", "people_json", "tags_json", "rel_path"):
            self.assertIn(column, schema.names)

    def test_upsert_search_update_delete(self):
        rows = [
            self._row("1" * 32, [1.0, 0.0, 0.0, 0.0], "红色连衣裙的女人"),
            self._row("2" * 32, [0.0, 1.0, 0.0, 0.0], "海边日落"),
        ]
        self.assertEqual(self.store.upsert_image_rows(rows), 2)

        stats = self.store.image_index_stats()
        self.assertEqual(stats["rows"], 2)
        self.assertEqual(stats["dimension"], 4)

        hits = self.store.search_image_vectors([1.0, 0.0, 0.0, 0.0], top_k=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["image_id"], "1" * 32)
        self.assertIn("_distance", hits[0])

        # Same id + new caption must update in place, not duplicate.
        updated = self._row("1" * 32, [1.0, 0.0, 0.0, 0.0], "改过的描述")
        self.store.upsert_image_rows([updated])
        self.assertEqual(self.store.image_index_stats()["rows"], 2)
        row = self.store.get_image_row("1" * 32, columns=["caption"])
        self.assertEqual(row["caption"], "改过的描述")

        self.store.delete_images(["2" * 32])
        self.assertEqual(self.store.image_index_stats()["rows"], 1)

    def test_dimension_change_recreates_the_table(self):
        self.store.upsert_image_rows([self._row("3" * 32, [1.0, 0.0], "两点向量")])
        self.assertEqual(self.store.image_index_stats()["dimension"], 2)
        # A new CLIP model with a different width must not corrupt the table.
        table, dimension = self.store.open_image_table(dimension=3)
        self.assertEqual(dimension, 3)
        self.assertEqual(table.schema.field("vector").type.list_size, 3)
        self.assertEqual(self.store.image_index_stats()["rows"], 0)

    def test_invalid_image_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.upsert_image_rows([self._row("not-a-hash", [1.0, 0.0], "x")])


class KeywordSearchTests(unittest.TestCase):
    def test_keyword_search_ranks_by_term_coverage(self):
        from src.services import image_index_service as service

        rows = [
            {
                "image_id": "a" * 32,
                "image_path": "/tmp/a.jpg",
                "rel_path": "a.jpg",
                "library_root": "/tmp",
                "caption": "穿红裙的女人在窗边",
                "people_json": json.dumps([{"count": 1, "clothing": "红裙"}], ensure_ascii=False),
                "tags_json": "[]",
            },
            {
                "image_id": "b" * 32,
                "image_path": "/tmp/b.jpg",
                "rel_path": "b.jpg",
                "library_root": "/tmp",
                "caption": "海边日落",
                "people_json": "[]",
                "tags_json": "[]",
            },
        ]
        original = service.__dict__["search_by_keyword"]
        from src.storage import image_lance_store as store

        original_iter = store.iter_image_rows
        store.iter_image_rows = lambda **kwargs: iter(rows)  # type: ignore[assignment]
        try:
            hits = original("红裙", top_k=5)
        finally:
            store.iter_image_rows = original_iter  # type: ignore[assignment]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["image_id"], "a" * 32)
        self.assertEqual(hits[0]["match"], "keyword")

    def test_people_filters(self):
        from src.services import image_index_service as service
        from src.storage import image_lance_store as store

        rows = [
            {
                "image_id": "a" * 32,
                "image_path": "/tmp/a.jpg",
                "rel_path": "a.jpg",
                "library_root": "/tmp",
                "caption": "一个人",
                "people_json": json.dumps([{"count": 1, "gender": "女"}], ensure_ascii=False),
                "tags_json": "[]",
            },
            {
                "image_id": "b" * 32,
                "image_path": "/tmp/b.jpg",
                "rel_path": "b.jpg",
                "library_root": "/tmp",
                "caption": "一群人",
                "people_json": json.dumps([{"count": 5, "gender": "男"}], ensure_ascii=False),
                "tags_json": "[]",
            },
        ]
        original_iter = store.iter_image_rows
        store.iter_image_rows = lambda **kwargs: iter(rows)  # type: ignore[assignment]
        try:
            many = service.search_by_keyword("人", top_k=5, min_people=3)
            only_one = service.search_by_keyword("人", top_k=5, max_people=2)
        finally:
            store.iter_image_rows = original_iter  # type: ignore[assignment]
        self.assertEqual([hit["image_id"] for hit in many], ["b" * 32])
        self.assertEqual([hit["image_id"] for hit in only_one], ["a" * 32])


class ChineseQueryTests(unittest.TestCase):
    """Chinese queries must not be handed to the English-only CLIP tower."""

    def test_has_cjk_detects_han_only(self):
        self.assertTrue(has_cjk("西装"))
        self.assertTrue(has_cjk("OOTD 穿搭"))
        self.assertFalse(has_cjk("a man in a suit"))
        self.assertFalse(has_cjk(""))

    def test_terms_split_at_stop_chars(self):
        fragments, latin = query_terms("穿西装的男人")
        self.assertIn("穿西装", fragments)
        self.assertIn("西装", fragments)
        self.assertIn("男人", fragments)
        self.assertNotIn("的", fragments)
        self.assertEqual(latin, [])

    def test_short_run_still_gets_sub_windows(self):
        # 红酒瓶 has to be able to hit a file named 汇立红酒文化.
        fragments, _ = query_terms("红酒瓶")
        self.assertEqual(fragments, ["红酒瓶", "红酒", "酒瓶"])

    def test_latin_words_are_kept_alongside_han(self):
        _, latin = query_terms("老钱风 OOTD")
        self.assertEqual(latin, ["ootd"])

    def test_literal_score_prefers_the_longer_fragment(self):
        fragments, latin = query_terms("穿西装的男人")
        strong = _literal_score("肌肉男穿西装不好看", fragments, latin)
        weak = _literal_score("西装店", fragments, latin)
        self.assertGreater(strong, weak)
        self.assertGreater(weak, 0.0)

    def test_literal_score_is_zero_without_a_match(self):
        fragments, latin = query_terms("红酒瓶")
        self.assertEqual(_literal_score("一只猫在睡觉", fragments, latin), 0.0)


class SearchByQueryTests(unittest.TestCase):
    ROWS = [
        {
            "image_id": "a" * 32,
            "image_path": "/tmp/穿西装的男人.jpg",
            "rel_path": "穿西装的男人.jpg",
            "library_root": "/tmp",
            "caption": "",
            "people_json": "[]",
            "tags_json": "[]",
        },
        {
            "image_id": "b" * 32,
            "image_path": "/tmp/猫.jpg",
            "rel_path": "猫.jpg",
            "library_root": "/tmp",
            "caption": "",
            "people_json": "[]",
            "tags_json": "[]",
        },
    ]

    def _run(self, query, *, vector_hits, top_k=10):
        from src.services import image_index_service as service
        from src.storage import image_lance_store as store

        original_iter = store.iter_image_rows
        original_vector = service.search_by_text
        store.iter_image_rows = lambda **kwargs: iter(self.ROWS)  # type: ignore[assignment]
        service.search_by_text = lambda *a, **k: list(vector_hits)  # type: ignore[assignment]
        try:
            return service.search_by_query(query, top_k=top_k)
        finally:
            store.iter_image_rows = original_iter  # type: ignore[assignment]
            service.search_by_text = original_vector  # type: ignore[assignment]

    @staticmethod
    def _vector_hit(image_id, score=0.3):
        return {
            "image_id": image_id,
            "image_path": f"/tmp/{image_id}.jpg",
            "rel_path": f"{image_id}.jpg",
            "library_root": "/tmp",
            "caption": "",
            "people": [],
            "tags": [],
            "people_count": 0,
            "score": score,
            "match": "vector",
        }

    def test_chinese_query_leads_with_literal_hits(self):
        hits = self._run("穿西装的男人", vector_hits=[self._vector_hit("b" * 32)])
        self.assertEqual(hits[0]["image_id"], "a" * 32)
        self.assertEqual(hits[0]["match"], "keyword")

    def test_literal_hits_are_not_padded_with_vectors(self):
        # Vector backfill would only add noise for a Chinese query.
        hits = self._run("穿西装的男人", vector_hits=[self._vector_hit("b" * 32)])
        self.assertEqual([hit["image_id"] for hit in hits], ["a" * 32])

    def test_vectors_are_used_when_nothing_matches_literally(self):
        hits = self._run("海边日落", vector_hits=[self._vector_hit("b" * 32)])
        self.assertEqual([hit["match"] for hit in hits], ["vector"])

    def test_english_query_skips_the_literal_scan(self):
        def _boom(**kwargs):
            raise AssertionError("literal scan must not run for an English query")

        from src.services import image_index_service as service
        from src.storage import image_lance_store as store

        original_iter = store.iter_image_rows
        original_vector = service.search_by_text
        store.iter_image_rows = _boom  # type: ignore[assignment]
        service.search_by_text = lambda *a, **k: [self._vector_hit("b" * 32)]  # type: ignore[assignment]
        try:
            hits = service.search_by_query("a man in a suit", top_k=5)
        finally:
            store.iter_image_rows = original_iter  # type: ignore[assignment]
            service.search_by_text = original_vector  # type: ignore[assignment]
        self.assertEqual([hit["match"] for hit in hits], ["vector"])

    def test_duplicate_ids_are_not_returned_twice(self):
        hits = self._run("穿西装的男人", vector_hits=[self._vector_hit("a" * 32), self._vector_hit("b" * 32)])
        ids = [hit["image_id"] for hit in hits]
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_query_returns_nothing(self):
        from src.services import image_index_service as service

        self.assertEqual(service.search_by_query("   "), [])


class PruneOfflineRootTests(unittest.TestCase):
    """An unplugged drive must not be mistaken for 1262 deleted photos."""

    def test_rows_under_an_offline_root_are_skipped(self):
        from src.services import image_index_service as service
        from src.storage import image_lance_store as store

        rows = [
            {"image_id": "a" * 32, "image_path": "/Volumes/GoneDrive/photos/x.jpg"},
            {"image_id": "b" * 32, "image_path": "/tmp/vs-definitely-missing.jpg"},
        ]
        original_iter = store.iter_image_rows
        original_manifest = service.load_manifest
        store.iter_image_rows = lambda **kwargs: iter(rows)  # type: ignore[assignment]
        service.load_manifest = lambda: {"roots": [{"root": "/Volumes/GoneDrive"}]}  # type: ignore[assignment]
        try:
            result = service.prune_missing_images(dry_run=True)
        finally:
            store.iter_image_rows = original_iter  # type: ignore[assignment]
            service.load_manifest = original_manifest  # type: ignore[assignment]
        self.assertEqual(result["skipped_offline"], 1)
        self.assertEqual(result["image_ids"], ["b" * 32])

    def test_nothing_is_skipped_when_every_root_is_online(self):
        from src.services import image_index_service as service
        from src.storage import image_lance_store as store

        rows = [{"image_id": "b" * 32, "image_path": "/tmp/vs-definitely-missing.jpg"}]
        original_iter = store.iter_image_rows
        original_manifest = service.load_manifest
        store.iter_image_rows = lambda **kwargs: iter(rows)  # type: ignore[assignment]
        service.load_manifest = lambda: {"roots": [{"root": "/tmp"}]}  # type: ignore[assignment]
        try:
            result = service.prune_missing_images(dry_run=True)
        finally:
            store.iter_image_rows = original_iter  # type: ignore[assignment]
            service.load_manifest = original_manifest  # type: ignore[assignment]
        self.assertEqual(result["skipped_offline"], 0)
        self.assertEqual(result["image_ids"], ["b" * 32])


class DescribeMarkerTests(unittest.TestCase):
    """describer records whether the reply parsed, so index() can retry.

    Without the marker a damaged row looks finished: it has a caption (the
    raw fragment), so the mtime check skips it forever, even after the parser
    learned to repair truncation.
    """

    def _describe(self, parsed):
        from src.services import image_index_service as service

        with (
            patch(
                "src.services.image_understanding.describe_image_bgr",
                return_value=parsed,
            ),
            patch(
                "src.services.understanding_resource_service.get_remote_vlm_settings",
                return_value={"model": "qwen3-vl:4b"},
            ),
        ):
            return service._describe_one(None, config=None, should_stop=None)

    def test_unparsed_reply_is_marked(self):
        row = self._describe({"caption": "x", "people": [], "tags": [], "parse_ok": False})
        self.assertEqual(row["describer"], "vlm:qwen3-vl:4b:unparsed")

    def test_parsed_reply_is_not_marked(self):
        row = self._describe({"caption": "x", "people": [], "tags": [], "parse_ok": True})
        self.assertEqual(row["describer"], "vlm:qwen3-vl:4b")

    def test_suffix_is_distinct_from_a_model_named_unparsed(self):
        from src.services.image_index_service import UNPARSED_SUFFIX

        self.assertEqual(UNPARSED_SUFFIX, ":unparsed")
        self.assertNotIn(UNPARSED_SUFFIX, "vlm:qwen3-vl:4b")


if __name__ == "__main__":
    unittest.main()
