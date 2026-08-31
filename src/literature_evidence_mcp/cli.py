from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .errors import LiteratureEvidenceError
from .registry import LibraryRegistry, default_application_root
from .retrieval import search_snapshot
from .snapshot import build_snapshot, verify_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="literature-evidence",
        description="在本机建立并检索可审计的文献证据冻结快照。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="显式导入 Markdown/PDF，并建立一个全新的冻结快照。"
    )
    build.add_argument("--library", required=True, type=Path, help="本地资料库目录。")
    build.add_argument("sources", nargs="+", type=Path, help="要导入的 Markdown/PDF。")

    verify = subparsers.add_parser("verify", help="重新计算快照哈希、schema 和数量。")
    verify.add_argument("snapshot", type=Path, help="快照目录。")

    search = subparsers.add_parser("search", help="对一个已冻结快照执行本地 BM25 预览。")
    search.add_argument("snapshot", type=Path, help="快照目录。")
    search.add_argument("query", help="完整检索问题或关键词。")
    search.add_argument("--top-k", type=int, default=5, help="返回 1-10 条结果。")
    search.add_argument(
        "--excerpt-chars", type=int, default=1000, help="每条摘录 1-1200 字符。"
    )

    serve = subparsers.add_parser(
        "serve", help="在 127.0.0.1 启动本机管理页，按 Ctrl+C 停止。"
    )
    serve.add_argument("--library", required=True, type=Path, help="固定本地资料库目录。")
    serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="本机端口（1024-65535，默认 8765）。",
    )
    serve.add_argument(
        "--open-browser",
        action="store_true",
        help="端口成功绑定后打开默认浏览器。",
    )

    libraries = subparsers.add_parser(
        "libraries", help="在固定应用目录中显式管理多个本地资料库。"
    )
    library_actions = libraries.add_subparsers(
        dest="library_action", required=True
    )
    library_actions.add_parser("list", help="只读列出资料库和当前选择。")

    create = library_actions.add_parser("create", help="创建一个新的物理资料库。")
    create.add_argument("name", help="显示名称；允许与其他资料库同名。")
    create.add_argument("--description", default="", help="可选描述。")

    select = library_actions.add_parser("select", help="按稳定 ID 选择资料库。")
    select.add_argument("library_id", help="要选择的稳定 library_id。")

    rename = library_actions.add_parser("rename", help="修改显示名称，不改变 ID。")
    rename.add_argument("library_id", help="稳定 library_id。")
    rename.add_argument("name", help="新的显示名称。")

    describe = library_actions.add_parser(
        "describe", help="修改描述，不改变 ID。"
    )
    describe.add_argument("library_id", help="稳定 library_id。")
    describe.add_argument("description", help="新的描述；空字符串表示清空。")
    return parser


def _emit(payload: object) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_snapshot(args.library, args.sources)
        elif args.command == "verify":
            result = verify_snapshot(args.snapshot)
        elif args.command == "search":
            result = search_snapshot(
                args.snapshot,
                args.query,
                top_k=args.top_k,
                excerpt_chars=args.excerpt_chars,
            )
        elif args.command == "libraries":
            registry = LibraryRegistry(default_application_root())
            if args.library_action == "list":
                result = {"libraries": registry.list_libraries()}
            elif args.library_action == "create":
                result = {
                    "library": registry.create(
                        args.name, description=args.description
                    )
                }
            elif args.library_action == "select":
                result = {"library": registry.select(args.library_id)}
            elif args.library_action == "rename":
                result = {"library": registry.rename(args.library_id, args.name)}
            else:
                result = {
                    "library": registry.update_description(
                        args.library_id, args.description
                    )
                }
        else:
            from .web import serve_local

            serve_local(
                args.library,
                port=args.port,
                open_browser=args.open_browser,
            )
            return 0
    except LiteratureEvidenceError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"错误：本地文件操作失败（{exc.strerror or exc}）。\n")
        return 2
    except KeyboardInterrupt:
        return 130
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
