from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from . import __version__
from .errors import LiteratureEvidenceError, SearchInputError, SnapshotError
from .mcp_tools import ReadOnlyEvidenceTools


_READ_ONLY_CLOSED = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    open_world_hint=False,
)

_LIBRARY_ID_SCHEMA = {
    "type": "string",
    "pattern": "^lib_[0-9a-f]{32}$",
    "minLength": 36,
    "maxLength": 36,
    "description": "A stable library_id returned by retrieval_status.",
}
_SNAPSHOT_ID_SCHEMA = {
    "type": "string",
    "pattern": "^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}-[0-9a-f]{8}$",
    "minLength": 44,
    "maxLength": 44,
    "description": "A snapshot_id returned for the explicit library_id.",
}
_DOCUMENT_ID_SCHEMA = {
    "type": "string",
    "pattern": "^doc_[0-9a-f]{24}$",
    "minLength": 28,
    "maxLength": 28,
    "description": "A document_id from the selected snapshot.",
}
_CHUNK_ID_SCHEMA = {
    "type": "string",
    "pattern": "^chunk_[0-9a-f]{24}$",
    "minLength": 30,
    "maxLength": 30,
    "description": "A chunk_id from the selected document and snapshot.",
}
_SECTION_ID_SCHEMA = {
    "type": "string",
    "pattern": "^sec_[0-9a-f]{24}$",
    "minLength": 28,
    "maxLength": 28,
    "description": "A section_id returned by get_document_toc for this snapshot.",
}
_QUERY_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 400,
    "description": "A complete local lexical query; no path or URI.",
}


