#!/usr/bin/env python3
"""FolioHook frozen-backend smoke test (desktop HTTP + stdio MCP).

Drives the *packaged* backend executable, never the source tree, so a pass
cannot be produced by rebuilding evidence in-process:

* ``<EXE> desktop --application-root <root>``  -> one JSON line on stdout
  containing ``url`` = ``http://127.0.0.1:<actual port>``.
* ``<EXE> mcp --application-root <root>``      -> stdio MCP server with the
  eight documented read-only tools.

Only the standard library plus the MCP SDK already present in the build
environment are used; no module from this repository is imported.

Interface assumptions (agreed with the desktop/packaging owners; desktop.py is
being implemented in parallel, see the task report):

1. Subcommands ``desktop`` and ``mcp`` accept ``--application-root <dir>``.
2. ``desktop`` prints one JSON object line on stdout once it is listening.
3. ``desktop`` shuts down on a ``shutdown`` line on stdin, or when stdin is
   closed, within a bounded time.
4. The HTTP surface is the existing one: session cookie plus CSRF from
   ``GET /api/status``, then ``POST /api/libraries`` (intent
   ``create-library``), ``POST /api/libraries/<id>/build`` (multipart with
   ``build_mode``/``base_snapshot_id`` and ``files``), ``GET .../snapshots``,
   ``GET .../snapshots/<id>/verify``, ``GET .../snapshots/<id>/documents`` and
   ``POST /api/libraries/<id>/search``.

Exit code 0 prints a concise JSON evidence object; any failure exits non-zero
with a JSON error object on stderr. No session token, cookie or CSRF value is
ever written to the output.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

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

READY_TIMEOUT_SECONDS = 120.0
HTTP_TIMEOUT_SECONDS = 30.0
SHUTDOWN_GRACE_SECONDS = 7.0
EXIT_TIMEOUT_SECONDS = 30.0
MCP_TIMEOUT_SECONDS = 180.0
STDERR_TAIL_LINES = 20

LIBRARY_NAME = "FolioHook 冒烟合成库"
LIBRARY_DESCRIPTION = "冻结后端冒烟：一次性合成资料，不是真实文献。"
MARKER_V1 = "foliohooksmokealphamarker"
MARKER_PDF = "foliohooksmokegammamarker"
MARKER_V2 = "foliohooksmokebetamarker"

MD_V1_NAME = "smoke-alpha.md"
MD_V2_NAME = "smoke-beta.md"
PDF_V1_NAME = "smoke-gamma.pdf"

MD_V1 = (
    "# Smoke Alpha\n\n"
    "本段为一次性合成正文，仅用于验证冻结后端的建库与检索。\n"
    f"标识 {MARKER_V1} 只出现在第一版。\n"
)
MD_V2 = (
    "# Smoke Beta\n\n"
    "本段为一次性合成正文，仅在继承快照中新增。\n"
    f"标识 {MARKER_V2} 只出现在第二版。\n"
)
PDF_PAGE_TEXT = f"FolioHook smoke PDF page one {MARKER_PDF}"


class SmokeFailure(RuntimeError):
    """One failed contract check; the message is safe to print."""


def _check(condition: object, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _redact(text: str) -> str:
    """Defensively strip token-shaped values from diagnostics."""
    text = re.sub(r"(?i)\b[0-9a-f]{32,}\b", "<redacted>", text)
    return re.sub(r"literature_evidence_session\S*", "<redacted>", text)


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SmokeFailure(f"{label} 未返回有效 JSON。") from exc
    _check(isinstance(value, dict), f"{label} 的 JSON 顶层不是对象。")
    return value


def _pdf_bytes(pages: list[str]) -> bytes:
    """Minimal text-layer PDF (same construction as tests/test_stage1.py)."""
    objects: dict[int, bytes] = {}
    page_object_numbers = [4 + index * 2 for index in range(len(pages))]
    content_object_numbers = [number + 1 for number in page_object_numbers]
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for page_object, content_object, text in zip(
        page_object_numbers, content_object_numbers, pages
    ):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects[content_object] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"\nendstream"
        )
        objects[page_object] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> "
            + f"/Contents {content_object} 0 R >>".encode()
        )
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number in range(1, max(objects) + 1):
        offsets.append(len(output))
        output.extend(f"{object_number} 0 obj\n".encode())
        output.extend(objects[object_number])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


def _make_root() -> Path:
    """Throwaway application root whose path contains Chinese and a space."""
    root = Path(tempfile.gettempdir()) / f"FolioHook 冒烟 {uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _force_remove(function: Callable[..., Any], path: str, _error: Any) -> None:
    # Frozen stores can contain read-only synthetic files. Retry only that
    # cleanup operation and propagate failure instead of reporting removed.
    os.chmod(path, os.stat(path, follow_symlinks=False).st_mode | stat.S_IWRITE)
    function(path)


def _remove_tree(root: Path) -> None:
    shutil.rmtree(root, onerror=_force_remove)


class FrozenBackend:
    """One frozen child process with bounded waits and captured stderr."""

    def __init__(self, executable: Path, subcommand: str, root: Path) -> None:
        self.executable = executable
        self.root = root
        self.argv = [
            os.fspath(executable),
            subcommand,
            "--application-root",
            os.fspath(root),
        ]
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(
            maxlen=STDERR_TAIL_LINES
        )

    def start(self) -> None:
        environment = dict(os.environ)
        environment.setdefault("PYTHONUTF8", "1")
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        self._proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            cwd=os.fspath(self.root),
        )
        threading.Thread(
            target=self._pump,
            args=(self._proc.stdout, self._lines.put),
            kwargs={"sentinel": True},
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump,
            args=(self._proc.stderr, self._stderr.append),
            daemon=True,
        ).start()

    @staticmethod
    def _pump(stream: Any, sink: Callable[[Any], None], *, sentinel: bool = False) -> None:
        try:
            for line in stream:
                sink(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            if sentinel:
                sink(None)
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @property
    def process(self) -> subprocess.Popen[str]:
        _check(self._proc is not None, "后端进程尚未启动。")
        assert self._proc is not None
        return self._proc

    def stderr_tail(self) -> str:
        return " | ".join(line for line in list(self._stderr) if line)

    def _assert_alive(self, context: str) -> None:
        code = self.process.poll()
        if code is not None:
            raise SmokeFailure(
                f"后端进程在{context}意外退出（exit={code}）；"
                f"stderr 尾部：{_redact(self.stderr_tail()) or '<空>'}"
            )

    @staticmethod
    def _ready_url(line: str | None) -> str | None:
        if not line or not line.lstrip().startswith("{"):
            return None
        try:
            value = json.loads(line)
        except ValueError:
            return None
        if not isinstance(value, dict):
            return None
        url = value.get("url")
        if isinstance(url, str) and url.startswith("http://"):
            return url
        return None

    def wait_ready(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(
                    "后端未在超时内于 stdout 打印含 url 的就绪 JSON 单行。"
                )
            try:
                line = self._lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                self._assert_alive("等待就绪")
                continue
            if line is None:
                self._assert_alive("等待就绪")
                continue
            url = self._ready_url(line)
            if url is not None:
                return url

    def _wait(self, seconds: float) -> bool:
        try:
            self.process.wait(timeout=max(0.0, seconds))
        except subprocess.TimeoutExpired:
            return False
        return True

    def shutdown(self) -> None:
        proc = self.process
        if proc.poll() is None and proc.stdin is not None:
            try:
                proc.stdin.write("shutdown\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
        exited = self._wait(SHUTDOWN_GRACE_SECONDS)
        if not exited:
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
            _check(
                self._wait(EXIT_TIMEOUT_SECONDS),
                f"后端在 shutdown 与关闭 stdin 后 {EXIT_TIMEOUT_SECONDS:.0f} 秒内未退出。",
            )
        _check(proc.returncode == 0, f"后端退出码为 {proc.returncode}，不是 0。")

    def kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


class HttpSession:
    """Loopback client for the frozen desktop surface (cookie + CSRF kept private)."""

    def __init__(self, url: str, timeout: float) -> None:
        parts = urllib.parse.urlsplit(url)
        try:
            port = parts.port
        except ValueError as exc:
            raise SmokeFailure(f"就绪 JSON 的 url 端口无效：{url!r}") from exc
        _check(
            parts.scheme == "http"
            and parts.hostname == "127.0.0.1"
            and port is not None,
            f"就绪 JSON 的 url 不是 http://127.0.0.1:<端口>：{url!r}",
        )
        assert port is not None
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.timeout = timeout
        self._cookie_name = f"literature_evidence_session_{port}"
        self._cookie_value = ""
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.csrf = ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, bytes]:
        request = urllib.request.Request(self.origin + path, data=body, method=method)
        request.add_header("Host", f"127.0.0.1:{self.port}")
        request.add_header("Origin", self.origin)
        if self._cookie_value:
            request.add_header("Cookie", f"{self._cookie_name}={self._cookie_value}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def _capture_cookie(self, headers: Any) -> None:
        for value in headers.get_all("Set-Cookie") or []:
            head = value.split(";", 1)[0]
            name, separator, cookie = head.partition("=")
            if separator and name.strip() == self._cookie_name:
                self._cookie_value = cookie.strip()
                return
        raise SmokeFailure("/api/status 未下发预期的本机会话 Cookie。")

    def bootstrap(self) -> dict[str, Any]:
        # The child already announced readiness. A failing status request is
        # evidence of failure, not a reason for a second readiness/retry loop.
        status, headers, body = self._request("GET", "/api/status")
        _check(status == 200, f"GET /api/status 返回 HTTP {status}。")
        payload = _json_object(body, "GET /api/status")
        token = payload.get("csrf_token")
        _check(isinstance(token, str) and bool(token), "GET /api/status 未返回 csrf_token。")
        self.csrf = token
        self._capture_cookie(headers)
        return payload

    def post_json(
        self, path: str, payload: dict[str, Any], *, intent: str | None = None
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "X-CSRF-Token": self.csrf}
        if intent is not None:
            headers["X-Action-Intent"] = intent
        status, _headers, body = self._request(
            "POST",
            path,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )
        _check(status in {200, 201}, f"POST {path} 返回 HTTP {status}。")
        return _json_object(body, f"POST {path}")

    def get_json(self, path: str) -> dict[str, Any]:
        status, _headers, body = self._request("GET", path)
        _check(status == 200, f"GET {path} 返回 HTTP {status}。")
        return _json_object(body, f"GET {path}")

    def build(
        self,
        library_id: str,
        files: list[tuple[str, bytes]],
        *,
        mode: str,
        base_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        boundary = f"----foliohooksmoke{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        fields = {"build_mode": mode}
        if base_snapshot_id is not None:
            fields["base_snapshot_id"] = base_snapshot_id
        for name, value in fields.items():
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        for filename, content in files:
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="files"; '
                    f'filename="{filename}"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n"
                ).encode("utf-8")
            )
            chunks.append(content)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        status, _headers, body = self._request(
            "POST",
            f"/api/libraries/{library_id}/build",
            body=b"".join(chunks),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-CSRF-Token": self.csrf,
                "X-Action-Intent": "build-snapshot",
            },
        )
        _check(status == 201, f"POST build 返回 HTTP {status}。")
        return _json_object(body, "POST build")

    def search(self, library_id: str, snapshot_id: str, query: str) -> dict[str, Any]:
        return self.post_json(
            f"/api/libraries/{library_id}/search",
            {"snapshot_id": snapshot_id, "query": query, "top_k": 5},
        )


def _names(items: list[dict[str, Any]]) -> set[str]:
    return {item["source_name"] for item in items}


def _http_phase(session: HttpSession, checks: list[str]) -> dict[str, Any]:
    status = session.bootstrap()
    _check(
        status.get("binding") == "127.0.0.1" and status.get("local_only") is True,
        "/api/status 未确认仅绑定 127.0.0.1。",
    )
    checks.append("desktop 就绪 JSON 的 url 指向 127.0.0.1 实际端口")
    checks.append("经 HTTP 取得本机会话与 CSRF（/api/status）")

    created = session.post_json(
        "/api/libraries",
        {"name": LIBRARY_NAME, "description": LIBRARY_DESCRIPTION},
        intent="create-library",
    )
    library = created.get("library")
    _check(isinstance(library, dict), "创建资料库响应缺少 library 对象。")
    library_id = library.get("library_id")
    _check(isinstance(library_id, str) and bool(library_id), "创建资料库未返回 library_id。")
    _check(library.get("name") == LIBRARY_NAME, "创建资料库返回的名称不一致。")
    checks.append("按真实 API 建立合成资料库")

    first = session.build(
        library_id,
        [
            (MD_V1_NAME, MD_V1.encode("utf-8")),
            (PDF_V1_NAME, _pdf_bytes([PDF_PAGE_TEXT])),
        ],
        mode="blank",
    )
    v1 = first.get("snapshot_id")
    _check(first.get("published") is True and isinstance(v1, str), "第一版未发布快照。")
    _check(
        _names(first.get("members", [])) == {MD_V1_NAME, PDF_V1_NAME},
        "第一版成员与上传的合成文件不一致。",
    )
    _check(
        first.get("difference", {}).get("counts", {}).get("added") == 2,
        "第一版差异计数不是新增 2 份文档。",
    )
    checks.append("上传合成 Markdown 与最小带文本层 PDF 并发布第一版")

    second = session.build(
        library_id,
        [(MD_V2_NAME, MD_V2.encode("utf-8"))],
        mode="inherit",
        base_snapshot_id=v1,
    )
    v2 = second.get("snapshot_id")
    _check(second.get("published") is True and isinstance(v2, str), "第二版未发布快照。")
    _check(v2 != v1, "第二版快照 ID 与第一版相同。")
    _check(
        second.get("difference", {}).get("counts")
        == {"added": 1, "inherited": 2, "replaced": 0, "removed": 0},
        "第二版继承差异计数不符合预期（应为新增1/继承2）。",
    )
    checks.append("以继承模式生成第二版快照")

    v1_documents = session.get_json(f"/api/libraries/{library_id}/snapshots/{v1}/documents")
    v1_names = _names(v1_documents.get("documents", []))
    _check(v1_names == {MD_V1_NAME, PDF_V1_NAME}, "第一版文档清单与上传不一致。")
    _check(
        session.get_json(f"/api/libraries/{library_id}/snapshots/{v1}/verify").get("verified")
        is True,
        "第一版快照未通过只读核验。",
    )
    checks.append("第一版快照核验通过且文档清单可读")

    v2_inherited = session.search(library_id, v2, MARKER_V1)
    _check(v2_inherited.get("found") is True, "第二版未命中继承自第一版的正文。")
    _check(MD_V1_NAME in _names(v2_inherited.get("results", [])), "继承正文未指向第一版文档。")

    v2_new = session.search(library_id, v2, MARKER_V2)
    _check(v2_new.get("found") is True, "第二版未命中新增正文。")
    _check(v2_new.get("retrieval_mode") == "bm25", "第二版检索不是 bm25 路线。")
    hit = v2_new["results"][0]
    _check(hit.get("source_name") == MD_V2_NAME, "第二版新增命中来源文档不符。")
    _check(MARKER_V2 in hit.get("excerpt", ""), "第二版命中片段不含预期合成正文。")
    checks.append("第二版 BM25 命中新增正文且出处为该合成文档")

    v1_absent = session.search(library_id, v1, MARKER_V2)
    _check(v1_absent.get("found") is False, "第二版新增内容出现在第一版中。")
    _check(v1_absent.get("results") == [], "第一版对新增内容的检索返回了结果。")

    v1_old = session.search(library_id, v1, MARKER_V1)
    _check(v1_old.get("found") is True, "第一版旧正文无法检索。")
    _check(MD_V1_NAME in _names(v1_old.get("results", [])), "第一版旧正文来源文档不符。")

    v1_pdf = session.search(library_id, v1, MARKER_PDF)
    _check(v1_pdf.get("found") is True, "第一版 PDF 正文无法检索。")
    pdf_hit = v1_pdf["results"][0]
    _check(pdf_hit.get("media_type") == "application/pdf", "PDF 命中未标记为 PDF。")
    _check(pdf_hit.get("anchor_label") == "PDF page 1", "PDF 命中的页码出处不符。")
    checks.append("新内容在旧版缺失、旧版正文与 PDF 页码仍可读")

    return {
        "library_id": library_id,
        "library_name": library.get("name"),
        "v1_snapshot_id": v1,
        "v2_snapshot_id": v2,
        "v1_documents": sorted(v1_names),
        "pdf_anchor_label": pdf_hit.get("anchor_label"),
    }


def _restart_phase(
    session: HttpSession, expected: dict[str, Any], checks: list[str]
) -> dict[str, Any]:
    library_id = expected["library_id"]
    listing = session.get_json(f"/api/libraries/{library_id}/snapshots")
    ids = {item["snapshot_id"] for item in listing.get("snapshots", [])}
    _check(
        ids == {expected["v1_snapshot_id"], expected["v2_snapshot_id"]},
        "重启后快照清单与重启前不一致。",
    )
    old_verify = session.get_json(
        f"/api/libraries/{library_id}/snapshots/{expected['v1_snapshot_id']}/verify"
    )
    _check(old_verify.get("verified") is True, "重启后第一版快照未通过核验。")
    persisted = session.search(library_id, expected["v2_snapshot_id"], MARKER_V2)
    _check(persisted.get("found") is True, "重启后第二版新增正文不可检索。")
    _check(
        MARKER_V2 in persisted["results"][0].get("excerpt", ""),
        "重启后命中片段不含预期合成正文。",
    )
    checks.append("退出并重启后资料库、两版快照与检索结果持久恢复")
    return {"persisted_snapshot_count": len(ids), "persisted_search_found": True}


async def _mcp_checks(
    executable: Path, root: Path, expected: dict[str, Any]
) -> dict[str, Any]:
    import asyncio

    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    def payload(result: Any) -> dict[str, Any]:
        _check(not result.is_error, "MCP 工具返回了错误结果。")
        _check(len(result.content) == 1, "MCP 工具返回的内容块数量异常。")
        text = getattr(result.content[0], "text", None)
        _check(isinstance(text, str), "MCP 工具返回了非文本内容。")
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise SmokeFailure("MCP 工具返回了无效 JSON。") from exc
        _check(isinstance(value, dict), "MCP 工具返回的顶层不是对象。")
        return value

    params = StdioServerParameters(
        command=os.fspath(executable),
        args=["mcp", "--application-root", os.fspath(root)],
        cwd=os.fspath(root),
        env=dict(os.environ),
    )
    checks: list[str] = []
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with asyncio.timeout(MCP_TIMEOUT_SECONDS):
            async with Client(stdio_client(params, errlog=errlog)) as client:
                tools = (await client.list_tools()).tools
                names = [tool.name for tool in tools]
                _check(len(names) == len(TOOL_NAMES), f"MCP 工具数不是 8（{len(names)}）。")
                _check(
                    set(names) == set(TOOL_NAMES),
                    f"MCP 工具集合与约定不一致：{sorted(set(names) ^ set(TOOL_NAMES))}",
                )
                for tool in tools:
                    annotations = tool.annotations
                    _check(
                        annotations is not None
                        and annotations.read_only_hint is True
                        and annotations.destructive_hint is False
                        and annotations.open_world_hint is (tool.name == "search_documents"),
                        f"MCP 工具 {tool.name} 的只读声明不符合合同。",
                    )
                checks.append("stdio 客户端初始化后恰好发现八个只读工具")

                libraries = payload(await client.call_tool("retrieval_status", {}))
                _check(libraries.get("scope") == "libraries", "检索状态未列出资料库。")
                listed = {item["library_id"]: item for item in libraries["libraries"]}
                _check(
                    expected["library_id"] in listed,
                    "MCP 列出的资料库不含桌面端创建的合成库。",
                )
                _check(
                    listed[expected["library_id"]].get("name") == LIBRARY_NAME,
                    "MCP 列出的资料库名称不一致。",
                )

                snapshots = payload(
                    await client.call_tool(
                        "retrieval_status", {"library_id": expected["library_id"]}
                    )
                )
                _check(snapshots.get("scope") == "snapshots", "检索状态未列出快照。")
                listed_ids = {item["snapshot_id"] for item in snapshots["snapshots"]}
                _check(
                    {expected["v1_snapshot_id"], expected["v2_snapshot_id"]} <= listed_ids,
                    "MCP 未列出两版合成快照。",
                )
                checks.append("retrieval_status 依次列出资料库与两版快照")

                detail = payload(
                    await client.call_tool(
                        "retrieval_status",
                        {
                            "library_id": expected["library_id"],
                            "snapshot_id": expected["v2_snapshot_id"],
                        },
                    )
                )
                _check(
                    detail.get("scope") == "snapshot" and detail.get("verified") is True,
                    "第二版快照的检索状态未通过核验。",
                )
                _check(detail.get("retrieval_mode") == "bm25", "检索状态未标注 bm25 路线。")
                _check(
                    detail.get("library_id") == expected["library_id"]
                    and detail.get("snapshot_id") == expected["v2_snapshot_id"],
                    "MCP 快照核验返回了不同的资料库或快照身份。",
                )
                checks.append("快照级 retrieval_status 核验通过且标注 bm25")

                found = payload(
                    await client.call_tool(
                        "search_documents",
                        {
                            "library_id": expected["library_id"],
                            "snapshot_id": expected["v2_snapshot_id"],
                            "query": MARKER_V2,
                        },
                    )
                )
                _check(found.get("found") is True, "MCP BM25 检索未命中新增正文。")
                _check(
                    found.get("library_id") == expected["library_id"]
                    and found.get("snapshot_id") == expected["v2_snapshot_id"]
                    and found.get("retrieval_mode") == "bm25",
                    "MCP 检索返回的资料库、快照或检索模式不符。",
                )
                result = found["results"][0]
                _check(result.get("source_name") == MD_V2_NAME, "MCP 命中来源文档不符。")
                _check(
                    MARKER_V2 in result.get("excerpt", ""),
                    "MCP 命中片段不含预期合成正文。",
                )
                _check(
                    str(result.get("anchor_label", "")).startswith("Markdown lines "),
                    "MCP 命中的 Markdown 行号出处缺失。",
                )
                checks.append("search_documents 命中新增正文并给出文档与行号出处")

                metadata = payload(
                    await client.call_tool(
                        "get_document_metadata",
                        {
                            "library_id": expected["library_id"],
                            "snapshot_id": expected["v2_snapshot_id"],
                            "document_id": result["document_id"],
                        },
                    )
                )
                _check(
                    metadata.get("document", {}).get("source_name") == MD_V2_NAME,
                    "文档元数据与检索命中的文档不一致。",
                )

                excerpt = payload(
                    await client.call_tool(
                        "get_excerpt",
                        {
                            "library_id": expected["library_id"],
                            "snapshot_id": expected["v2_snapshot_id"],
                            "document_id": result["document_id"],
                            "chunk_id": result["chunk_id"],
                        },
                    )
                )
                _check(excerpt.get("found") is True, "get_excerpt 未找到该 chunk。")
                body = excerpt["result"]
                _check(
                    excerpt.get("library_id") == expected["library_id"]
                    and excerpt.get("snapshot_id") == expected["v2_snapshot_id"]
                    and body.get("document_id") == result["document_id"]
                    and body.get("chunk_id") == result["chunk_id"],
                    "get_excerpt 返回的库、版本、文档或块身份不符。",
                )
                _check(MARKER_V2 in body.get("excerpt", ""), "get_excerpt 正文不含预期标识。")
                _check(
                    body.get("anchor_label") == result["anchor_label"],
                    "get_excerpt 的出处与检索命中不一致。",
                )
                _check(
                    body.get("source_name") == MD_V2_NAME,
                    "get_excerpt 的来源文档与检索命中不一致。",
                )
                checks.append("get_excerpt 按 chunk 取回同一文档与同一致出处")

    return {
        "tool_count": len(TOOL_NAMES),
        "tools": list(TOOL_NAMES),
        "library_id": expected["library_id"],
        "snapshot_id": expected["v2_snapshot_id"],
        "document_id": result["document_id"],
        "chunk_id": result["chunk_id"],
        "anchor_label": result["anchor_label"],
        "excerpt_chars": len(body.get("excerpt", "")),
        "checks": checks,
    }


def run_smoke(executable: Path, keep_temp: bool) -> dict[str, Any]:
    import asyncio

    started = time.monotonic()
    _check(executable.is_file(), f"--backend 指向的可执行文件不存在：{executable}")
    root = _make_root()
    checks: list[str] = []
    desktop: FrozenBackend | None = None
    try:
        desktop = FrozenBackend(executable, "desktop", root)
        desktop.start()
        session = HttpSession(desktop.wait_ready(READY_TIMEOUT_SECONDS), HTTP_TIMEOUT_SECONDS)
        http_evidence = _http_phase(session, checks)

        desktop.shutdown()
        checks.append("desktop 收到 shutdown 后有限时间内以 0 退出")
        desktop = None

        desktop = FrozenBackend(executable, "desktop", root)
        desktop.start()
        restart_session = HttpSession(
            desktop.wait_ready(READY_TIMEOUT_SECONDS), HTTP_TIMEOUT_SECONDS
        )
        restart_session.bootstrap()
        restart_evidence = _restart_phase(restart_session, http_evidence, checks)
        desktop.shutdown()
        desktop = None

        mcp_evidence = asyncio.run(_mcp_checks(executable, root, http_evidence))
        checks.extend(mcp_evidence.pop("checks"))

        return {
            "ok": True,
            "script": "scripts/packaging/smoke_backend.py",
            "backend": os.fspath(executable),
            "evidence_level": "frozen_local_http_and_stdio_mcp",
            "synthetic_only": True,
            "temp_root": os.fspath(root) if keep_temp else "<removed>",
            "http": http_evidence,
            "restart": restart_evidence,
            "mcp": mcp_evidence,
            "checks": checks,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except BaseException:
        if keep_temp:
            sys.stderr.write(f"[keep-temp] {_redact(os.fspath(root))}\n")
        raise
    finally:
        if desktop is not None:
            desktop.kill()
        if not keep_temp:
            _remove_tree(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_backend.py",
        description="驱动冻结后端 EXE（desktop HTTP 与 stdio MCP）做一次性合成冒烟。",
    )
    parser.add_argument("--backend", required=True, help="冻结后端可执行文件（EXE）路径")
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="保留一次性合成临时根，便于排查（默认删除）",
    )
    args = parser.parse_args(argv)
    try:
        # MCP intentionally starts in the synthetic root, not the caller's
        # working directory. Resolve before changing the child's cwd.
        evidence = run_smoke(Path(args.backend).resolve(), args.keep_temp)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - report, never leak secrets
        message = _redact(str(exc)) or exc.__class__.__name__
        sys.stderr.write(json.dumps({"ok": False, "error": message}, ensure_ascii=False) + "\n")
        return 1
    sys.stdout.write(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
