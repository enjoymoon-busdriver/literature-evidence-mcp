from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from .library import FixedLibrary
from .registry import LibraryRegistry, default_application_root


SERVER_NAME = "literature-evidence"
SELF_CHECK_INTENT = "mcp-self-check"
MCP_SHIM_NAME = "mcp-server"
_SHELL_COMMAND = (
    'exec "$HOME/Library/Application Support/literature-evidence-mcp/mcp-server"'
)
_SHELL = "/bin/zsh"
_SHELL_FLAG = "-fc"
_QUERY = "stage8localmarker"
_SAFE_FAILURE = "此步骤未通过；自检已停止，未重试。"
_OBSERVATION_SCOPE = (
    "本次自检直接核验 fake HOME 内的固定启动模板、STDIO 协议、BM25 路线和"
    "完整隔离树；未安装系统级网络或隔离树外写入观测，因此这两项为未观测。"
)
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


def _guide_base() -> dict[str, Any]:
    return {
        "server_name": SERVER_NAME,
        "api_key_required": False,
        "tool_count": len(_TOOL_NAMES),
        "local_clients": ["ChatGPT desktop", "Codex CLI", "Codex IDE extension"],
        "web_boundary_zh": (
            "ChatGPT web 不会读取本机 Codex 配置；Stage 9 仅提供 Tunnel 离线模拟向导。"
        ),
    }


def _ready_mcp_shim(application_root: Path) -> bool:
    try:
        requested = Path(os.path.abspath(os.fspath(application_root)))
        if requested != default_application_root():
            return False
        shim = requested / MCP_SHIM_NAME
        status = shim.lstat()
        if not stat.S_ISREG(status.st_mode):
            return False
        text = shim.read_text(encoding="utf-8")
        words = shlex.split(text.splitlines()[1])
        python = Path(words[1])
        python_status = python.stat()
    except (IndexError, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return False
    return (
        stat.S_ISREG(status.st_mode)
        and stat.S_IMODE(status.st_mode) == 0o700
        and os.access(shim, os.X_OK)
        and text.splitlines()[0] == "#!/bin/zsh -f"
        and len(text.splitlines()) == 2
        and words
        == [
            "exec",
            os.fspath(python),
            "-I",
            "-B",
            "-m",
            "literature_evidence_mcp.mcp_server",
            "--application-root",
            os.fspath(requested),
        ]
        and python.is_absolute()
        and python.parent.name == "bin"
        and python.parent.parent.name == ".venv"
        and stat.S_ISREG(python_status.st_mode)
        and os.access(python, os.X_OK)
        and Path(_SHELL).is_file()
        and os.access(_SHELL, os.X_OK)
    )


def local_mcp_guide(application_root: Path) -> dict[str, Any]:
    """Return the copy-only guide only for a prepared standard installation."""
    base = _guide_base()
    if not _ready_mcp_shim(application_root):
        return {
            **base,
            "state": "unavailable",
            "reason": (
                "当前不是已由 Finder 入口准备的标准应用目录，"
                "或本地 MCP 启动入口尚未就绪；不能复制配置。"
            ),
        }
    return {
        **base,
        "state": "copy_ready_not_configured",
        "cli": (
            f"codex mcp add {SERVER_NAME} -- {_SHELL} {_SHELL_FLAG} "
            f"{shlex.quote(_SHELL_COMMAND)}"
        ),
        "toml": (
            f"[mcp_servers.{SERVER_NAME}]\n"
            f"command = {json.dumps(_SHELL)}\n"
            f"args = [{json.dumps(_SHELL_FLAG)}, {json.dumps(_SHELL_COMMAND)}]\n"
        ),
    }


def _temporary_mcp_shim_text(application_root: Path) -> str:
    command = (
        f"exec {shlex.quote(sys.executable)} -I -B -m "
        "literature_evidence_mcp.mcp_server --application-root "
        f"{shlex.quote(os.fspath(application_root))}"
    )
    return f"#!/bin/zsh -f\n{command}\n"


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
        "observation_scope_zh": _OBSERVATION_SCOPE,
        "message": "本地离线自检尚未完成；这不代表客户端已经配置。",
        "tool_count": None,
        "network_calls": None,
        "model_calls": 0,
        "api_keys_used": 0,
        "external_config_writes": None,
        "retry_count": 0,
        "steps": [],
    }