def _input_schema(
    properties: dict[str, dict[str, Any]], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOLS = (
    Tool(
        name="search_documents",
        description=(
            "Search one verified local snapshot with the existing SQLite FTS5/BM25 "
            "ranking. A real empty result does not prove corpus absence."
        ),
        input_schema=_input_schema(
            {
                "library_id": _LIBRARY_ID_SCHEMA,
                "snapshot_id": _SNAPSHOT_ID_SCHEMA,
                "query": _QUERY_SCHEMA,
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
                "excerpt_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1200,
                    "default": 1000,
                },
            },
            ["library_id", "snapshot_id", "query"],
        ),
        annotations=_READ_ONLY_CLOSED,
    ),
    Tool(
        name="get_excerpt",
        description=(
            "Return one bounded evidence excerpt after verifying its snapshot, "
            "document_id, and chunk_id association."
        ),
        input_schema=_input_schema(
            {
                "library_id": _LIBRARY_ID_SCHEMA,
                "snapshot_id": _SNAPSHOT_ID_SCHEMA,
                "document_id": _DOCUMENT_ID_SCHEMA,
                "chunk_id": _CHUNK_ID_SCHEMA,
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1200,
                    "default": 600,
                },
            },
            ["library_id", "snapshot_id", "document_id", "chunk_id"],
        ),
        annotations=_READ_ONLY_CLOSED,
    ),
    Tool(
        name="get_multiple_excerpts",
        description=(
            "Return one to five bounded evidence excerpts in requested chunk_id order; "
            "all IDs must belong to the selected document and snapshot."
        ),
        input_schema=_input_schema(
            {
                "library_id": _LIBRARY_ID_SCHEMA,
                "snapshot_id": _SNAPSHOT_ID_SCHEMA,
                "document_id": _DOCUMENT_ID_SCHEMA,
                "chunk_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                    "items": _CHUNK_ID_SCHEMA,
                },
                "per_item_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1200,
                    "default": 600,
                },
            },
            ["library_id", "snapshot_id", "document_id", "chunk_ids"],
        ),
        annotations=_READ_ONLY_CLOSED,
    ),
    Tool(
        name="get_document_metadata",
        description=(
            "Return path-free document and asset metadata from one verified local snapshot."
        ),
        input_schema=_input_schema(
            {
                "library_id": _LIBRARY_ID_SCHEMA,
                "snapshot_id": _SNAPSHOT_ID_SCHEMA,
                "document_id": _DOCUMENT_ID_SCHEMA,
            },
            ["library_id", "snapshot_id", "document_id"],
        ),
        annotations=_READ_ONLY_CLOSED,
    ),
    Tool(
        name="get_document_toc",
        description=(
            "Return bounded navigation derived from indexed Markdown headings or PDF "
            "pages; it does not claim to be an author-provided table of contents."
        ),
        input_schema=_input_schema(
            {
                "library_id": _LIBRARY_ID_SCHEMA,
                "snapshot_id": _SNAPSHOT_ID_SCHEMA,
                "document_id": _DOCUMENT_ID_SCHEMA,
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 100,
                },
            },
            ["library_id", "snapshot_id", "document_id"],
        ),
        annotations=_READ_ONLY_CLOSED,
    ),
    Tool(
        name="read_document_section",
        description=(
            "Read at most 1200 characters from a section_id previously returned for "
            "the same snapshot and document."
        ),
        input_schema=_input_schema(
            {
                "library_id": _LIBRARY_ID_SCHEMA,
                "snapshot_id": _SNAPSHOT_ID_SCHEMA,
                "document_id": _DOCUMENT_ID_SCHEMA,
                "section_id": _SECTION_ID_SCHEMA,
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1200,
                    "default": 1200,
                },
            },
            ["library_id", "snapshot_id", "document_id", "section_id"],
        ),
        annotations=_READ_ONLY_CLOSED,
    ),
    Tool(
        name="find_in_document",
        description=(
            "Search inside one document using the same local SQLite FTS5/BM25 "
            "expression and deterministic tie order as search_documents."
        ),
        input_schema=_input_schema(
            {
                "library_id": _LIBRARY_ID_SCHEMA,
                "snapshot_id": _SNAPSHOT_ID_SCHEMA,
                "document_id": _DOCUMENT_ID_SCHEMA,
                "query": _QUERY_SCHEMA,
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
                "excerpt_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1200,
                    "default": 600,
                },
            },
            ["library_id", "snapshot_id", "document_id", "query"],
        ),
        annotations=_READ_ONLY_CLOSED,
    ),
    Tool(
        name="retrieval_status",
        description=(
            "List libraries, list one library's successful snapshots, or verify one "
            "explicit library_id and snapshot_id pair."
        ),
        input_schema=_input_schema(
            {
                "library_id": _LIBRARY_ID_SCHEMA,
                "snapshot_id": _SNAPSHOT_ID_SCHEMA,
            },
            [],
        ),
        annotations=_READ_ONLY_CLOSED,
    ),
)

_REQUIRED_ARGUMENTS = {
    "search_documents": {"library_id", "snapshot_id", "query"},
    "get_excerpt": {"library_id", "snapshot_id", "document_id", "chunk_id"},
    "get_multiple_excerpts": {
        "library_id",
        "snapshot_id",
        "document_id",
        "chunk_ids",
    },
    "get_document_metadata": {"library_id", "snapshot_id", "document_id"},
    "get_document_toc": {"library_id", "snapshot_id", "document_id"},
    "read_document_section": {
        "library_id",
        "snapshot_id",
        "document_id",
        "section_id",
    },
    "find_in_document": {"library_id", "snapshot_id", "document_id", "query"},
    "retrieval_status": set(),
}
_OPTIONAL_ARGUMENTS = {
    "search_documents": {"top_k", "excerpt_chars"},
    "get_excerpt": {"max_chars"},
    "get_multiple_excerpts": {"per_item_chars"},
    "get_document_metadata": set(),
    "get_document_toc": {"max_items"},
    "read_document_section": {"max_chars"},
    "find_in_document": {"top_k", "excerpt_chars"},
    "retrieval_status": {"library_id", "snapshot_id"},
}


def _strict_arguments(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    values = {} if arguments is None else arguments
    required = _REQUIRED_ARGUMENTS[name]
    allowed = required | _OPTIONAL_ARGUMENTS[name]
    extra = set(values) - allowed
    missing = required - set(values)
    if extra:
        raise SearchInputError("调用包含未声明参数。")
    if missing:
        raise SearchInputError("调用缺少必要参数。")
    return values


def _success(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        ]
    )


