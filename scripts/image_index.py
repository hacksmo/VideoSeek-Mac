"""Image library CLI: scan, index and search still images.

Additive to the video pipeline — rows live in the ``image_assets`` Lance table
and the video index is never touched. Descriptions reuse the VLM configured in
Settings → 理解 → 模型服务 (LM Studio / Ollama / any OpenAI-compatible server);
without a VLM only CLIP vectors are stored and semantic search still works.

Examples:
    python scripts/image_index.py add ~/Pictures/travel
    python scripts/image_index.py scan
    python scripts/image_index.py index
    python scripts/image_index.py search "穿红裙子的女人" --top-k 10
    python scripts/image_index.py search "红裙" --mode keyword
    python scripts/image_index.py search --mode image --image query.jpg
    python scripts/image_index.py status
    python scripts/image_index.py prune
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


def _print_rows(rows: list[dict], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("没有命中。")
        return
    for index, row in enumerate(rows, start=1):
        people = row.get("people") or []
        people_text = ""
        if people:
            bits = []
            for person in people:
                text = " ".join(
                    str(person.get(field) or "")
                    for field in ("gender", "age", "clothing", "action", "position")
                    if person.get(field)
                ).strip()
                if text:
                    bits.append(f"{person.get('count', 1)}×{text}")
            people_text = " / ".join(bits)
        print(f"[{index}] {row.get('score', 0):.3f}  {row.get('rel_path') or row.get('image_path')}")
        caption = str(row.get("caption") or "").strip()
        if caption:
            print(f"    描述: {caption}")
        if people_text:
            print(f"    人物: {people_text}")
        tags = row.get("tags") or []
        if tags:
            print(f"    标签: {' · '.join(str(tag) for tag in tags)}")
        print(f"    路径: {row.get('image_path')}")
        print()


def _cmd_add(args) -> int:
    from src.services import image_index_service as service

    for folder in args.folders:
        try:
            service.add_library_root(folder)
            print(f"已加入图片库: {os.path.normpath(os.path.abspath(os.path.expanduser(folder)))}")
        except ValueError as exc:
            print(f"跳过 {folder}: {exc}")
    return 0


def _cmd_remove(args) -> int:
    from src.services import image_index_service as service

    service.remove_library_root(args.folder)
    print("已移除（索引行保留，如需清理请运行 prune）")
    return 0


def _cmd_roots(args) -> int:
    from src.services import image_index_service as service

    manifest = service.load_manifest()
    roots = manifest.get("roots") or []
    if not roots:
        print("图片库为空。用 `add <folder>` 添加目录。")
        return 0
    for entry in roots:
        print(f"{entry.get('root')}  (added {entry.get('count', 0)})")
    return 0


def _cmd_scan(args) -> int:
    from src.services import image_index_service as service

    if args.root:
        files = service.scan_image_files(args.root, recursive=not args.no_recursive)
    else:
        files = service.scan_manifest_roots(recursive=not args.no_recursive)
    print(f"发现 {len(files)} 张图片。")
    if args.preview:
        for path in files[: args.preview]:
            print(f"  {path}")
    return 0


def _cmd_index(args) -> int:
    from src.services import image_index_service as service

    if args.root:
        files = service.scan_image_files(args.root, recursive=not args.no_recursive)
        root = os.path.normpath(os.path.abspath(args.root))
    else:
        files = service.scan_manifest_roots(recursive=not args.no_recursive)
        root = ""
    if args.limit:
        files = files[: int(args.limit)]
    if not files:
        print("没有需要索引的图片。")
        return 0

    print(f"开始索引 {len(files)} 张图片（描述={'开' if not args.no_describe else '关'}）...")

    def _progress(done: int, total: int, path: str) -> None:
        if args.quiet:
            return
        sys.stdout.write(f"\r[{done}/{total}] {os.path.basename(path)[:48]:<48}")
        sys.stdout.flush()

    result = service.index_images(
        files,
        describe=not args.no_describe,
        force=args.force,
        root=root,
        concurrency=args.concurrency,
        progress=_progress,
    )
    if not args.quiet:
        print()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("describe_reason"):
        print(f"提示: {result['describe_reason']}")
    return 0 if result.get("failed", 0) == 0 else 1


def _cmd_search(args) -> int:
    from src.services import image_index_service as service

    mode = args.mode
    if mode == "image" or args.image:
        if not args.image:
            print("--mode image 需要同时给出 --image <路径>")
            return 2
        rows = service.search_by_image(args.image, top_k=args.top_k, min_score=args.min_score)
    elif mode == "keyword":
        rows = service.search_by_keyword(
            " ".join(args.query or []),
            top_k=args.top_k,
            min_people=args.min_people,
            max_people=args.max_people,
        )
    else:
        query = " ".join(args.query or "").strip()
        if not query:
            print("请输入查询词，或用 --mode image --image <路径>")
            return 2
        # Chinese queries must not go straight to CLIP: the bundled text tower
        # is English-only, so search_by_query matches literal text first and
        # only backfills with vectors.
        rows = service.search_by_query(
            query,
            top_k=args.top_k,
            min_people=args.min_people,
            max_people=args.max_people,
            min_score=args.min_score,
        )
    _print_rows(rows, as_json=args.json)
    return 0


def _cmd_status(args) -> int:
    from src.services import image_index_service as service

    status = service.get_image_library_status()
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    print(f"图片库目录 : {service.get_image_library_dir()}")
    print(f"索引表目录 : {status.get('lance_dir') or '-'}")
    print(f"已索引     : {status.get('indexed', 0)} 张（其中 {status.get('described', 0)} 张有描述）")
    print(f"向量维度   : {status.get('dimension') or '-'}")
    print(f"VLM 就绪   : {'是' if status.get('vlm_ready') else '否'} {status.get('vlm_reason') or ''}")
    roots = status.get("roots") or []
    print(f"已登记目录 : {len(roots)}")
    for entry in roots:
        print(f"  - {entry.get('root')}")
    if status.get("error"):
        print(f"错误: {status['error']}")
    return 0


def _cmd_prune(args) -> int:
    from src.services import image_index_service as service

    result = service.prune_missing_images(dry_run=args.dry_run)
    missing = int(result.get("missing") or 0)
    if not missing:
        print("没有失效记录。")
        return 0
    if args.dry_run:
        print(f"将删除 {missing} 条失效记录（--dry-run，未执行）")
        return 0
    print(f"已删除 {result.get('removed', 0)} 条失效记录。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VideoSeek 图片库：扫描 / 索引 / 检索（附带人物描述）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="登记图片目录")
    add.add_argument("folders", nargs="+")
    add.set_defaults(func=_cmd_add)

    remove = sub.add_parser("remove", help="取消登记目录")
    remove.add_argument("folder")
    remove.set_defaults(func=_cmd_remove)

    roots = sub.add_parser("roots", help="列出已登记目录")
    roots.set_defaults(func=_cmd_roots)

    scan = sub.add_parser("scan", help="统计待索引图片")
    scan.add_argument("--root", default="")
    scan.add_argument("--no-recursive", action="store_true")
    scan.add_argument("--preview", type=int, default=10)
    scan.set_defaults(func=_cmd_scan)

    index = sub.add_parser("index", help="索引图片")
    index.add_argument("--root", default="", help="只索引该目录（默认用已登记目录）")
    index.add_argument("--no-recursive", action="store_true")
    index.add_argument("--no-describe", action="store_true", help="只算 CLIP 向量，不调 VLM")
    index.add_argument("--force", action="store_true", help="忽略 mtime，强制重算")
    index.add_argument("--concurrency", type=int, default=2, help="VLM 并发（1-4）")
    index.add_argument("--limit", type=int, default=0)
    index.add_argument("--quiet", action="store_true")
    index.set_defaults(func=_cmd_index)

    search = sub.add_parser("search", help="检索图片")
    search.add_argument("query", nargs="*")
    search.add_argument("--mode", choices=["text", "keyword", "image"], default="text")
    search.add_argument("--image", default="")
    search.add_argument("--top-k", type=int, default=20)
    search.add_argument("--min-score", type=float, default=0.0)
    search.add_argument("--min-people", type=int, default=0, help="仅 keyword 模式：最少人数")
    search.add_argument("--max-people", type=int, default=0, help="仅 keyword 模式：最多人数")
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=_cmd_search)

    status = sub.add_parser("status", help="查看索引状态")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

    prune = sub.add_parser("prune", help="删除文件已不存在的记录")
    prune.add_argument("--dry-run", action="store_true")
    prune.set_defaults(func=_cmd_prune)

    return parser


def main(argv: list[str] | None = None) -> int:
    if not _ensure_repo_on_path():
        print("Could not locate the VideoSeek source tree. Run this script from the repository.")
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
