from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from .errors import SnapshotError
from .ingest import (
    ChunkDraft,
    ParsedDocument,
    PreparedDocument,
    _chunk_parsed,
    _chunker_identity,
    _parse_payload,
    _parser_identity,
    _prepared_document,
)


OBJECT_STORE_FORMAT = "literature-evidence-object-store"
OBJECT_STORE_VERSION = 1
OBJECTS_DIRECTORY = "objects"
OBJECT_PAYLOAD_NAME = "payload"
OBJECT_KINDS = ("source", "parsed", "chunks")
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _relative_path(kind: str, digest: str) -> str:
    return f"{OBJECTS_DIRECTORY}/{kind}/{digest}/{OBJECT_PAYLOAD_NAME}"


def _validated_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SnapshotError(f"{label}摘要无效。")
    return value


def _validated_pipeline(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "implementation",
        "version",
        "config",
    }:
        raise SnapshotError(f"{label}身份无效。")
    if not isinstance(value["implementation"], str) or not value["implementation"]:
        raise SnapshotError(f"{label}实现名称无效。")
    if type(value["version"]) is not int or value["version"] < 1:
        raise SnapshotError(f"{label}实现版本无效。")
    if not isinstance(value["config"], dict):
        raise SnapshotError(f"{label}配置无效。")
    try:
        _canonical_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SnapshotError(f"{label}身份不是规范 JSON。") from exc
    return value


def _read_regular_file(path: Path, *, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"缺少{label}。") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SnapshotError(f"{label}必须是普通文件且不能是符号链接。")
    if before.st_nlink != 1:
        raise SnapshotError(f"{label}不能是多链接文件。")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        blocks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            blocks.append(block)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise SnapshotError(f"无法读取{label}。") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SnapshotError(f"读取期间{label}发生变化。")
    return b"".join(blocks)


def _validated_directory(path: Path, *, label: str) -> None:
    try:
        status = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"缺少{label}。") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise SnapshotError(f"{label}必须是普通目录且不能是符号链接。")


def _object_payload_path(library_root: Path, kind: str, digest: str) -> Path:
    if kind not in OBJECT_KINDS:
        raise SnapshotError("对象类型无效。")
    digest = _validated_digest(digest, label=f"{kind} 对象")
    _validated_directory(library_root, label="资料库根目录")
    objects = library_root / OBJECTS_DIRECTORY
    _validated_directory(objects, label="objects 目录")
    kind_root = objects / kind
    _validated_directory(kind_root, label=f"{kind} 对象目录")
    object_directory = kind_root / digest
    _validated_directory(object_directory, label=f"{kind} 对象")
    try:
        entries = {entry.name for entry in object_directory.iterdir()}
    except OSError as exc:
        raise SnapshotError(f"无法读取{kind} 对象目录。") from exc
    if entries != {OBJECT_PAYLOAD_NAME}:
        raise SnapshotError(f"{kind} 对象目录内容无效。")
    return object_directory / OBJECT_PAYLOAD_NAME


def _read_object(
    library_root: Path,
    kind: str,
    descriptor: dict[str, Any],
) -> bytes:
    digest, size = _validated_descriptor(kind, descriptor)
    path = _object_payload_path(library_root, kind, digest)
    payload = _read_regular_file(path, label=f"{kind} 对象内容")
    if len(payload) != size or _sha256(payload) != digest:
        raise SnapshotError(f"{kind} 对象字节数或 SHA-256 不匹配。")
    return payload


def _validated_descriptor(
    kind: str,
    descriptor: object,
) -> tuple[str, int]:
    if not isinstance(descriptor, dict) or set(descriptor) != {
        "sha256",
        "byte_size",
        "path",
        "created_in_snapshot",
    }:
        raise SnapshotError(f"manifest 的 {kind} 对象记录无效。")
    digest = _validated_digest(descriptor["sha256"], label=f"{kind} 对象")
    size = descriptor["byte_size"]
    if type(size) is not int or size < 0:
        raise SnapshotError(f"manifest 的 {kind} 对象字节数无效。")
    if type(descriptor["created_in_snapshot"]) is not bool:
        raise SnapshotError(f"manifest 的 {kind} 对象复用状态无效。")
    if descriptor["path"] != _relative_path(kind, digest):
        raise SnapshotError(f"manifest 的 {kind} 对象路径与摘要不一致。")
    return digest, size


