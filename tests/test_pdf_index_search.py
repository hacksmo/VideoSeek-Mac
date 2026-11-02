"""PDF library end-to-end: index a generated PDF, search its text back."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch


def _make_pdf(path: str, pages: list[str]) -> None:
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontname="china-s")
    doc.save(path)
    doc.close()


class PdfIndexSearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        # Point both the Lance dir and the manifest dir at the temp folder.
        self.patch_lance = patch(
            "src.storage.pdf_lance_store.get_pdf_lance_dir",
            return_value=os.path.join(self.root, "lance"),
        )
        self.patch_lance.start()
        self.patch_manifest = patch(
            "src.services.pdf_service.get_document_library_dir",
            return_value=os.path.join(self.root, "document_library"),
        )
        self.patch_manifest.start()

    def tearDown(self):
        self.patch_lance.stop()
        self.patch_manifest.stop()
        self._tmp.cleanup()

    def test_index_then_search_finds_pdf_page(self):
        from src.services import pdf_service as service

        pdf_path = os.path.join(self.root, "report.pdf")
        _make_pdf(pdf_path, ["资产负债表", "2024 年度报告 income statement"])

        result = service.index_documents([pdf_path])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["pages"], 2)

        hits = service.search_by_text("资产负债表")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["page_no"], 1)
        self.assertIn("资产负债表", hits[0]["text"])

        # English keyword search works too.
        hits_en = service.search_by_text("income")
        self.assertEqual(len(hits_en), 1)
        self.assertEqual(hits_en[0]["page_no"], 2)

    def test_search_no_match_is_empty(self):
        from src.services import pdf_service as service

        pdf_path = os.path.join(self.root, "report.pdf")
        _make_pdf(pdf_path, ["资产负债表"])
        service.index_documents([pdf_path])

        self.assertEqual(service.search_by_text("不存在的词"), [])

    def test_register_and_scan_root(self):
        from src.services import pdf_service as service

        sub = os.path.join(self.root, "docs")
        os.makedirs(sub)
        _make_pdf(os.path.join(sub, "a.pdf"), ["第一份"])
        _make_pdf(os.path.join(sub, "b.pdf"), ["第二份"])

        service.add_library_root(sub)
        files = service.scan_manifest_roots()
        self.assertEqual(len(files), 2)


if __name__ == "__main__":
    unittest.main()