async def _run_stdio_self_check() -> dict[str, Any]:
    report = _base_report()
    current_step = "准备临时合成资料库"
    try:
        with tempfile.TemporaryDirectory(prefix="lemcp-stage8-selfcheck-") as raw:
            temporary = Path(raw)
            fake_home = temporary / "fake home"
            application_root = (
                fake_home
                / "Library"
                / "Application Support"
                / "literature-evidence-mcp"
            )
            source = temporary / "synthetic source" / "stage8-evidence.md"
            source.parent.mkdir()
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
            fake_config = fake_home / ".codex" / "config.toml"
            fake_config.parent.mkdir()
            fake_config.write_text("# self-check sentinel; do not change\n", encoding="utf-8")
            shim = application_root / MCP_SHIM_NAME
            shim.write_text(
                _temporary_mcp_shim_text(application_root),
                encoding="utf-8",
            )
            shim.chmod(0o700)
            before = _tree_identity(temporary)
            report["steps"].append(
                {
                    "name": current_step,
                    "passed": True,
                    "message": (
                        "已在隔离 fake HOME 建立合成快照和固定启动入口；"
                        "未读取用户资料库。"
                    ),
                }
            )

            current_step = "通过复制模板启动真实本地 STDIO MCP 并核验八个工具"
            params = StdioServerParameters(
                command=_SHELL,
                args=[_SHELL_FLAG, _SHELL_COMMAND],
                cwd=temporary,
                env={
                    "HOME": os.fspath(fake_home),
                    "PATH": "/usr/bin:/bin",
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
                                    "固定 /bin/zsh -fc 与 $HOME shim 链启动成功；"
                                    "STDIO 子进程返回恰好八个工具，均只读，"
                                    "仅 search_documents 标注可能访问开放世界。"
                                ),
                            }
                        )

                        current_step = "依次列出资料库、列出快照并核验明确身份"
                        libraries = _payload(
                            await client.call_tool("retrieval_status", {})
                        )
                        listed_libraries = libraries.get("libraries")
                        if (
                            libraries.get("scope") != "libraries"
                            or not isinstance(listed_libraries, list)
                            or len(listed_libraries) != 1
                            or not isinstance(listed_libraries[0], dict)
                            or listed_libraries[0].get("library_id") != library_id
                        ):
                            raise RuntimeError("MCP library listing mismatch")
                        snapshots = _payload(
                            await client.call_tool(
                                "retrieval_status", {"library_id": library_id}
                            )
                        )
                        listed_snapshots = snapshots.get("snapshots")
                        if (
                            snapshots.get("scope") != "snapshots"
                            or snapshots.get("library_id") != library_id
                            or not isinstance(listed_snapshots, list)
                            or len(listed_snapshots) != 1
                            or not isinstance(listed_snapshots[0], dict)
                            or listed_snapshots[0].get("snapshot_id") != snapshot_id
                        ):
                            raise RuntimeError("MCP snapshot listing mismatch")
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
                            status.get("scope") != "snapshot"
                            or status.get("library_id") != library_id
                            or status.get("snapshot_id") != snapshot_id
                            or status.get("verified") is not True
                            or status.get("transport") != "stdio"
                        ):
                            raise RuntimeError("MCP status identity mismatch")
                        report["steps"].append(
                            {
                                "name": current_step,
                                "passed": True,
                                "message": (
                                    "STDIO 工具依次列出唯一合成资料库与快照，"
                                    "再确认该快照已发布且实时核验通过。"
                                ),
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

            current_step = "确认自检链路没有改写完整隔离树"
            if _tree_identity(temporary) != before:
                raise RuntimeError("self-check changed the isolated tree")
            report["steps"].append(
                {
                    "name": current_step,
                    "passed": True,
                    "message": "协议读取前后，fake HOME 与合成资料的完整隔离树一致。",
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
        "本地离线 STDIO 自检通过；这只证明隔离 fake HOME 中相同的固定启动模板"
        "和合成证据链可用，不证明真实 HOME 的入口可执行，也不代表客户端已经配置。"
    )
    return report


def run_stdio_self_check() -> dict[str, Any]:
    """Run one bounded self-check synchronously for the local Web endpoint."""
    return asyncio.run(_run_stdio_self_check())


__all__ = ["SELF_CHECK_INTENT", "local_mcp_guide", "run_stdio_self_check"]
