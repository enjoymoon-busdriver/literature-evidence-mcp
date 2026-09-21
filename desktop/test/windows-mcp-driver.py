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
    """One fixed, non-sensitive acceptance failure."""


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


async def _run(shim: Path, cwd: Path) -> None:
    if sys.platform != "win32":
        raise SmokeFailure("the real cmd.exe smoke test requires Windows")
    raw_shim = os.fspath(shim.absolute())
    if any(character in raw_shim for character in ('"', "%", "\r", "\n")):
        raise SmokeFailure("the stable shim path is not safe for the product cmd template")
    if not shim.is_file() or not cwd.is_dir():
        raise SmokeFailure("the isolated shim fixture is incomplete")

    # Keep this byte-for-byte argument shape aligned with
    # mcp_selfcheck._windows_launcher_command and _WINDOWS_FLAGS.
    command_string = f'""{raw_shim}""'
    parameters = StdioServerParameters(
        command="cmd.exe",
        args=["/d", "/v:off", "/s", "/c", command_string],
        cwd=cwd,
        env=_child_environment(),
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with asyncio.timeout(60):
            async with Client(stdio_client(parameters, errlog=errlog)) as client:
                tools = (await client.list_tools()).tools
                names = [tool.name for tool in tools]
                if names != list(TOOL_NAMES):
                    raise SmokeFailure("tools/list did not return the eight-tool contract")
                status = _payload(await client.call_tool("retrieval_status", {}))
                if status.get("scope") != "libraries" or status.get("libraries") != []:
                    raise SmokeFailure("the isolated default application root was not empty")


def main() -> int:
    args = _arguments()
    try:
        asyncio.run(_run(args.shim, args.cwd))
    except (SmokeFailure, TimeoutError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}), file=sys.stderr)
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
