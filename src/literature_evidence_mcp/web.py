from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from python_multipart.exceptions import MultipartParseError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import ImportPolicyError, SearchInputError, SnapshotError
from .library import FixedLibrary


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SESSION_COOKIE = "literature_evidence_session"
BUILD_INTENT = "create-new-snapshot"
UPLOAD_TEMP_PREFIX = "literature-evidence-upload-"
_MIB = 1024 * 1024
_ALLOWED_SUFFIXES = {".md", ".markdown", ".pdf"}
_SESSION_ID = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")
_STATIC_ROOT = Path(__file__).with_name("static")


@dataclass(frozen=True)
class UploadLimits:
    max_files: int = 20
    max_file_bytes: int = 10 * _MIB
    max_total_bytes: int = 20 * _MIB
    max_request_bytes: int = 21 * _MIB
    max_filename_bytes: int = 240
    max_json_bytes: int = 8 * 1024


DEFAULT_UPLOAD_LIMITS = UploadLimits()


class RequestBoundaryError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _RequestBodyTooLarge(Exception):
    pass


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": message},
        status_code=status_code,
    )


def _size_label(value: int) -> str:
    if value % _MIB == 0:
        return f"{value // _MIB} MiB"
    return f"{value} 字节"


def _raw_header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", [])
        if key.lower() == name
    ]


