from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import stat
import tempfile
import threading
import unicodedata
from contextlib import asynccontextmanager
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

from .errors import (
    EnhancedSearchError,
    ImportPolicyError,
    LibraryRegistryError,
    LiteratureEvidenceError,
    SearchInputError,
    SnapshotError,
)
from .ingest import prepare_document
from .library import FixedLibrary
from .mcp_selfcheck import (
    SELF_CHECK_INTENT,
    local_mcp_guide,
    run_stdio_self_check,
)
from .registry import LibraryRegistry
from .tunnel_wizard import (
    PRODUCTION_START_INTENT,
    TunnelSimulation,
    production_boundary_report,
    tunnel_wizard_guide,
)


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SESSION_COOKIE = "literature_evidence_session"
BUILD_INTENT = "build-snapshot"
CREATE_LIBRARY_INTENT = "create-library"
SELECT_LIBRARY_INTENT = "select-library"
ACTIVATE_SNAPSHOT_INTENT = "activate-snapshot"
ACTION_INTENT_HEADER = "x-action-intent"
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
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


class _RequestBodyTooLarge(Exception):
    pass


def _error_response(
    status_code: int,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if details:
        payload.update(details)
    return JSONResponse(
        payload,
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


def _safe_upload_name(filename: object) -> str:
    display_name = "所选文件"
    if isinstance(filename, str):
        candidate = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if candidate and not any(
            unicodedata.category(character).startswith("C")
            for character in candidate
        ):
            display_name = candidate[:240]
    return display_name


def _file_failure_details(
    filenames: list[object],
    failures: dict[int, str],
    *,
    not_published_error: str = "同批存在失败文件，整批没有发布。",
) -> dict[str, Any]:
    return {
        "published": False,
        "files": [
            {
                "name": _safe_upload_name(filename),
                "state": "failed" if index in failures else "not_published",
                "stage": (
                    "失败、未发布"
                    if index in failures
                    else "未发布（同批文件失败）"
                ),
                "error": failures.get(index, not_published_error),
            }
            for index, filename in enumerate(filenames)
        ],
    }


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
                f"JSON 参数超过 {_size_label(maximum)} 上限。",
            )
    try:
        value = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RequestBoundaryError(400, "JSON 请求体无效。") from exc
    if not isinstance(value, dict):
        raise RequestBoundaryError(400, "JSON 请求体顶层必须是对象。")
    return value


def _require_intent(request: Request, expected: str) -> None:
    if _single_header(request, ACTION_INTENT_HEADER) != expected:
        raise RequestBoundaryError(400, "缺少与当前按钮一致的明确操作意图。")


def _expected_error_status(message: str) -> int:
    if "正在进行" in message:
        return 409
    if "找不到" in message or "不存在" in message or "尚无" in message:
        return 404
    return 400


def _fixed_library(
    registry: LibraryRegistry,
    library_id: str,
    enhanced_search: Any | None = None,
) -> FixedLibrary:
    return FixedLibrary(
        registry.library_path(library_id),
        library_id=library_id,
        enhanced_search=enhanced_search,
    )


def _public_library(
    record: dict[str, Any],
) -> dict[str, Any]:
    library = FixedLibrary(Path(record["library_root"]))
    status = library.catalog_status()
    return {
        "library_id": record["library_id"],
        "name": record["name"],
        "description": record["description"],
        "selected": record["selected"],
        "snapshot_count": status["snapshot_count"],
        "current_snapshot_id": status["current_snapshot_id"],
        "last_successful_snapshot_id": status["last_successful_snapshot_id"],
    }


def _member_difference(
    members: list[dict[str, Any]],
    base_members: list[dict[str, Any]],
) -> dict[str, Any]:
    current_by_id = {item["document_id"]: item for item in members}
    base_by_id = {item["document_id"]: item for item in base_members}
    inherited_ids = set(current_by_id) & set(base_by_id)
    inherited = [current_by_id[item] for item in sorted(inherited_ids)]
    unmatched_current = {
        item: value for item, value in current_by_id.items() if item not in inherited_ids
    }
    unmatched_base = {
        item: value for item, value in base_by_id.items() if item not in inherited_ids
    }

    current_names: dict[str, list[str]] = {}
    base_names: dict[str, list[str]] = {}
    for document_id, item in unmatched_current.items():
        current_names.setdefault(item["source_name"], []).append(document_id)
    for document_id, item in unmatched_base.items():
        base_names.setdefault(item["source_name"], []).append(document_id)

    replaced: list[dict[str, str]] = []
    replaced_current: set[str] = set()
    replaced_base: set[str] = set()
    for source_name in sorted(set(current_names) & set(base_names)):
        current_ids = current_names[source_name]
        base_ids = base_names[source_name]
        if len(current_ids) == 1 and len(base_ids) == 1:
            replaced_current.add(current_ids[0])
            replaced_base.add(base_ids[0])
            replaced.append(
                {
                    "source_name": source_name,
                    "from_document_id": base_ids[0],
                    "to_document_id": current_ids[0],
                }
            )

    added = [
        item
        for document_id, item in sorted(unmatched_current.items())
        if document_id not in replaced_current
    ]
    removed = [
        item
        for document_id, item in sorted(unmatched_base.items())
        if document_id not in replaced_base
    ]
    return {
        "counts": {
            "added": len(added),
            "inherited": len(inherited),
            "replaced": len(replaced),
            "removed": len(removed),
        },
        "added": added,
        "inherited": inherited,
        "replaced": replaced,
        "removed": removed,
    }


def _snapshot_view(
    library: FixedLibrary,
    status: dict[str, Any],
) -> dict[str, Any]:
    if not status.get("verified"):
        return {**status, "members": [], "difference": None}
    members = library.snapshot_members(status["snapshot_id"])
    base_snapshot_id = status.get("base_snapshot_id")
    try:
        base_members = (
            []
            if base_snapshot_id is None
            else library.snapshot_members(base_snapshot_id)
        )
    except LiteratureEvidenceError:
        return {
            **status,
            "members": members,
            "difference": None,
            "difference_error": "基础快照未通过核验，无法计算差异。",
        }
    return {
        **status,
        "members": members,
        "difference": _member_difference(members, base_members),
    }


async def _copy_uploads_and_build(
    request: Request,
    library: FixedLibrary,
    limits: UploadLimits,
) -> dict[str, Any]:
    try:
        form_context = request.form(
            max_files=limits.max_files,
            max_fields=2,
            max_part_size=1024,
        )
        async with form_context as form:
            items = form.multi_items()
            if not items:
                raise RequestBoundaryError(400, "构建请求不能为空。")
            raw_uploads: list[tuple[str, UploadFile]] = []
            fields: dict[str, str] = {}
            parameter_error: str | None = None
            for field_name, item in items:
                if not isinstance(item, UploadFile):
                    if field_name not in {"build_mode", "base_snapshot_id"}:
                        parameter_error = "构建请求包含未允许的字段。"
                    elif field_name in fields or not isinstance(item, str):
                        parameter_error = "构建模式字段不能重复。"
                    else:
                        fields[field_name] = item
                    continue
                raw_uploads.append((field_name, item))

            upload_names = [item.filename for _field_name, item in raw_uploads]
            if parameter_error is not None:
                raise RequestBoundaryError(
                    400,
                    parameter_error,
                    details=_file_failure_details(
                        upload_names,
                        {},
                        not_published_error=parameter_error,
                    ),
                )
            uploads: list[tuple[int, str, UploadFile]] = []
            validation_errors: dict[int, str] = {}
            validation_statuses: dict[int, int] = {}
            seen_names: dict[str, int] = {}
            declared_total = 0
            for index, (field_name, item) in enumerate(raw_uploads):
                if field_name != "files":
                    validation_errors[index] = (
                        "构建请求只能在 files 字段上传文件。"
                    )
                    validation_statuses[index] = 400
                try:
                    filename = _validated_filename(item.filename, limits)
                except RequestBoundaryError as exc:
                    validation_errors[index] = exc.message
                    validation_statuses[index] = exc.status_code
                    continue
                collision_key = unicodedata.normalize("NFC", filename).casefold()
                if collision_key in seen_names:
                    message = "一次构建中不能包含重名文件。"
                    validation_errors[seen_names[collision_key]] = message
                    validation_errors[index] = message
                    validation_statuses[seen_names[collision_key]] = 400
                    validation_statuses[index] = 400
                else:
                    seen_names[collision_key] = index
                if item.size is not None:
                    if item.size > limits.max_file_bytes:
                        message = (
                            f"单个文件超过 {_size_label(limits.max_file_bytes)} 上限。"
                        )
                        validation_errors[index] = message
                        validation_statuses[index] = 413
                    declared_total += item.size
                uploads.append((index, filename, item))
            if validation_errors:
                status_code = 413 if 413 in validation_statuses.values() else 400
                message = next(iter(validation_errors.values()))
                raise RequestBoundaryError(
                    status_code,
                    message,
                    details=_file_failure_details(upload_names, validation_errors),
                )
            build_mode = fields.get("build_mode")
            base_snapshot_id = fields.get("base_snapshot_id")
            if build_mode not in {"blank", "inherit"}:
                message = "必须明确选择从空白建立或继承基础快照。"
                raise RequestBoundaryError(
                    400,
                    message,
                    details=_file_failure_details(
                        upload_names,
                        {},
                        not_published_error=message,
                    ),
                )
            if build_mode == "blank" and base_snapshot_id is not None:
                message = "从空白建立时不能提供 base_snapshot_id。"
                raise RequestBoundaryError(
                    400,
                    message,
                    details=_file_failure_details(
                        upload_names,
                        {},
                        not_published_error=message,
                    ),
                )
            if build_mode == "inherit" and not base_snapshot_id:
                message = "继承建立必须明确提供 base_snapshot_id。"
                raise RequestBoundaryError(
                    400,
                    message,
                    details=_file_failure_details(
                        upload_names,
                        {},
                        not_published_error=message,
                    ),
                )
            if not uploads:
                raise RequestBoundaryError(400, "请至少选择一个 Markdown 或 PDF 文件。")
            if len(uploads) > limits.max_files:
                raise RequestBoundaryError(
                    413,
                    f"一次最多选择 {limits.max_files} 个文件。",
                )
            if declared_total > limits.max_total_bytes:
                message = (
                    f"所选文件合计超过 {_size_label(limits.max_total_bytes)} 上限。"
                )
                raise RequestBoundaryError(
                    413,
                    message,
                    details=_file_failure_details(
                        upload_names,
                        {},
                        not_published_error=message,
                    ),
                )

            raw = tempfile.mkdtemp(prefix=UPLOAD_TEMP_PREFIX)
            temporary = Path(raw)
            try:
                os.chmod(temporary, 0o700)
                controlled_sources: list[Path] = []
                total_bytes = 0
                for upload_index, filename, upload in uploads:
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
                                    message = (
                                        "单个文件超过 "
                                        f"{_size_label(limits.max_file_bytes)} 上限。"
                                    )
                                    raise RequestBoundaryError(
                                        413,
                                        message,
                                        details=_file_failure_details(
                                            upload_names,
                                            {upload_index: message},
                                        ),
                                    )
                                if total_bytes > limits.max_total_bytes:
                                    message = (
                                        "所选文件合计超过 "
                                        f"{_size_label(limits.max_total_bytes)} 上限。"
                                    )
                                    raise RequestBoundaryError(
                                        413,
                                        message,
                                        details=_file_failure_details(
                                            upload_names,
                                            {},
                                            not_published_error=message,
                                        ),
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
                        message = "受控上传文件状态无效。"
                        raise RequestBoundaryError(
                            400,
                            message,
                            details=_file_failure_details(
                                upload_names,
                                {upload_index: message},
                            ),
                        )
                    controlled_sources.append(target)

                preflight_errors: dict[str, str] = {}
                prepared_members: list[dict[str, Any]] = []
                for filename, target in zip(
                    (item[1] for item in uploads),
                    controlled_sources,
                    strict=True,
                ):
                    try:
                        document = await run_in_threadpool(prepare_document, target)
                    except ImportPolicyError as exc:
                        preflight_errors[filename] = str(exc)
                    else:
                        prepared_members.append(
                            {
                                "document_id": document.document_id,
                                "source_name": document.source_name,
                                "title": document.title,
                                "media_type": document.media_type,
                                "byte_size": document.byte_size,
                                "chunk_count": len(document.chunks),
                            }
                        )
                if preflight_errors:
                    file_results = [
                        {
                            "name": filename,
                            "state": (
                                "failed"
                                if filename in preflight_errors
                                else "not_published"
                            ),
                            "stage": (
                                "失败、未发布"
                                if filename in preflight_errors
                                else "未发布（同批文件失败）"
                            ),
                            "error": preflight_errors.get(
                                filename,
                                "同批存在失败文件，整批没有发布。",
                            ),
                        }
                        for _index, filename, _upload in uploads
                    ]
                    raise RequestBoundaryError(
                        422,
                        "至少一个文件解析失败，整批未发布。",
                        details={"published": False, "files": file_results},
                    )

                try:
                    base_members = (
                        []
                        if base_snapshot_id is None
                        else await run_in_threadpool(
                            library.snapshot_members,
                            base_snapshot_id,
                        )
                    )
                    result = await run_in_threadpool(
                        library.build,
                        controlled_sources,
                        base_snapshot_id=base_snapshot_id,
                    )
                except LiteratureEvidenceError as exc:
                    status_code = _expected_error_status(str(exc))
                    message = (
                        "快照构建或基础快照核验失败。"
                        if isinstance(exc, SnapshotError) and status_code != 409
                        else str(exc)
                    )
                    raise RequestBoundaryError(
                        status_code,
                        f"{message} 整批未发布。",
                        details={
                            "published": False,
                            "files": [
                                {
                                    "name": filename,
                                    "state": "failed",
                                    "stage": "失败、未发布",
                                    "error": message,
                                }
                                for _index, filename, _upload in uploads
                            ],
                        },
                    ) from exc
                response = {
                    **result,
                    "published": True,
                    "members": sorted(
                        [*base_members, *prepared_members],
                        key=lambda item: item["document_id"],
                    ),
                    "difference": _member_difference(
                        [*base_members, *prepared_members],
                        base_members,
                    ),
                    "files": [
                        {
                            "name": filename,
                            "state": "published",
                            "stage": "已进入成功快照",
                            "error": None,
                        }
                        for _index, filename, _upload in uploads
                    ],
                }
            except BaseException:
                try:
                    shutil.rmtree(temporary)
                except OSError:
                    pass
                raise
            try:
                shutil.rmtree(temporary)
            except OSError:
                response["cleanup_warning"] = (
                    "快照已成功发布，但本次上传临时文件未能完全清理；"
                    "请勿重复构建。"
                )
            return response
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
    application_root: Path,
    *,
    port: int = DEFAULT_PORT,
    upload_limits: UploadLimits = DEFAULT_UPLOAD_LIMITS,
    enhanced_search: Any | None = None,
    connections: Any | None = None,
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

    registry = LibraryRegistry(application_root)
    sessions = SessionTokens()
    tunnel_simulations: dict[str, TunnelSimulation] = {}
    build_lock = threading.Lock()
    authority = f"{LOOPBACK_HOST}:{port}"
    origin = f"http://{authority}"
    cookie_name = f"{SESSION_COOKIE}_{port}"
    enhanced_summary = (
        None if enhanced_search is None else enhanced_search.public_summary()
    )
    enhanced_zero_call_audit = {
        "simulated": (
            enhanced_summary["simulated"]
            if enhanced_summary is not None
            and type(enhanced_summary.get("simulated")) is bool
            else None
        ),
        "call_count": 0,
        "calls": [],
    }

    async def index(_request: Request) -> Response:
        return FileResponse(_STATIC_ROOT / "index.html", media_type="text/html")

    async def icon(_request: Request) -> Response:
        return FileResponse(_STATIC_ROOT / "icon.png", media_type="image/png")

    async def styles(_request: Request) -> Response:
        return FileResponse(_STATIC_ROOT / "styles.css", media_type="text/css")

    async def script(_request: Request) -> Response:
        return FileResponse(
            _STATIC_ROOT / "app.js",
            media_type="text/javascript",
        )

    async def connection_script(_request: Request) -> Response:
        return FileResponse(_STATIC_ROOT / "connections.js", media_type="text/javascript")

    async def status(request: Request) -> Response:
        connection_status = None if connections is None else await run_in_threadpool(connections.status)
        active_enhanced = enhanced_summary
        if connection_status and connection_status["aliyun_configured"]:
            active_enhanced = connection_status["models"]
        cookie = request.cookies.get(cookie_name)
        session_id = sessions.validate_cookie(cookie)
        new_cookie: str | None = None
        if session_id is None:
            new_cookie, csrf_token = sessions.issue()
            session_id = sessions.validate_cookie(new_cookie)
            assert session_id is not None
        else:
            csrf_token = sessions.csrf_for(session_id)
        library_records = await run_in_threadpool(registry.list_libraries)
        payload = {
            "service": "ready",
            "binding": "127.0.0.1",
            "local_only": True,
            "offline_default": "bm25",
            "enhanced_available": active_enhanced is not None,
            "enhanced": active_enhanced,
            "connections": connection_status,
            "library_count": len(library_records),
            "readonly_actions": ["status", "list", "verify", "search"],
            "write_actions": ["create", "select", "build", "activate"],
            "upload_limits": {
                "max_files": upload_limits.max_files,
                "max_file_bytes": upload_limits.max_file_bytes,
                "max_total_bytes": upload_limits.max_total_bytes,
            },
            "mcp_guide": local_mcp_guide(application_root),
            "tunnel_wizard": {
                "guide": tunnel_wizard_guide(),
                "simulation": tunnel_simulations.setdefault(
                    session_id, TunnelSimulation()
                ).snapshot(),
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

    async def libraries(_request: Request) -> Response:
        records = await run_in_threadpool(registry.list_libraries)
        values = [
            await run_in_threadpool(_public_library, record)
            for record in records
        ]
        return JSONResponse({"libraries": values})

    async def create_library(request: Request) -> Response:
        _require_intent(request, CREATE_LIBRARY_INTENT)
        body = await _bounded_json(request, upload_limits.max_json_bytes)
        if set(body) - {"name", "description"}:
            raise RequestBoundaryError(400, "创建资料库请求包含未允许的参数。")
        if "name" not in body:
            raise RequestBoundaryError(400, "创建资料库请求缺少名称。")
        record = await run_in_threadpool(
            registry.create,
            body["name"],
            description=body.get("description", ""),
        )
        public = await run_in_threadpool(_public_library, record)
        return JSONResponse({"library": public}, status_code=201)

    async def select_library(request: Request) -> Response:
        _require_intent(request, SELECT_LIBRARY_INTENT)
        record = await run_in_threadpool(
            registry.select,
            request.path_params["library_id"],
        )
        public = await run_in_threadpool(_public_library, record)
        return JSONResponse({"library": public})

    async def snapshots(request: Request) -> Response:
        library_id = request.path_params["library_id"]
        library = await run_in_threadpool(_fixed_library, registry, library_id)
        values = await run_in_threadpool(library.list_snapshots)
        views = [
            await run_in_threadpool(_snapshot_view, library, item)
            for item in values
        ]
        return JSONResponse(
            {
                "library_id": library_id,
                "current_snapshot_id": next(
                    (
                        item["snapshot_id"]
                        for item in values
                        if item["current"]
                    ),
                    None,
                ),
                "last_successful_snapshot_id": next(
                    (
                        item["snapshot_id"]
                        for item in values
                        if item["last_successful"]
                    ),
                    None,
                ),
                "snapshots": views,
            }
        )

    async def verify(request: Request) -> Response:
        library_id = request.path_params["library_id"]
        snapshot_id = request.path_params["snapshot_id"]
        library = await run_in_threadpool(_fixed_library, registry, library_id)
        result = await run_in_threadpool(library.verify, snapshot_id)
        view = await run_in_threadpool(_snapshot_view, library, result)
        return JSONResponse({"library_id": library_id, **view})

    async def activate(request: Request) -> Response:
        _require_intent(request, ACTIVATE_SNAPSHOT_INTENT)
        library_id = request.path_params["library_id"]
        snapshot_id = request.path_params["snapshot_id"]
        library = await run_in_threadpool(_fixed_library, registry, library_id)
        result = await run_in_threadpool(library.activate, snapshot_id)
        return JSONResponse({"library_id": library_id, **result})

    async def search(request: Request) -> Response:
        library_id = request.path_params["library_id"]
        body = await _bounded_json(request, upload_limits.max_json_bytes)
        allowed = {"snapshot_id", "query", "top_k", "excerpt_chars", "mode"}
        mode = body.get("mode", "bm25")
        if set(body) - allowed:
            if mode == "enhanced":
                raise EnhancedSearchError(
                    "增强搜索请求包含未允许的参数。",
                    enhanced_zero_call_audit,
                )
            raise RequestBoundaryError(400, "搜索请求包含未允许的参数。")
        if type(mode) is not str or mode not in {"bm25", "enhanced"}:
            raise RequestBoundaryError(400, "mode 必须是 bm25 或 enhanced。")
        if "snapshot_id" not in body or "query" not in body:
            if mode == "enhanced":
                raise EnhancedSearchError(
                    "增强搜索请求缺少 snapshot_id 或 query。",
                    enhanced_zero_call_audit,
                )
            raise RequestBoundaryError(400, "搜索请求缺少 snapshot_id 或 query。")
        try:
            selected_enhanced = enhanced_search
            if mode == "enhanced" and connections is not None and selected_enhanced is None:
                selected_enhanced = connections.enhanced
            library = await run_in_threadpool(
                _fixed_library, registry, library_id, selected_enhanced
            )
        except LiteratureEvidenceError as exc:
            if mode == "enhanced":
                raise EnhancedSearchError(
                    "增强搜索本地核验失败：资料库或快照不可用。",
                    enhanced_zero_call_audit,
                ) from exc
            raise
        result = await run_in_threadpool(
            library.search,
            body["snapshot_id"],
            body["query"],
            top_k=body.get("top_k", 5),
            excerpt_chars=body.get("excerpt_chars", 1000),
            mode=mode,
        )
        return JSONResponse({"library_id": library_id, **result})

    async def build(request: Request) -> Response:
        _require_intent(request, BUILD_INTENT)
        library_id = request.path_params["library_id"]
        content_type = request.headers.get("content-type", "").lower()
        if not content_type.startswith("multipart/form-data;"):
            raise RequestBoundaryError(415, "build 只接受浏览器 multipart 文件选择。")
        if not build_lock.acquire(blocking=False):
            raise RequestBoundaryError(409, "已有构建正在进行；本次未重试、未发布。")
        try:
            library = await run_in_threadpool(_fixed_library, registry, library_id)
            result = await _copy_uploads_and_build(request, library, upload_limits)
        finally:
            build_lock.release()
        return JSONResponse(
            {
                "library_id": library_id,
                **result,
            },
            status_code=201,
        )

    async def mcp_self_check(request: Request) -> Response:
        _require_intent(request, SELF_CHECK_INTENT)
        if request.url.query or await request.body():
            raise RequestBoundaryError(
                400,
                "本地 MCP 自检不接受参数、路径、命令或环境变量。",
            )
        if local_mcp_guide(application_root)["state"] != "copy_ready_not_configured":
            return _error_response(
                503,
                "本地 MCP 启动入口尚未由 Finder 路径准备；不能运行连接自检。",
            )
        try:
            report = await run_in_threadpool(run_stdio_self_check)
        except Exception:
            return _error_response(
                503,
                "本地 MCP 自检未能安全启动；未写入任何客户端配置。",
            )
        return JSONResponse({"self_check": report})

    async def tunnel_action(request: Request) -> Response:
        action = request.path_params["action"]
        if action not in {"start", "health", "stop"}:
            raise RequestBoundaryError(404, "未找到该模拟操作。")
        _require_intent(request, f"tunnel-simulated-{action}")
        if request.url.query or await request.body():
            raise RequestBoundaryError(400, "Tunnel 离线模拟不接受任何参数。")
        session_id = sessions.validate_cookie(request.cookies.get(cookie_name))
        assert session_id is not None
        simulation = tunnel_simulations.setdefault(session_id, TunnelSimulation())
        # The fake completes synchronously, so state transitions cannot interleave.
        status_code, report = getattr(simulation, action)()
        return JSONResponse({"tunnel": report}, status_code=status_code)

    async def tunnel_production_start(request: Request) -> Response:
        _require_intent(request, PRODUCTION_START_INTENT)
        if request.url.query or await request.body():
            raise RequestBoundaryError(400, "生产 Tunnel 边界不接受任何参数。")
        if connections is not None:
            report = await run_in_threadpool(connections.start_tunnel)
            return JSONResponse({"tunnel": report}, status_code=200 if report["passed"] else 503)
        report = production_boundary_report()
        return _error_response(
            409, report["message"], {**report, "code": "real-approval-required"}
        )

    def require_connections() -> Any:
        if connections is None:
            raise RequestBoundaryError(503, "真实接入尚未在此管理页启用。")
        return connections

    async def save_credential(request: Request) -> Response:
        _require_intent(request, "save-credential")
        runtime = require_connections()
        body = await _bounded_json(request, 4096)
        if request.url.query or set(body) != {"kind", "key"}:
            raise RequestBoundaryError(400, "凭据保存只接受指定种类和 Key。")
        result = await run_in_threadpool(runtime.save_key, body["kind"], body["key"])
        return JSONResponse({"connections": result})

    async def save_tunnel_settings(request: Request) -> Response:
        _require_intent(request, "save-tunnel-settings")
        body = await _bounded_json(request, 4096)
        if request.url.query or set(body) != {"tunnel_id", "accept_backoff"}:
            raise RequestBoundaryError(400, "Tunnel 设置只接受身份和重连选项。")
        result = await run_in_threadpool(require_connections().save_tunnel,
                                        body["tunnel_id"], body["accept_backoff"])
        return JSONResponse({"connections": result})

    async def real_tunnel_action(request: Request) -> Response:
        action = request.path_params["action"]
        if action not in {"health", "stop"}:
            raise RequestBoundaryError(404, "未找到此操作。")
        _require_intent(request, f"tunnel-production-{action}")
        if request.url.query or await request.body():
            raise RequestBoundaryError(400, "此操作不接受参数。")
        report = await run_in_threadpool(getattr(require_connections().tunnel, action))
        return JSONResponse({"tunnel": report}, status_code=200 if report["passed"] else 503)

    async def vectors_action(request: Request) -> Response:
        action = request.path_params["action"]
        if action not in {"preview", "build"}:
            raise RequestBoundaryError(404, "未找到此操作。")
        _require_intent(request, f"vectors-{action}")
        body = await _bounded_json(request, 4096)
        if request.url.query or set(body) != {"snapshot_id"}:
            raise RequestBoundaryError(400, "向量操作必须明确指定快照。")
        runtime = require_connections()
        method = runtime.preview_vectors if action == "preview" else runtime.build_vectors
        result = await run_in_threadpool(method, request.path_params["library_id"], body["snapshot_id"])
        return JSONResponse({"vectors": result})

    async def connection_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        from .connections import ConnectionError
        from .credentials import CredentialError
        message = str(_exc) if isinstance(_exc, (ConnectionError, CredentialError)) else (
            "真实接入操作未完成；请检查本机凭据和所选配置。没有自动重试。")
        return _error_response(503, message)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            yield
        finally:
            if connections is not None:
                await run_in_threadpool(connections.tunnel.close)

    async def request_boundary_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        assert isinstance(exc, RequestBoundaryError)
        return _error_response(exc.status_code, exc.message, exc.details)

    async def import_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ImportPolicyError)
        return _error_response(422, str(exc))

    async def search_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, SearchInputError)
        return _error_response(400, str(exc))

    async def enhanced_error_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        assert isinstance(exc, EnhancedSearchError)
        unavailable = "尚未配置" in str(exc) or "当前不可用" in str(exc)
        return _error_response(
            503 if unavailable else 422,
            str(exc),
            {"mode": "enhanced", "audit": exc.audit},
        )

    async def snapshot_error_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        assert isinstance(exc, SnapshotError)
        audit = getattr(exc, "provider_audit", None)
        return _error_response(
            _expected_error_status(str(exc)), str(exc),
            {"provider_audit": audit} if audit is not None else None,
        )

    async def registry_error_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        assert isinstance(exc, LibraryRegistryError)
        return _error_response(_expected_error_status(str(exc)), str(exc))

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
        Route("/static/icon.png", icon, methods=["GET"]),
        Route("/static/styles.css", styles, methods=["GET"]),
        Route("/static/app.js", script, methods=["GET"]),
        Route("/static/connections.js", connection_script, methods=["GET"]),
        Route("/api/connections/credentials", save_credential, methods=["POST"]),
        Route("/api/connections/tunnel", save_tunnel_settings, methods=["POST"]),
        Route("/api/tunnel/production/{action:str}", real_tunnel_action, methods=["POST"]),
        Route("/api/libraries/{library_id:str}/vectors/{action:str}", vectors_action, methods=["POST"]),
        Route("/api/status", status, methods=["GET"]),
        Route("/api/mcp-self-check", mcp_self_check, methods=["POST"]),
        Route("/api/tunnel/simulated/{action:str}", tunnel_action, methods=["POST"]),
        Route("/api/tunnel/production-start", tunnel_production_start, methods=["POST"]),
        Route("/api/libraries", libraries, methods=["GET"]),
        Route("/api/libraries", create_library, methods=["POST"]),
        Route(
            "/api/libraries/{library_id:str}/select",
            select_library,
            methods=["POST"],
        ),
        Route(
            "/api/libraries/{library_id:str}/snapshots",
            snapshots,
            methods=["GET"],
        ),
        Route(
            "/api/libraries/{library_id:str}/snapshots/{snapshot_id:str}/verify",
            verify,
            methods=["GET"],
        ),
        Route(
            "/api/libraries/{library_id:str}/snapshots/{snapshot_id:str}/activate",
            activate,
            methods=["POST"],
        ),
        Route(
            "/api/libraries/{library_id:str}/search",
            search,
            methods=["POST"],
        ),
        Route(
            "/api/libraries/{library_id:str}/build",
            build,
            methods=["POST"],
        ),
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
        lifespan=lifespan,
        debug=False,
        routes=routes,
        middleware=middleware,
        exception_handlers={
            RequestBoundaryError: request_boundary_handler,
            EnhancedSearchError: enhanced_error_handler,
            ImportPolicyError: import_error_handler,
            SearchInputError: search_error_handler,
            SnapshotError: snapshot_error_handler,
            LibraryRegistryError: registry_error_handler,
            HTTPException: http_error_handler,
            LiteratureEvidenceError: connection_error_handler,
        },
    )


def serve_local(
    application_root: Path,
    *,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
    browser_opener: Callable[[str], object] | None = None,
) -> None:
    """Run one foreground Uvicorn process bound only to IPv4 loopback."""
    import uvicorn

    from .connections import Connections
    app = create_app(application_root, port=port, connections=Connections(application_root))
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
        listener.listen(socket.SOMAXCONN)
        print(f"本机管理页：{url}")
        print("按 Ctrl+C 可停止服务并释放端口。")
        if open_browser:
            if browser_opener is None:
                import webbrowser

                browser_opener = webbrowser.open
            try:
                opened = browser_opener(url)
            except Exception:
                opened = False
            if opened is False:
                print("未能自动打开浏览器；请手动打开上述本机地址。")
        server.run(sockets=[listener])
    finally:
        listener.close()


__all__ = [
    "ACTION_INTENT_HEADER",
    "ACTIVATE_SNAPSHOT_INTENT",
    "BUILD_INTENT",
    "CREATE_LIBRARY_INTENT",
    "DEFAULT_PORT",
    "DEFAULT_UPLOAD_LIMITS",
    "SELF_CHECK_INTENT",
    "SELECT_LIBRARY_INTENT",
    "SESSION_COOKIE",
    "UploadLimits",
    "create_app",
    "serve_local",
]
