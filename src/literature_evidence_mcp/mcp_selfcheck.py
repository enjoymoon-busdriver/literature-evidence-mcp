from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from .library import FixedLibrary
from .registry import LibraryRegistry


SERVER_NAME = "literature-evidence"
SERVER_COMMAND = "literature-evidence-mcp"
SELF_CHECK_INTENT = "mcp-self-check"
_QUERY = "stage8localmarker"
_SAFE_FAILURE = "此步骤未通过；自检已停止，未重试。"
_TOOL_NAMES = (
    "search_documents",
    "get_excerpt",
    "get_multiple_excerpts",
    "get_document_metadata",
    "get_document_toc",
    "read_document_section",
    "find_in_document",
    "retrieval_status",
)


def local_mcp_guide() -> dict[str, Any]:
    """Return copy-only, path-free local MCP setup text."""
    return {
        "state": "copy_only_not_configured",
        "server_name": SERVER_NAME,
        "cli": f"codex mcp add {SERVER_NAME} -- {SERVER_COMMAND}",
        "toml": (
            f"[mcp_servers.{SERVER_NAME}]\n"
            f'command = "{SERVER_COMMAND}"\n'
        ),
        "api_key_required": False,
        "tool_count": len(_TOOL_NAMES),
        "local_clients": ["ChatGPT desktop", "Codex CLI", "Codex IDE extension"],
        "web_boundary_zh": (
            "ChatGPT web 不会读取本机 Codex 配置；远程插件或 Tunnel 留到 Stage 9。"
        ),
    }