class SessionTokens:
    """Per-process signed browser session and domain-separated CSRF tokens."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def _signature(self, purpose: bytes, session_id: str) -> str:
        return hmac.new(
            self._secret,
            purpose + b"\0" + session_id.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def issue(self) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        cookie = session_id + "." + self._signature(b"session", session_id)
        return cookie, self._signature(b"csrf", session_id)

    def validate_cookie(self, cookie: str | None) -> str | None:
        if not isinstance(cookie, str):
            return None
        parts = cookie.split(".")
        if len(parts) != 2:
            return None
        session_id, supplied = parts
        if _SESSION_ID.fullmatch(session_id) is None or not re.fullmatch(
            r"[0-9a-f]{64}", supplied
        ):
            return None
        expected = self._signature(b"session", session_id)
        if not hmac.compare_digest(supplied, expected):
            return None
        return session_id

    def csrf_for(self, session_id: str) -> str:
        return self._signature(b"csrf", session_id)


class LocalBoundaryMiddleware:
    """Enforce loopback browser boundaries before request bodies are parsed."""

    _SECURITY_HEADERS = (
        (b"cache-control", b"no-store"),
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self'; style-src 'self'; "
            b"connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            b"base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        ),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
    )

    def __init__(
        self,
        app: ASGIApp,
        *,
        authority: str,
        origin: str,
        cookie_name: str,
        sessions: SessionTokens,
        max_body_bytes: int,
    ) -> None:
        self.app = app
        self.authority = authority
        self.origin = origin
        self.cookie_name = cookie_name
        self.sessions = sessions
        self.max_body_bytes = max_body_bytes
        self.max_body_label = _size_label(max_body_bytes)

    async def _respond(
        self,
        response: Response,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        await response(scope, receive, self._secure_send(send))

    def _secure_send(self, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                blocked = {
                    b"cache-control",
                    b"content-security-policy",
                    b"cross-origin-resource-policy",
                    b"referrer-policy",
                    b"x-content-type-options",
                    b"x-frame-options",
                }
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in blocked
                    and not key.lower().startswith(b"access-control-")
                ]
                headers.extend(self._SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        return wrapped

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host_values = _raw_header_values(scope, b"host")
        if host_values != [self.authority]:
            await self._respond(
                _error_response(400, "Host 不符合本机服务地址，已拒绝请求。"),
                scope,
                receive,
                send,
            )
            return

        origin_values = _raw_header_values(scope, b"origin")
        if origin_values and origin_values != [self.origin]:
            await self._respond(
                _error_response(403, "跨站 Origin 已被本机服务拒绝。"),
                scope,
                receive,
                send,
            )
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        request = Request(scope)
        session_id = self.sessions.validate_cookie(
            request.cookies.get(self.cookie_name)
        )
        if path.startswith("/api/") and path != "/api/status" and session_id is None:
            await self._respond(
                _error_response(403, "浏览器会话无效，请刷新管理页后重试。"),
                scope,
                receive,
                send,
            )
            return

        if method == "POST":
            if session_id is None:
                await self._respond(
                    _error_response(403, "浏览器会话无效，请刷新管理页后重试。"),
                    scope,
                    receive,
                    send,
                )
                return
            if origin_values != [self.origin]:
                await self._respond(
                    _error_response(403, "写入或查询请求必须来自当前本机管理页。"),
                    scope,
                    receive,
                    send,
                )
                return
            csrf_values = _raw_header_values(scope, b"x-csrf-token")
            expected = self.sessions.csrf_for(session_id)
            if (
                len(csrf_values) != 1
                or re.fullmatch(r"[0-9a-f]{64}", csrf_values[0]) is None
                or not hmac.compare_digest(csrf_values[0], expected)
            ):
                await self._respond(
                    _error_response(403, "CSRF 校验失败，请刷新管理页后重试。"),
                    scope,
                    receive,
                    send,
                )
                return

        content_length_values = _raw_header_values(scope, b"content-length")
        if len(content_length_values) > 1:
            await self._respond(
                _error_response(400, "Content-Length 请求头重复。"),
                scope,
                receive,
                send,
            )
            return
        if content_length_values:
            if re.fullmatch(r"[0-9]+", content_length_values[0]) is None:
                await self._respond(
                    _error_response(400, "Content-Length 请求头无效。"),
                    scope,
                    receive,
                    send,
                )
                return
            content_length = int(content_length_values[0], 10)
            if content_length > self.max_body_bytes:
                await self._respond(
                    _error_response(
                        413,
                        f"请求体超过 {self.max_body_label} 上限，请减少所选文件。",
                    ),
                    scope,
                    receive,
                    send,
                )
                return

        received = 0
        response_started = False

        async def receive_limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        secure_send = self._secure_send(send)

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await secure_send(message)

        try:
            await self.app(scope, receive_limited, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._respond(
                _error_response(
                    413,
                    f"请求体超过 {self.max_body_label} 上限，请减少所选文件。",
                ),
                scope,
                receive,
                send,
            )
        except Exception:
            if response_started:
                raise
            await self._respond(
                _error_response(
                    500,
                    "本机操作未完成，请检查所选文件与资料库权限后重试。",
                ),
                scope,
                receive,
                send,
            )


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise RequestBoundaryError(400, f"{name} 请求头不能重复。")
    return values[0] if values else None


def _validated_filename(filename: str | None, limits: UploadLimits) -> str:
    if not isinstance(filename, str) or not filename:
        raise RequestBoundaryError(400, "每个上传文件都必须保留原文件名。")
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise RequestBoundaryError(400, "文件名不能包含路径或路径分隔符。")
    if any(unicodedata.category(character).startswith("C") for character in filename):
        raise RequestBoundaryError(400, "文件名不能包含控制字符。")
    try:
        encoded_length = len(filename.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise RequestBoundaryError(400, "文件名不是有效 Unicode 文本。") from exc
    if encoded_length > limits.max_filename_bytes:
        raise RequestBoundaryError(
            400,
            f"文件名不得超过 {limits.max_filename_bytes} 个 UTF-8 字节。",
        )
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise RequestBoundaryError(400, "仅接受 .md、.markdown 和 .pdf 文件。")
    return filename


async def _bounded_json(request: Request, maximum: int) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise RequestBoundaryError(415, "此接口只接受 application/json。")
    payload = bytearray()
    async for block in request.stream():
        payload.extend(block)
        if len(payload) > maximum:
            raise RequestBoundaryError(
                413,
                f"搜索参数超过 {_size_label(maximum)} 上限。",
            )
    try:
        value = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RequestBoundaryError(400, "JSON 请求体无效。") from exc
    if not isinstance(value, dict):
        raise RequestBoundaryError(400, "JSON 请求体顶层必须是对象。")
    return value


async def _copy_uploads_and_build(
    request: Request,
    library: FixedLibrary,
    limits: UploadLimits,
) -> dict[str, Any]:
    try:
        form_context = request.form(
            max_files=limits.max_files,
            max_fields=0,
            max_part_size=1024,
        )
        async with form_context as form:
            items = form.multi_items()
            if not items:
                raise RequestBoundaryError(400, "请至少选择一个 Markdown 或 PDF 文件。")
            uploads: list[tuple[str, UploadFile]] = []
            seen_names: set[str] = set()
            declared_total = 0
            for field_name, item in items:
                if field_name != "files" or not isinstance(item, UploadFile):
                    raise RequestBoundaryError(400, "构建请求只能包含 files 文件字段。")
                filename = _validated_filename(item.filename, limits)
                collision_key = unicodedata.normalize("NFC", filename).casefold()
                if collision_key in seen_names:
                    raise RequestBoundaryError(400, "一次构建中不能包含重名文件。")
                seen_names.add(collision_key)
                if item.size is not None:
                    if item.size > limits.max_file_bytes:
                        raise RequestBoundaryError(
                            413,
                            f"单个文件超过 {_size_label(limits.max_file_bytes)} 上限。",
                        )
                    declared_total += item.size
                uploads.append((filename, item))
            if len(uploads) > limits.max_files:
                raise RequestBoundaryError(
                    413,
                    f"一次最多选择 {limits.max_files} 个文件。",
                )
            if declared_total > limits.max_total_bytes:
                raise RequestBoundaryError(
                    413,
                    f"所选文件合计超过 {_size_label(limits.max_total_bytes)} 上限。",
                )

            with tempfile.TemporaryDirectory(prefix=UPLOAD_TEMP_PREFIX) as raw:
                temporary = Path(raw)
                os.chmod(temporary, 0o700)
                controlled_sources: list[Path] = []
                total_bytes = 0
                for filename, upload in uploads:
                    await upload.seek(0)
                    target = temporary / filename
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(target, flags, 0o600)
                    file_bytes = 0
                    try:
                        with os.fdopen(descriptor, "wb", closefd=True) as handle:
                            descriptor = -1
                            while True:
                                block = await upload.read(1024 * 1024)
                                if not block:
                                    break
                                file_bytes += len(block)
                                total_bytes += len(block)
                                if file_bytes > limits.max_file_bytes:
                                    raise RequestBoundaryError(
                                        413,
                                        f"单个文件超过 {_size_label(limits.max_file_bytes)} 上限。",
                                    )
                                if total_bytes > limits.max_total_bytes:
                                    raise RequestBoundaryError(
                                        413,
                                        f"所选文件合计超过 {_size_label(limits.max_total_bytes)} 上限。",
                                    )
                                handle.write(block)
                            handle.flush()
                            os.fsync(handle.fileno())
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                    source_status = target.lstat()
                    if stat.S_ISLNK(source_status.st_mode) or not stat.S_ISREG(
                        source_status.st_mode
                    ):
                        raise RequestBoundaryError(400, "受控上传文件状态无效。")
                    controlled_sources.append(target)
                return await run_in_threadpool(library.build, controlled_sources)
    except RequestBoundaryError:
        raise
    except MultipartParseError:
        raise RequestBoundaryError(
            400,
            "multipart 上传格式无效，请重新选择文件后再试。",
        ) from None
    except HTTPException as exc:
        detail = str(exc.detail)
        if "Maximum number of files" in detail:
            raise RequestBoundaryError(
                413,
                f"一次最多选择 {limits.max_files} 个文件。",
            ) from None
        raise RequestBoundaryError(
            400,
            "上传格式无效，或文件/字段数量超过允许范围。",
        ) from None


def create_app(
    library_path: Path,
    *,
    port: int = DEFAULT_PORT,
    upload_limits: UploadLimits = DEFAULT_UPLOAD_LIMITS,
) -> Starlette:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ImportPolicyError("port 必须是 1024-65535 的整数。")
    if (
        type(upload_limits.max_files) is not int
        or upload_limits.max_files < 1
        or upload_limits.max_file_bytes < 1
        or upload_limits.max_total_bytes < upload_limits.max_file_bytes
        or upload_limits.max_request_bytes < upload_limits.max_total_bytes
        or upload_limits.max_filename_bytes < 1
        or upload_limits.max_json_bytes < 1
    ):
        raise ImportPolicyError("上传资源上限配置无效。")

    library = FixedLibrary(library_path)
    sessions = SessionTokens()
    build_lock = threading.Lock()
    authority = f"{LOOPBACK_HOST}:{port}"
    origin = f"http://{authority}"
    cookie_name = f"{SESSION_COOKIE}_{port}"

    async def index(_request: Request) -> Response:
        return FileResponse(_STATIC_ROOT / "index.html", media_type="text/html")

    async def styles(_request: Request) -> Response:
        return FileResponse(_STATIC_ROOT / "styles.css", media_type="text/css")

    async def script(_request: Request) -> Response:
        return FileResponse(
            _STATIC_ROOT / "app.js",
            media_type="text/javascript",
        )

    async def status(request: Request) -> Response:
        cookie = request.cookies.get(cookie_name)
        session_id = sessions.validate_cookie(cookie)
        new_cookie: str | None = None
        if session_id is None:
            new_cookie, csrf_token = sessions.issue()
            session_id = sessions.validate_cookie(new_cookie)
            assert session_id is not None
        else:
            csrf_token = sessions.csrf_for(session_id)
        library_status = await run_in_threadpool(library.status)
        payload = {
            "service": "ready",
            "binding": "127.0.0.1",
            "library": {
                "available": library_status.get("ready", False),
                "snapshot_candidates": library_status.get("snapshot_count", 0),
                "error": library_status.get("error"),
            },
            "readonly_actions": ["status", "list", "verify", "search"],
            "write_actions": ["build"],
            "upload_limits": {
                "max_files": upload_limits.max_files,
                "max_file_bytes": upload_limits.max_file_bytes,
                "max_total_bytes": upload_limits.max_total_bytes,
            },
            "csrf_token": csrf_token,
        }
        response = JSONResponse(payload)
        if new_cookie is not None:
            response.set_cookie(
                cookie_name,
                new_cookie,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        return response

    async def snapshots(_request: Request) -> Response:
        try:
            values = await run_in_threadpool(library.list_snapshots)
        except SnapshotError as exc:
            raise RequestBoundaryError(400, str(exc)) from exc
        return JSONResponse({"snapshots": values})

    async def verify(request: Request) -> Response:
        snapshot_id = request.path_params["snapshot_id"]
        try:
            result = await run_in_threadpool(library.verify, snapshot_id)
        except SnapshotError as exc:
            status_code = 404 if "找不到" in str(exc) or "尚无" in str(exc) else 400
            raise RequestBoundaryError(status_code, str(exc)) from exc
        return JSONResponse(result)

    async def search(request: Request) -> Response:
        body = await _bounded_json(request, upload_limits.max_json_bytes)
        allowed = {"snapshot_id", "query", "top_k", "excerpt_chars"}
        if set(body) - allowed:
            raise RequestBoundaryError(400, "搜索请求包含未允许的参数。")
        if "snapshot_id" not in body or "query" not in body:
            raise RequestBoundaryError(400, "搜索请求缺少 snapshot_id 或 query。")
        try:
            result = await run_in_threadpool(
                library.search,
                body["snapshot_id"],
                body["query"],
                top_k=body.get("top_k", 5),
                excerpt_chars=body.get("excerpt_chars", 1000),
            )
        except SnapshotError as exc:
            status_code = 404 if "找不到" in str(exc) or "尚无" in str(exc) else 400
            raise RequestBoundaryError(status_code, str(exc)) from exc
        return JSONResponse(result)

    async def build(request: Request) -> Response:
        intent = _single_header(request, "x-build-intent")
        if intent != BUILD_INTENT:
            raise RequestBoundaryError(
                400,
                "只有明确点击“构建全新快照”后才能执行 build。",
            )
        content_type = request.headers.get("content-type", "").lower()
        if not content_type.startswith("multipart/form-data;"):
            raise RequestBoundaryError(415, "build 只接受浏览器 multipart 文件选择。")
        if not build_lock.acquire(blocking=False):
            raise RequestBoundaryError(409, "已有构建正在进行，请等待完成。")
        try:
            result = await _copy_uploads_and_build(request, library, upload_limits)
        finally:
            build_lock.release()
        return JSONResponse(result, status_code=201)

    async def request_boundary_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        assert isinstance(exc, RequestBoundaryError)
        return _error_response(exc.status_code, exc.message)

    async def import_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ImportPolicyError)
        return _error_response(422, str(exc))

    async def search_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, SearchInputError)
        return _error_response(400, str(exc))

    async def snapshot_error_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        assert isinstance(exc, SnapshotError)
        return _error_response(400, "快照构建前核验失败，未发布半成品。")

    async def http_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HTTPException)
        messages = {
            404: "未找到请求的本机功能。",
            405: "该接口不接受此 HTTP 方法。",
            413: (
                f"请求体超过 {_size_label(upload_limits.max_request_bytes)} 上限，"
                "请减少所选文件。"
            ),
        }
        return _error_response(
            exc.status_code,
            messages.get(exc.status_code, "HTTP 请求格式无效。"),
        )

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/static/styles.css", styles, methods=["GET"]),
        Route("/static/app.js", script, methods=["GET"]),
        Route("/api/status", status, methods=["GET"]),
        Route("/api/snapshots", snapshots, methods=["GET"]),
        Route(
            "/api/snapshots/{snapshot_id:str}/verify",
            verify,
            methods=["GET"],
        ),
        Route("/api/search", search, methods=["POST"]),
        Route("/api/build", build, methods=["POST"]),
    ]
    middleware = [
        Middleware(
            LocalBoundaryMiddleware,
            authority=authority,
            origin=origin,
            cookie_name=cookie_name,
            sessions=sessions,
            max_body_bytes=upload_limits.max_request_bytes,
        )
    ]
    return Starlette(
        debug=False,
        routes=routes,
        middleware=middleware,
        exception_handlers={
            RequestBoundaryError: request_boundary_handler,
            ImportPolicyError: import_error_handler,
            SearchInputError: search_error_handler,
            SnapshotError: snapshot_error_handler,
            HTTPException: http_error_handler,
        },
    )


def serve_local(library_path: Path, *, port: int = DEFAULT_PORT) -> None:
    """Run one foreground Uvicorn process bound only to IPv4 loopback."""
    import uvicorn

    app = create_app(library_path, port=port)
    url = f"http://{LOOPBACK_HOST}:{port}/"
    config = uvicorn.Config(
        app,
        host=LOOPBACK_HOST,
        port=port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        workers=1,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            listener.bind((LOOPBACK_HOST, port))
        except OSError as exc:
            raise ImportPolicyError(
                f"无法绑定本机端口 {port}；端口可能已被占用。"
            ) from exc
        print(f"本机管理页：{url}")
        print("按 Ctrl+C 可停止服务并释放端口。")
        server.run(sockets=[listener])
    finally:
        listener.close()


__all__ = [
    "BUILD_INTENT",
    "DEFAULT_PORT",
    "DEFAULT_UPLOAD_LIMITS",
    "SESSION_COOKIE",
    "UploadLimits",
    "create_app",
    "serve_local",
]
