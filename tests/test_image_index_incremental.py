"""Incremental behaviour of image indexing.

The question this file answers: when a photo is added to an already indexed
folder, what does ``index`` actually redo? Only the new or changed files --
an untouched photo must not be re-encoded or re-described, because that is
what makes a nightly ``index`` over a 10k-photo drive practical.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from src.services import image_index_service as service


class _Recorder:
    """Stands in for the CLIP encoder and the VLM so nothing heavy runs."""

    def __init__(self) -> None:
        self.encoded: list[str] = []
        self.described: list[str] = []

    def encode(self, image_bgr):
        return np.ones(4, dtype=np.float32)

    def describe(self, image_bgr, *, config, should_stop):
        self.described.append("x")
        return {
            "caption": "一张新照片",
            "people": [{"count": 1, "gender": "男", "build": "偏瘦", "style": "休闲"}],
            "tags": ["测试"],
            "parse_ok": True,
            "describer": "vlm:qwen3-vl:4b",
        }


class IncrementalIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.rows: dict[str, dict] = {}
        self.upserted: list[dict] = []
        self.hashed: list[str] = []
        self.recorder = _Recorder()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers --------------------------------------------------------
    def _make_photo(self, name: str) -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as handle:
            handle.write(name.encode("utf-8"))
        return path

    def _existing_row(self, path: str, *, caption: str = "旧描述", describer: str = "vlm:qwen3-vl:4b", mtime=None):
        stat = os.stat(path)
        return {
            "image_id": service.file_content_id(path),
            "image_path": path,
            "mtime": float(stat.st_mtime if mtime is None else mtime),
            "caption": caption,
            "describer": describer,
        }

    def _run(self, paths, *, describe=True, force=False, model="qwen3-vl:4b"):
        def fake_get_row(image_id, *, config=None, columns=None):
            return self.rows.get(image_id)

        def fake_iter_rows(*, config=None, columns=None, page_size=4096):
            keys = list(columns or [])
            for row in list(self.rows.values()):
                yield {key: row.get(key) for key in keys} if keys else dict(row)

        def fake_upsert(rows, *, config=None):
            for row in rows:
                self.rows[row["image_id"]] = dict(row)
                self.upserted.append(dict(row))
            return len(rows)

        def fake_describe(image_bgr, *, config, should_stop):
            self.recorder.described.append("x")
            return {
                "caption": "一张新照片",
                "people": [],
                "tags": ["测试"],
                "parse_ok": True,
            }

        real_content_id = service.file_content_id

        def spy_content_id(path):
            self.hashed.append(path)
            return real_content_id(path)

        with (
            patch("src.core.image_io.load_image_bgr", return_value=np.zeros((8, 8, 3), dtype=np.uint8)),
            patch.object(service, "_encode_image", side_effect=self.recorder.encode),
            patch.object(service, "_describe_one", side_effect=fake_describe),
            patch.object(service, "file_content_id", side_effect=spy_content_id),
            patch(
                "src.services.image_understanding.vlm_availability",
                return_value=(True, ""),
            ),
            patch(
                "src.services.understanding_resource_service.get_remote_vlm_settings",
                return_value={"model": model},
            ),
            patch(
                "src.storage.image_lance_store.iter_image_rows",
                side_effect=fake_iter_rows,
            ),
            patch(
                "src.storage.image_lance_store.get_image_row",
                side_effect=fake_get_row,
            ),
            patch(
                "src.storage.image_lance_store.upsert_image_rows",
                side_effect=fake_upsert,
            ),
        ):
            return service.index_images(
                list(paths),
                config={},
                describe=describe,
                force=force,
                root=self.root,
                concurrency=1,
            )

    # -- cases ---------------------------------------------------------
    def test_new_photo_is_encoded_and_described(self):
        path = self._make_photo("new.jpg")
        result = self._run([path])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(self.recorder.described), 1)

    def test_untouched_photo_is_skipped_entirely(self):
        path = self._make_photo("old.jpg")
        self.rows[service.file_content_id(path)] = self._existing_row(path)

        result = self._run([path])

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["updated"], 0)
        # Neither the encoder nor the VLM ran: this is the whole point.
        self.assertEqual(self.recorder.described, [])
        # ... and the file was not even read to prove it was unchanged. On a
        # 1262-photo external drive that saves ~23 s per index run.
        self.assertEqual(self.hashed, [])

    def test_new_photo_is_still_hashed_for_dedupe(self):
        path = self._make_photo("brand-new.jpg")

        self._run([path])

        self.assertEqual(self.hashed, [path])

    def test_touched_photo_is_redone(self):
        path = self._make_photo("edited.jpg")
        self.rows[service.file_content_id(path)] = self._existing_row(path, mtime=1.0)

        result = self._run([path])

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(self.recorder.described), 1)

    def test_vector_only_photo_gets_its_description_when_the_vlm_is_back(self):
        path = self._make_photo("clip-only.jpg")
        self.rows[service.file_content_id(path)] = self._existing_row(
            path, caption="", describer="clip-only"
        )

        result = self._run([path])

        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(self.recorder.described), 1)

    def test_unparsed_photo_is_retried_without_force(self):
        path = self._make_photo("broken.jpg")
        self.rows[service.file_content_id(path)] = self._existing_row(
            path, caption='{"caption": "半截', describer="vlm:qwen3-vl:4b:unparsed"
        )

        result = self._run([path])

        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(self.recorder.described), 1)

    def test_no_description_mode_leaves_existing_captions_alone(self):
        path = self._make_photo("plain.jpg")
        self.rows[service.file_content_id(path)] = self._existing_row(path)

        result = self._run([path], describe=False)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.recorder.described, [])

    def test_force_redoes_everything(self):
        path = self._make_photo("forced.jpg")
        self.rows[service.file_content_id(path)] = self._existing_row(path)

        result = self._run([path], force=True)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(self.recorder.described), 1)

    def test_photo_described_by_another_model_is_redone(self):
        # Upgrading 4b -> 8b and running index should rewrite the captions
        # without --force (which would also re-encode every vector).
        path = self._make_photo("upgraded.jpg")
        self.rows[service.file_content_id(path)] = self._existing_row(
            path, describer="vlm:qwen3-vl:8b"
        )

        result = self._run([path], model="qwen3-vl:4b")

        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(self.recorder.described), 1)


class DescriberStalenessTests(unittest.TestCase):
    """Switching the VLM should upgrade descriptions without --force.

    Skipping is keyed on mtime alone, so a library described by 4b stays on 4b
    forever unless the user knows to pass --force -- which re-encodes every
    vector as well, i.e. minutes of work for a text-only change.
    """

    def test_different_model_is_stale(self):
        self.assertTrue(service._describer_is_stale("vlm:qwen3-vl:8b", "vlm:qwen3-vl:4b"))

    def test_same_model_is_not_stale(self):
        self.assertFalse(service._describer_is_stale("vlm:qwen3-vl:4b", "vlm:qwen3-vl:4b"))

    def test_unparsed_suffix_does_not_hide_the_model(self):
        self.assertFalse(service._describer_is_stale("vlm:qwen3-vl:4b:unparsed", "vlm:qwen3-vl:4b"))
        self.assertTrue(service._describer_is_stale("vlm:qwen3-vl:8b:unparsed", "vlm:qwen3-vl:4b"))

    def test_clip_only_and_vlm_error_rows_are_not_stale(self):
        # Their captions are empty or broken, which the skip check already
        # redoes; treating them as stale would only duplicate that logic.
        self.assertFalse(service._describer_is_stale("clip-only", "vlm:qwen3-vl:4b"))
        self.assertFalse(service._describer_is_stale("vlm-error: HTTPError", "vlm:qwen3-vl:4b"))

    def test_no_configured_model_means_no_staleness(self):
        self.assertFalse(service._describer_is_stale("vlm:qwen3-vl:8b", ""))


if __name__ == "__main__":
    unittest.main()