def _tree_identity(root: Path) -> dict[str, tuple[str, str]]:
    identity: dict[str, tuple[str, str]] = {}
    if not root.exists():
        return identity
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            identity[relative] = ("directory", "")
        elif stat.S_ISREG(status.st_mode):
            identity[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        elif stat.S_ISLNK(status.st_mode):
            identity[relative] = ("symlink", "")
        else:
            identity[relative] = ("other", "")
    return identity


def _payload(result: Any) -> dict[str, Any]:
    if result.is_error or len(result.content) != 1:
        raise RuntimeError("MCP tool failed")
    content = result.content[0]
    if not isinstance(content, TextContent):
        raise RuntimeError("MCP tool returned an unexpected content type")
    try:
        payload = json.loads(content.text)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeError("MCP tool returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool returned a non-object")
    return payload


def _tool_contract(tools: list[Any]) -> None:
    if [tool.name for tool in tools] != list(_TOOL_NAMES):
        raise RuntimeError("MCP tool set mismatch")
    if len(tools) != len(_TOOL_NAMES):
        raise RuntimeError("MCP tool count mismatch")
    for tool in tools:
        annotations = tool.annotations
        if (
            annotations is None
            or annotations.read_only_hint is not True
            or annotations.destructive_hint is not False
            or annotations.open_world_hint is not (tool.name == "search_documents")
        ):
            raise RuntimeError("MCP tool annotations mismatch")


def _base_report() -> dict[str, Any]:
    return {
        "passed": False,
        "scope": "local_stdio_self_check",
        "evidence_level": "offline_local_stdio",
        "message": "本地离线自检尚未完成；这不代表客户端已经配置。",
        "tool_count": None,
        "network_calls": 0,
        "model_calls": 0,
        "api_keys_used": 0,
        "external_config_writes": 0,
        "retry_count": 0,
        "steps": [],
    }


async def _run_stdio_self_check() -> dict[str, Any]:
    report = _base_report()
    current_step = "准备临时合成资料库"
    try:
        with tempfile.TemporaryDirectory(prefix="lemcp-stage8-selfcheck-") as raw:
            temporary = Path(raw)
            application_root = temporary / "application"
            source = temporary / "stage8-evidence.md"
            source.write_text(
                "# Stage 8 local evidence\n\n"
                "## Read-only route\n\n"
                "stage8localmarker proves the local stdio evidence route.\n",
                encoding="utf-8",
            )
            registry = LibraryRegistry(application_root)
            record = registry.create("Stage 8 synthetic self-check")
            library_id = record["library_id"]
            built = FixedLibrary(
                Path(record["library_root"]), library_id=library_id
            ).build([source])
            snapshot_id = built["snapshot_id"]
            before = _tree_identity(application_root)
            report["steps"].append(
                {
                    "name": current_step,
                    "passed": True,
                    "message": "已在临时目录建立合成快照；未读取用户资料库。",
                }
            )

            current_step = "启动真实本地 STDIO MCP 并核验八个工具"
            source_root = Path(__file__).resolve().parents[1]
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "literature_evidence_mcp.mcp_server",
                    "--application-root",
                    str(application_root),
                ],
                cwd=temporary,
                env={
                    "PYTHONPATH": str(source_root),
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
                async with asyncio.timeout(30):
                    async with Client(
                        stdio_client(params, errlog=errlog)
                    ) as client:
                        listed = await client.list_tools()
                        _tool_contract(listed.tools)
                        report["tool_count"] = len(listed.tools)
                        report["steps"].append(
                            {
                                "name": current_step,
                                "passed": True,
                                "message": (
                                    "真实 STDIO 子进程返回恰好八个工具；均只读，"
                                    "仅 search_documents 标注可能访问开放世界。"
                                ),
                            }
                        )

                        current_step = "核验明确资料库与冻结快照状态"
                        status = _payload(
                            await client.call_tool(
                                "retrieval_status",
                                {
                                    "library_id": library_id,
                                    "snapshot_id": snapshot_id,
                                },
                            )
                        )
                        if (
                            status.get("library_id") != library_id
                            or status.get("snapshot_id") != snapshot_id
                            or status.get("verified") is not True
                            or status.get("transport") != "stdio"
                        ):
                            raise RuntimeError("MCP status identity mismatch")
                        report["steps"].append(
                            {
                                "name": current_step,
                                "passed": True,
                                "message": "STDIO 工具确认合成快照已发布且实时核验通过。",
                            }
                        )

                        current_step = "执行默认与显式 BM25 搜索"
                        arguments = {
                            "library_id": library_id,
                            "snapshot_id": snapshot_id,
                            "query": _QUERY,
                            "top_k": 1,
                            "excerpt_chars": 160,
                        }
                        implicit = _payload(
                            await client.call_tool("search_documents", arguments)
                        )
                        explicit = _payload(
                            await client.call_tool(
                                "search_documents", {**arguments, "mode": "bm25"}
                            )
                        )
                        if implicit != explicit or implicit.get("found") is not True:
                            raise RuntimeError("BM25 self-check mismatch")
                        results = implicit.get("results")
                        if not isinstance(results, list) or len(results) != 1:
                            raise RuntimeError("BM25 self-check result mismatch")
                        evidence = results[0]
                        if not isinstance(evidence, dict):
                            raise RuntimeError("BM25 evidence mismatch")
                        report["steps"].append(
                            {
                                "name": current_step,
                                "passed": True,
                                "message": (
                                    "默认与显式 BM25 返回一致；未选择增强搜索，"
                                    "模型调用为零。"
                                ),
                            }
                        )

                        current_step = "通过 STDIO 取回一条可追溯证据"
                        excerpt = _payload(
                            await client.call_tool(
                                "get_excerpt",
                                {
                                    "library_id": library_id,
                                    "snapshot_id": snapshot_id,
                                    "document_id": evidence["document_id"],
                                    "chunk_id": evidence["chunk_id"],
                                    "max_chars": 160,
                                },
                            )
                        )
                        excerpt_result = excerpt.get("result")
                        if (
                            excerpt.get("found") is not True
                            or excerpt.get("library_id") != library_id
                            or excerpt.get("snapshot_id") != snapshot_id
                            or not isinstance(excerpt_result, dict)
                            or excerpt_result.get("chunk_id") != evidence["chunk_id"]
                        ):
                            raise RuntimeError("MCP evidence identity mismatch")
                        report["steps"].append(
                            {
                                "name": current_step,
                                "passed": True,
                                "message": (
                                    "get_excerpt 返回同一资料库、快照、文档与 chunk 的"
                                    "有界证据。"
                                ),
                            }
                        )

            current_step = "确认自检链路没有改写冻结资料树"
            if _tree_identity(application_root) != before:
                raise RuntimeError("self-check changed the snapshot tree")
            report["steps"].append(
                {
                    "name": current_step,
                    "passed": True,
                    "message": "读取前后资料树身份一致。",
                }
            )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        report["steps"].append(
            {"name": current_step, "passed": False, "message": _SAFE_FAILURE}
        )
        report["message"] = (
            "本地离线自检未通过；已在首个错误处停止，未重试。"
            "这不代表客户端已经配置。"
        )
        return report

    report["passed"] = True
    report["message"] = (
        "本地离线 STDIO 自检通过；这只证明本项目服务器与合成证据链可用，"
        "不代表 ChatGPT desktop、Codex CLI 或 IDE 已经配置。"
    )
    return report


def run_stdio_self_check() -> dict[str, Any]:
    """Run one bounded self-check synchronously for the local Web endpoint."""
    return asyncio.run(_run_stdio_self_check())


__all__ = ["SELF_CHECK_INTENT", "local_mcp_guide", "run_stdio_self_check"]