def _prepare_store(library_root: Path) -> dict[str, Path]:
    _validated_directory(library_root, label="资料库根目录")
    objects = library_root / OBJECTS_DIRECTORY
    for path, label in (
        (objects, "objects 目录"),
        *((objects / kind, f"{kind} 对象目录") for kind in OBJECT_KINDS),
    ):
        created = False
        try:
            path.mkdir()
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise SnapshotError(f"无法建立{label}。") from exc
        _validated_directory(path, label=label)
        if created:
            _fsync_directory(path.parent)
    return {kind: objects / kind for kind in OBJECT_KINDS}


def _write_new_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_directory_no_replace(source: Path, target: Path) -> None:
    if sys.platform == "darwin":
        renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        if renamex_np(os.fsencode(source), os.fsencode(target), 0x00000004) == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(error_number, os.strerror(error_number), target)
        raise OSError(error_number, os.strerror(error_number), target)
    if target.exists():
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), target)
    os.rename(source, target)


def _ensure_object(
    kind_root: Path,
    kind: str,
    digest: str,
    payload: bytes,
    required_existing: set[tuple[str, str]],
) -> bool:
    if _sha256(payload) != digest:
        raise SnapshotError(f"待写入的 {kind} 对象摘要不匹配。")
    target = kind_root / digest
    try:
        target.lstat()
    except FileNotFoundError:
        if (kind, digest) in required_existing:
            raise SnapshotError(f"已登记快照引用的 {kind} 对象缺失。") from None
    except OSError as exc:
        raise SnapshotError(f"无法读取 {kind} 对象目标状态。") from exc
    else:
        existing = _read_regular_file(
            _object_payload_path(kind_root.parent.parent, kind, digest),
            label=f"{kind} 对象内容",
        )
        if existing != payload:
            raise SnapshotError(f"已有 {kind} 对象内容与摘要不一致。")
        return False

    temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=kind_root))
    try:
        _write_new_file(temporary / OBJECT_PAYLOAD_NAME, payload)
        _fsync_directory(temporary)
        try:
            _publish_directory_no_replace(temporary, target)
        except FileExistsError:
            existing = _read_regular_file(
                _object_payload_path(kind_root.parent.parent, kind, digest),
                label=f"{kind} 对象内容",
            )
            if existing != payload:
                raise SnapshotError(f"竞争产生的 {kind} 对象内容不匹配。")
            return False
        _fsync_directory(kind_root)
        written = _read_regular_file(
            _object_payload_path(kind_root.parent.parent, kind, digest),
            label=f"{kind} 对象内容",
        )
        if written != payload:
            raise SnapshotError(f"新发布的 {kind} 对象核验失败。")
        return True
    finally:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)


def _parsed_payload(document: PreparedDocument) -> bytes:
    parsed = document.parsed
    return _canonical_json(
        {
            "format": OBJECT_STORE_FORMAT,
            "version": OBJECT_STORE_VERSION,
            "kind": "parsed",
            "identity": {
                "source_sha256": document.source_sha256,
                "parser": parsed.parser,
            },
            "payload": {
                "media_type": parsed.media_type,
                "extraction_method": parsed.extraction_method,
                "extraction_status": parsed.extraction_status,
                "title_hint": parsed.title_hint,
                "units": list(parsed.units),
                "page_count": parsed.page_count,
            },
        }
    )