def _error(code: str, message: str) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": {"code": code, "message": message}},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        ],
        is_error=True,
    )


def create_server(service: ReadOnlyEvidenceTools) -> Server[object]:
    async def list_tools(
        _context: ServerRequestContext[object],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=list(TOOLS))

    async def call_tool(
        _context: ServerRequestContext[object],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        if params.name not in _REQUIRED_ARGUMENTS:
            return _error("LEMCP_E_UNKNOWN_TOOL", "未知工具。")
        try:
            arguments = _strict_arguments(params.name, params.arguments)
            if params.name == "search_documents":
                payload = service.search_documents(
                    arguments["library_id"],
                    arguments["snapshot_id"],
                    arguments["query"],
                    top_k=arguments.get("top_k", 5),
                    excerpt_chars=arguments.get("excerpt_chars", 1000),
                )
            elif params.name == "get_excerpt":
                payload = service.get_excerpt(
                    arguments["library_id"],
                    arguments["snapshot_id"],
                    arguments["document_id"],
                    arguments["chunk_id"],
                    max_chars=arguments.get("max_chars", 600),
                )
            elif params.name == "get_multiple_excerpts":
                payload = service.get_multiple_excerpts(
                    arguments["library_id"],
                    arguments["snapshot_id"],
                    arguments["document_id"],
                    arguments["chunk_ids"],
                    per_item_chars=arguments.get("per_item_chars", 600),
                )
            elif params.name == "get_document_metadata":
                payload = service.get_document_metadata(
                    arguments["library_id"],
                    arguments["snapshot_id"],
                    arguments["document_id"],
                )
            elif params.name == "get_document_toc":
                payload = service.get_document_toc(
                    arguments["library_id"],
                    arguments["snapshot_id"],
                    arguments["document_id"],
                    max_items=arguments.get("max_items", 100),
                )
            elif params.name == "read_document_section":
                payload = service.read_document_section(
                    arguments["library_id"],
                    arguments["snapshot_id"],
                    arguments["document_id"],
                    arguments["section_id"],
                    max_chars=arguments.get("max_chars", 1200),
                )
            elif params.name == "find_in_document":
                payload = service.find_in_document(
                    arguments["library_id"],
                    arguments["snapshot_id"],
                    arguments["document_id"],
                    arguments["query"],
                    top_k=arguments.get("top_k", 5),
                    excerpt_chars=arguments.get("excerpt_chars", 600),
                )
            else:
                payload = service.retrieval_status(
                    arguments.get("library_id"), arguments.get("snapshot_id")
                )
        except SearchInputError as exc:
            return _error("LEMCP_E_INVALID_INPUT", str(exc))
        except SnapshotError:
            return _error(
                "LEMCP_E_SNAPSHOT",
                "所选快照不存在、与 library_id 不匹配，或未通过只读核验。",
            )
        except LiteratureEvidenceError:
            return _error("LEMCP_E_LOCAL", "本地只读检索失败。")
        except Exception:
            return _error("LEMCP_E_INTERNAL", "本地只读工具发生内部错误。")
        return _success(payload)

    return Server(
        "literature-evidence-mcp",
        version=__version__,
        description="Closed-world read-only literature evidence retrieval over stdio.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def _serve_stdio(server: Server[object]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="literature-evidence-mcp",
        description="以本地 stdio 暴露注册资料库的八个只读 MCP 工具。",
    )
    parser.add_argument(
        "--application-root",
        type=Path,
        help="固定应用目录；省略时使用当前用户的标准 Application Support 目录。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        service = ReadOnlyEvidenceTools(args.application_root)
        server = create_server(service)
        asyncio.run(_serve_stdio(server))
    except (LiteratureEvidenceError, OSError, ValueError):
        sys.stderr.write("错误：本地资料库注册表无法安全启动只读 MCP 服务。\n")
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["TOOLS", "create_server", "main"]
