"""Frozen desktop backend entry point.

The packaged executable has two deliberately small modes:

``desktop`` owns one loopback HTTP server for its parent desktop shell, while
``mcp`` hands the process' original stdio streams to the existing MCP server.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Sequence, TextIO


_POLL_SECONDS = 0.02


def _desktop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="foliohook-backend desktop",
        description="为 FolioHook 桌面外壳启动单个本机管理服务。",
    )
    parser.add_argument(
        "--application-root",
        type=Path,
        help="仅用于受控测试；省略时使用当前用户的标准应用目录。",
    )
    return parser


def _watch_stdin(stream: TextIO | None, shutdown: threading.Event) -> None:
    """Treat parent-pipe EOF or an explicit shutdown line as process ownership ending."""
    if stream is None:
        shutdown.set()
        return
    try:
        while not shutdown.is_set():
            line = stream.readline()
            if line == "" or line.strip() == "shutdown":
                shutdown.set()
                return
    except (AttributeError, OSError, ValueError):
        shutdown.set()


def _run_server(server: object, listener: socket.socket) -> None:
    """Keep backend exceptions out of the console protocol and caller logs."""
    try:
        server.run(sockets=[listener])  # type: ignore[attr-defined]
    except BaseException:
        return


def run_desktop(
    application_root: Path | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the loopback server until its owning parent closes stdin."""
    import uvicorn

    from .connections import desktop_connections
    from .registry import default_application_root
    from .web import LOOPBACK_HOST, create_app

    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    root = default_application_root() if application_root is None else Path(application_root)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        # Bind before constructing the application so its origin/cookie boundary
        # is built from the exact socket that Uvicorn will serve.
        listener.bind((LOOPBACK_HOST, 0))
        listener.listen(socket.SOMAXCONN)
        port = listener.getsockname()[1]
        app = create_app(
            root,
            port=port,
            connections=desktop_connections(root),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=LOOPBACK_HOST,
                port=port,
                access_log=False,
                proxy_headers=False,
                server_header=False,
                workers=1,
                timeout_graceful_shutdown=5,
                log_level="critical",
            )
        )
        shutdown = threading.Event()
        watcher = threading.Thread(
            target=_watch_stdin,
            args=(input_stream, shutdown),
            name="foliohook-parent-pipe",
            daemon=True,
        )
        server_thread = threading.Thread(
            target=_run_server,
            args=(server, listener),
            name="foliohook-http",
            daemon=True,
        )
        watcher.start()
        server_thread.start()

        while not server.started and server_thread.is_alive() and not shutdown.is_set():
            time.sleep(_POLL_SECONDS)
        if shutdown.is_set() and not server.started:
            server.should_exit = True
            server_thread.join(timeout=5)
            return 0
        if not server.started:
            server.should_exit = True
            server_thread.join(timeout=5)
            return 2

        output_stream.write(
            json.dumps({"url": f"http://{LOOPBACK_HOST}:{port}"}, separators=(",", ":"))
            + "\n"
        )
        output_stream.flush()

        while server_thread.is_alive() and not shutdown.is_set():
            time.sleep(_POLL_SECONDS)
        unexpected_exit = not shutdown.is_set()
        if shutdown.is_set():
            server.should_exit = True
        server_thread.join(timeout=10)
        if server_thread.is_alive():
            server.force_exit = True
            server_thread.join(timeout=2)
        return 0 if not server_thread.is_alive() and not unexpected_exit else 2
    except KeyboardInterrupt:
        try:
            server.should_exit = True
            server_thread.join(timeout=5)
        except UnboundLocalError:
            pass
        return 130
    finally:
        listener.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        sys.stderr.write("用法：foliohook-backend {desktop|mcp} ...\n")
        return 2
    command, forwarded = arguments[0], arguments[1:]
    if command == "mcp":
        from .mcp_server import main as mcp_main

        # Do not parse, rewrite, or wrap MCP arguments or stdio.
        return mcp_main(forwarded)
    if command == "desktop":
        args = _desktop_parser().parse_args(forwarded)
        try:
            status = run_desktop(args.application_root)
        except Exception:
            sys.stderr.write("错误：FolioHook 本机服务未能安全启动。\n")
            return 2
        if status == 2:
            sys.stderr.write("错误：FolioHook 本机服务未能安全启动。\n")
        return status
    sys.stderr.write("错误：未知子命令；应为 desktop 或 mcp。\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_desktop"]
