"""Document (PDF) library CLI: scan, index and search PDF text.

Phase 1 indexes PDF page text only (PyMuPDF); page-image vectors arrive later.
Rows live in the ``pdf_documents`` / ``pdf_pages`` Lance tables and never touch
the video or image index.

Examples:
    python scripts/pdf_index.py add ~/Documents/reports
    python scripts/pdf_index.py scan
    python scripts/pdf_index.py index
    python scripts/pdf_index.py search "资产负债表" --top-k 10
    python scripts/pdf_index.py status
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _find_repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    starts = [os.path.dirname(here), here, os.getcwd()]
    for start in starts:
        current = os.path.abspath(start)
        for _ in range(8):
            if os.path.isfile(os.path.join(current, "src", "app", "config.py")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return ""


def _ensure_repo_on_path() -> bool:
    root = _find_repo_root()
    if not root:
        return False
    if root not in sys.path:
        sys.path.insert(0, root)
    return True


def _print_hits(rows: list[dict], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("没有命中。")
        return
    for index, row in enumerate(rows, start=1):
        path = row.get("rel_path") or row.get("pdf_path") or ""
        print(f"[{index}] {row.get('score', 0):.3f}  {path}  第 {row.get('page_no', 1)} 页")
        snippet = str(row.get("snippet") or "").strip()
        if snippet:
            print(f"    {snippet}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Document (PDF) library CLI")
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="register a folder")
    p_add.add_argument("root", help="folder containing PDFs")

    sub.add_parser("scan", help="list PDFs under registered folders")

    p_index = sub.add_parser("index", help="extract text and index")
    p_index.add_argument("--root", default="", help="optional single folder to index")

    p_search = sub.add_parser("search", help="literal text search")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=50)

    sub.add_parser("status", help="show document / page counts")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    if not _ensure_repo_on_path():
        print("无法定位仓库根目录（缺少 src/app/config.py）。", file=sys.stderr)
        return 1

    from src.services import pdf_service as service

    if args.command == "add":
        try:
            manifest = service.add_library_root(args.root)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        print(f"已登记 {len(manifest.get('roots', []))} 个目录")
        return 0

    if args.command == "scan":
        files = service.scan_manifest_roots()
        print(f"发现 {len(files)} 个 PDF")
        for path in files:
            print(f"  {path}")
        return 0

    if args.command == "index":
        files = service.scan_manifest_roots()
        if args.root:
            files = service.scan_pdf_files(args.root)

        def progress(done, total, path):
            name = os.path.basename(path or "")
            print(f"  [{done}/{total}] {name}", file=sys.stderr)

        result = service.index_documents(files, progress=progress)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "search":
        hits = service.search_by_text(args.query, top_k=args.top_k)
        _print_hits(hits, as_json=False)
        return 0

    if args.command == "status":
        status = service.get_document_library_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