def _chunk_record(chunk: ChunkDraft) -> dict[str, Any]:
    return {
        "ordinal": chunk.ordinal,
        "text": chunk.text,
        "heading_path": list(chunk.heading_path),
        "pdf_page_start": chunk.pdf_page_start,
        "pdf_page_end": chunk.pdf_page_end,
        "source_line_start": chunk.source_line_start,
        "source_line_end": chunk.source_line_end,
        "anchor_label": chunk.anchor_label,
    }


def _chunks_payload(document: PreparedDocument, parsed_sha256: str) -> bytes:
    return _canonical_json(
        {
            "format": OBJECT_STORE_FORMAT,
            "version": OBJECT_STORE_VERSION,
            "kind": "chunks",
            "identity": {
                "parsed_sha256": parsed_sha256,
                "chunker": document.chunker,
            },
            "payload": {
                "title_hint": document.chunk_title_hint,
                "extracted_char_count": document.extracted_char_count,
                "chunks": [_chunk_record(chunk) for chunk in document.chunks],
            },
        }
    )


def _descriptor(kind: str, payload: bytes) -> dict[str, Any]:
    digest = _sha256(payload)
    return {
        "sha256": digest,
        "byte_size": len(payload),
        "path": _relative_path(kind, digest),
    }


def ensure_document_objects(
    library_root: Path,
    document: PreparedDocument,
    *,
    required_existing: set[tuple[str, str]],
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, bool, int]]]:
    roots = _prepare_store(library_root)
    source_payload = document.payload
    parsed_payload = _parsed_payload(document)
    parsed_sha256 = _sha256(parsed_payload)
    chunks_payload = _chunks_payload(document, parsed_sha256)
    payloads = {
        "source": source_payload,
        "parsed": parsed_payload,
        "chunks": chunks_payload,
    }
    descriptors = {kind: _descriptor(kind, payload) for kind, payload in payloads.items()}
    if descriptors["source"]["sha256"] != document.source_sha256:
        raise SnapshotError("源对象摘要与导入摘要不一致。")
    writes = []
    for kind in OBJECT_KINDS:
        descriptor = descriptors[kind]
        created = _ensure_object(
            roots[kind],
            kind,
            descriptor["sha256"],
            payloads[kind],
            required_existing,
        )
        writes.append((kind, created, descriptor["byte_size"]))
    return descriptors, writes


