"""PDF document library: format whitelist, fragment split, scoring, scanning."""

from __future__ import annotations

import os
import tempfile
import unittest

from src.media.formats import is_supported_pdf_path
from src.services.pdf_service import (
    _literal_score,
    _query_fragments,
    _snippet,
    file_content_id,
    scan_pdf_files,
)


class PdfFormatTests(unittest.TestCase):
    def test_pdf_extensions_detected_case_insensitively(self):
        for name in ("a.pdf", "b.PDF"):
            with self.subTest(name=name):
                self.assertTrue(is_supported_pdf_path(name))

    def test_non_pdf_are_not_documents(self):
        self.assertFalse(is_supported_pdf_path("a.jpg"))
        self.assertFalse(is_supported_pdf_path("a.mp4"))
        self.assertFalse(is_supported_pdf_path("a.txt"))


class PdfFragmentTests(unittest.TestCase):
    def test_chinese_fragments_split_into_windows(self):
        fragments, latin = _query_fragments("资产负债表")
        # A 5-char word becomes 2-4 char sliding windows.
        self.assertIn("资产负债", fragments)
        self.assertIn("资产", fragments)
        self.assertEqual(latin, [])

    def test_latin_words_are_extracted(self):
        fragments, latin = _query_fragments("report 2024")
        self.assertEqual(fragments, [])
        self.assertEqual(latin, ["report", "2024"])

    def test_stop_chars_are_dropped(self):
        fragments, _ = _query_fragments("合同和协议")
        self.assertNotIn("和", fragments)

    def test_empty_query_yields_nothing(self):
        self.assertEqual(_query_fragments(""), ([], []))


class PdfLiteralScoreTests(unittest.TestCase):
    def test_longer_match_scores_higher(self):
        frag_a, latin_a = _query_fragments("资产负债表")
        frag_b, latin_b = _query_fragments("资产")
        haystack = "本页包含资产负债表内容".casefold()
        self.assertGreater(
            _literal_score(haystack, frag_a, latin_a),
            _literal_score(haystack, frag_b, latin_b),
        )

    def test_no_match_is_zero(self):
        frag, latin = _query_fragments("资产负债表")
        self.assertEqual(_literal_score("完全无关的文字".casefold(), frag, latin), 0.0)


class PdfSnippetTests(unittest.TestCase):
    def test_snippet_centers_on_match(self):
        text = "这是一段很长的开头文字，然后提到了资产负债表，接着是结尾。"
        frag, latin = _query_fragments("资产负债表")
        snippet = _snippet(text, frag, latin)
        self.assertIn("资产负债表", snippet)


class PdfScanTests(unittest.TestCase):
    def test_scan_finds_pdfs_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = os.path.join(tmp, "sub")
            os.makedirs(sub)
            open(os.path.join(tmp, "a.pdf"), "w").close()
            open(os.path.join(sub, "b.PDF"), "w").close()
            open(os.path.join(tmp, "c.jpg"), "w").close()
            found = scan_pdf_files(tmp)
            self.assertEqual(len(found), 2)

    def test_scan_skips_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, ".hidden.pdf"), "w").close()
            found = scan_pdf_files(tmp)
            self.assertEqual(found, [])


class PdfContentIdTests(unittest.TestCase):
    def test_content_id_is_stable_and_different(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = os.path.join(tmp, "a.pdf")
            p2 = os.path.join(tmp, "b.pdf")
            with open(p1, "wb") as f:
                f.write(b"hello")
            with open(p2, "wb") as f:
                f.write(b"world")
            self.assertEqual(file_content_id(p1), file_content_id(p1))
            self.assertNotEqual(file_content_id(p1), file_content_id(p2))

    def test_missing_file_is_empty(self):
        self.assertEqual(file_content_id("/no/such/file.pdf"), "")


if __name__ == "__main__":
    unittest.main()
