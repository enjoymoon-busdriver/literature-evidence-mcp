#!/usr/bin/env python3
"""Drive one real Windows cmd.exe -> stable shim -> frozen MCP session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


TOOL_NAMES = (
    "search_documents",
    "get_excerpt",
    "get_multiple_excerpts",
    "get_document_metadata",
    "get_document_toc",
    "read_document_section",
    "find_in_document",
    "retrieval_status",
)


class SmokeFailure(RuntimeError):
    """One fixed, synthetic-fixture-only acceptance failure."""

    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shim", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    return parser.parse_args()


def _payload(result: Any) -> dict[str, Any]:
    if result.is_error or len(result.content) != 1:
        raise SmokeFailure("retrieval_status returned an MCP error")
    content = result.content[0]
    if not isinstance(content, TextContent):
        raise SmokeFailure("retrieval_status returned an unexpected content type")
    try:
        payload = json.loads(content.text)
    except (TypeError, ValueError) as exc:
        raise SmokeFailure("retrieval_status returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SmokeFailure("retrieval_status returned a non-object")
    return payload


def _child_environment() -> dict[str, str]:
    required = ("LOCALAPPDATA", "TEMP", "TMP")
    environment = {name: os.environ[name] for name in required}
    for name in ("ComSpec", "PATH", "SystemRoot", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _sanitize_diagnostic(text: str, shim: Path, cwd: Path) -> str:
    replacements = {
        os.fspath(shim.absolute()): "<shim>",
        os.fspath(cwd.absolute()): "<fixture>",
        os.environ.get("LOCALAPPDATA", ""): "<localappdata>",
        os.environ.get("TEMP", ""): "<temp>",
        os.environ.get("TMP", ""): "<temp>",
    }
    result = text
    for raw, replacement in sorted(
        ((raw, replacement) for raw, replacement in replacements.items() if raw),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        result = result.replace(raw, replacement)
    return result[-2000:]


def _exception_chain(exception: BaseException, shim: Path, cwd: Path) -> list[str]:
    entries: list[str] = []

    def visit(item: BaseException) -> None:
        if len(entries) >= 8:
            return
        if isinstance(item, BaseExceptionGroup):
            entries.append(type(item).__name__)
            for child in item.exceptions:
                visit(child)
            return
        detail = _sanitize_diagnostic(str(item), shim, cwd)
        entries.append(f"{type(item).__name__}: {detail}" if detail else type(item).__name__)

    visit(exception)
    return entries


async def _run(shim: Path, cwd: Path) -> None:
    if sys.platform != "win32":
        raise SmokeFailure("the real cmd.exe smoke test requires Windows")
    raw_shim = os.fspath(shim.absolute())
    if any(character in raw_shim for character in ('"', "%", "\r", "\n")):
        raise SmokeFailure("the stable shim path is not safe for the product cmd template")
    if not shim.is_file() or not cwd.is_dir():
        raise SmokeFailure("the isolated shim fixture is incomplete")

    # Keep this argv shape aligned with mcp_selfcheck._WINDOWS_FLAGS. Python's
    # Windows launcher quotes the separate path item for CreateProcess; do not
    # add nested quotes because cmd.exe does not use the C runtime parser.
    parameters = StdioServerParameters(
        command="cmd.exe",
        args=["/d", "/v:off", "/c", "call", raw_shim],
        cwd=cwd,
        env=_child_environment(),
    )
    stage = "initialize"
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        try:
            async with asyncio.timeout(60):
                async with Client(stdio_client(parameters, errlog=errlog)) as client:
                    stage = "tools/list"
                    tools = (await client.list_tools()).tools
                    names = [tool.name for tool in tools]
                    if names != list(TOOL_NAMES):
                        raise SmokeFailure("tools/list did not return the eight-tool contract")
                    stage = "retrieval_status"
                    status = _payload(await client.call_tool("retrieval_status", {}))
                    if status.get("scope") != "libraries" or status.get("libraries") != []:
                        raise SmokeFailure("the isolated default application root was not empty")
        except Exception as exc:
            errlog.flush()
            errlog.seek(0)
            server_stderr = _sanitize_diagnostic(errlog.read(), shim, cwd).strip()
            raise SmokeFailure(
                "the synthetic cmd.exe MCP chain failed",
                {
                    "stage": stage,
                    "exception_chain": _exception_chain(exc, shim, cwd),
                    "server_stderr": server_stderr,
                    "cmd_args": ["/d", "/v:off", "/c", "call", "<shim>"],
                },
            ) from exc


def main() -> int:
    args = _arguments()
    try:
        asyncio.run(_run(args.shim, args.cwd))
    except (SmokeFailure, TimeoutError) as exc:
        payload = {"passed": False, "error": str(exc)}
        if isinstance(exc, SmokeFailure):
            payload.update(exc.diagnostics)
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print(
            json.dumps(
                {"passed": False, "error": "unexpected MCP shim smoke failure"}
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "passed": True,
                "transport": "cmd.exe -> stable .cmd -> frozen backend mcp",
                "tool_count": len(TOOL_NAMES),
                "library_count": 0,
                "model_calls": 0,
                "api_keys_used": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