def _json_object(raw: bytes, *, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SnapshotError(f"{kind} 对象不是有效 UTF-8 JSON。") from exc
    if not isinstance(value, dict) or _canonical_json(value) != raw:
        raise SnapshotError(f"{kind} 对象不是规范 JSON。")
    if (
        set(value) != {"format", "version", "kind", "identity", "payload"}
        or value["format"] != OBJECT_STORE_FORMAT
        or value["version"] != OBJECT_STORE_VERSION
        or value["kind"] != kind
        or not isinstance(value["identity"], dict)
        or not isinstance(value["payload"], dict)
    ):
        raise SnapshotError(f"{kind} 对象结构或版本无效。")
    return value


def _decode_parsed(raw: bytes, *, source_sha256: str) -> ParsedDocument:
    value = _json_object(raw, kind="parsed")
    identity = value["identity"]
    payload = value["payload"]
    if set(identity) != {"source_sha256", "parser"}:
        raise SnapshotError("parsed 对象上游身份无效。")
    if identity["source_sha256"] != source_sha256:
        raise SnapshotError("parsed 对象未绑定所引用的源对象。")
    parser = _validated_pipeline(identity["parser"], label="parser")
    if set(payload) != {
        "media_type",
        "extraction_method",
        "extraction_status",
        "title_hint",
        "units",
        "page_count",
    }:
        raise SnapshotError("parsed 对象正文结构无效。")
    if (
        payload["media_type"] not in {"text/markdown", "application/pdf"}
        or not isinstance(payload["extraction_method"], str)
        or not isinstance(payload["extraction_status"], str)
        or not isinstance(payload["title_hint"], str)
        or not isinstance(payload["units"], list)
        or not payload["units"]
        or not all(isinstance(item, str) for item in payload["units"])
    ):
        raise SnapshotError("parsed 对象正文值无效。")
    page_count = payload["page_count"]
    if payload["media_type"] == "text/markdown":
        if len(payload["units"]) != 1 or page_count is not None:
            raise SnapshotError("Markdown parsed 对象结构无效。")
    elif type(page_count) is not int or page_count < 1 or page_count != len(
        payload["units"]
    ):
        raise SnapshotError("PDF parsed 对象页数无效。")
    return ParsedDocument(
        media_type=payload["media_type"],
        extraction_method=payload["extraction_method"],
        extraction_status=payload["extraction_status"],
        title_hint=payload["title_hint"],
        units=tuple(payload["units"]),
        page_count=page_count,
        parser=parser,
    )


def _optional_positive_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise SnapshotError(f"chunks 对象的 {label} 无效。")
    return value


def _decode_chunks(
    raw: bytes,
    *,
    parsed_sha256: str,
) -> tuple[str, tuple[ChunkDraft, ...], int, dict[str, object]]:
    value = _json_object(raw, kind="chunks")
    identity = value["identity"]
    payload = value["payload"]
    if set(identity) != {"parsed_sha256", "chunker"}:
        raise SnapshotError("chunks 对象上游身份无效。")
    if identity["parsed_sha256"] != parsed_sha256:
        raise SnapshotError("chunks 对象未绑定所引用的 parsed 对象。")
    chunker = _validated_pipeline(identity["chunker"], label="chunker")
    if set(payload) != {"title_hint", "extracted_char_count", "chunks"}:
        raise SnapshotError("chunks 对象正文结构无效。")
    if (
        not isinstance(payload["title_hint"], str)
        or type(payload["extracted_char_count"]) is not int
        or payload["extracted_char_count"] < 1
        or not isinstance(payload["chunks"], list)
        or not payload["chunks"]
    ):
        raise SnapshotError("chunks 对象正文值无效。")
    chunks: list[ChunkDraft] = []
    expected_keys = {
        "ordinal",
        "text",
        "heading_path",
        "pdf_page_start",
        "pdf_page_end",
        "source_line_start",
        "source_line_end",
        "anchor_label",
    }
    for raw_chunk in payload["chunks"]:
        if not isinstance(raw_chunk, dict) or set(raw_chunk) != expected_keys:
            raise SnapshotError("chunks 对象中的切块记录无效。")
        if (
            type(raw_chunk["ordinal"]) is not int
            or raw_chunk["ordinal"] < 1
            or not isinstance(raw_chunk["text"], str)
            or not raw_chunk["text"].strip()
            or not isinstance(raw_chunk["heading_path"], list)
            or not all(isinstance(item, str) for item in raw_chunk["heading_path"])
            or not isinstance(raw_chunk["anchor_label"], str)
            or not raw_chunk["anchor_label"]
        ):
            raise SnapshotError("chunks 对象中的切块值无效。")
        chunks.append(
            ChunkDraft(
                ordinal=raw_chunk["ordinal"],
                text=raw_chunk["text"],
                heading_path=tuple(raw_chunk["heading_path"]),
                pdf_page_start=_optional_positive_int(
                    raw_chunk["pdf_page_start"], label="pdf_page_start"
                ),
                pdf_page_end=_optional_positive_int(
                    raw_chunk["pdf_page_end"], label="pdf_page_end"
                ),
                source_line_start=_optional_positive_int(
                    raw_chunk["source_line_start"], label="source_line_start"
                ),
                source_line_end=_optional_positive_int(
                    raw_chunk["source_line_end"], label="source_line_end"
                ),
                anchor_label=raw_chunk["anchor_label"],
            )
        )
    return (
        payload["title_hint"],
        tuple(chunks),
        payload["extracted_char_count"],
        chunker,
    )


def _source_objects(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects = source.get("objects")
    if not isinstance(objects, dict) or set(objects) != set(OBJECT_KINDS):
        raise SnapshotError("manifest source 缺少完整三层对象引用。")
    return objects


def referenced_object_identities(source: dict[str, Any]) -> set[tuple[str, str]]:
    objects = _source_objects(source)
    identities: set[tuple[str, str]] = set()
    for kind in OBJECT_KINDS:
        digest, _size = _validated_descriptor(kind, objects[kind])
        identities.add((kind, digest))
    return identities


def load_document(
    library_root: Path,
    source: dict[str, Any],
    *,
    current_pipeline: bool,
) -> PreparedDocument:
    objects = _source_objects(source)
    source_payload = _read_object(library_root, "source", objects["source"])
    source_sha256 = objects["source"]["sha256"]
    if (
        source.get("source_sha256") != source_sha256
        or source.get("byte_size") != len(source_payload)
        or source.get("stored_path") != objects["source"]["path"]
    ):
        raise SnapshotError("manifest source 元数据与源对象不一致。")
    source_name = source.get("source_name")
    if (
        not isinstance(source_name, str)
        or not source_name
        or Path(source_name).name != source_name
        or "/" in source_name
        or "\\" in source_name
        or "\x00" in source_name
    ):
        raise SnapshotError("manifest source_name 无效。")
    suffix = Path(source_name).suffix.lower()
    if suffix not in {".md", ".markdown", ".pdf"}:
        raise SnapshotError("manifest source 文件类型无效。")

    parsed_raw = _read_object(library_root, "parsed", objects["parsed"])
    parsed = _decode_parsed(parsed_raw, source_sha256=source_sha256)
    chunks_raw = _read_object(library_root, "chunks", objects["chunks"])
    chunk_title_hint, chunks, extracted_chars, chunker = _decode_chunks(
        chunks_raw,
        parsed_sha256=objects["parsed"]["sha256"],
    )
    exact = _prepared_document(
        source_name=source_name,
        suffix=suffix,
        payload=source_payload,
        source_sha256=source_sha256,
        parsed=parsed,
        chunk_title_hint=chunk_title_hint,
        chunks=chunks,
        extracted_char_count=extracted_chars,
        chunker=chunker,
    )
    if not current_pipeline:
        return exact

    if parsed.parser == _parser_identity(suffix):
        current_parsed = parsed
    else:
        current_parsed = _parse_payload(source_payload, suffix)
    current_parsed_raw = _canonical_json(
        {
            "format": OBJECT_STORE_FORMAT,
            "version": OBJECT_STORE_VERSION,
            "kind": "parsed",
            "identity": {
                "source_sha256": source_sha256,
                "parser": current_parsed.parser,
            },
            "payload": {
                "media_type": current_parsed.media_type,
                "extraction_method": current_parsed.extraction_method,
                "extraction_status": current_parsed.extraction_status,
                "title_hint": current_parsed.title_hint,
                "units": list(current_parsed.units),
                "page_count": current_parsed.page_count,
            },
        }
    )
    if (
        _sha256(current_parsed_raw) == objects["parsed"]["sha256"]
        and chunker == _chunker_identity(current_parsed.media_type)
    ):
        return exact
    title_hint, current_chunks, current_chars, current_chunker = _chunk_parsed(
        current_parsed
    )
    return _prepared_document(
        source_name=source_name,
        suffix=suffix,
        payload=source_payload,
        source_sha256=source_sha256,
        parsed=current_parsed,
        chunk_title_hint=title_hint,
        chunks=current_chunks,
        extracted_char_count=current_chars,
        chunker=current_chunker,
    )


def object_store_manifest() -> dict[str, Any]:
    return {"format": OBJECT_STORE_FORMAT, "version": OBJECT_STORE_VERSION}


__all__ = [
    "OBJECT_KINDS",
    "OBJECT_STORE_FORMAT",
    "OBJECT_STORE_VERSION",
    "ensure_document_objects",
    "load_document",
    "object_store_manifest",
    "referenced_object_identities",
]
