from __future__ import annotations

import ctypes
import errno
import functools
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from . import __version__
from .catalog import (
    _root_guard,
    _validated_root,
    empty_snapshot_catalog,
    load_snapshot_catalog,
    snapshot_catalog_exists,
    snapshot_catalog_lock,
    snapshot_record,
    write_snapshot_catalog,
)
from .errors import ImportPolicyError, SnapshotError
from .ingest import (
    MAX_CHUNK_CHARS,
    PreparedDocument,
    prepare_document,
    prepare_documents,
)
from .object_store import (
    OBJECT_KINDS,
    ensure_document_objects,
    load_document,
    object_store_manifest,
    referenced_object_identities,
)
from .registry import LibraryRegistry


SNAPSHOT_FORMAT = "literature-evidence-snapshot"
SCHEMA_VERSION = 2
DATABASE_NAME = "evidence.sqlite"
MANIFEST_NAME = "manifest.json"
SCHEMA_NAME = "schema.sql"
_DOCUMENT_ID = re.compile(r"\Adoc_[0-9a-f]{24}\Z")

SCHEMA_SQL = """PRAGMA foreign_keys=ON;
PRAGMA page_size=4096;
PRAGMA auto_vacuum=NONE;
CREATE TABLE artifact_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE document(
    document_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    identifiers TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    media_type TEXT NOT NULL,
    material_type TEXT NOT NULL,
    language TEXT NOT NULL,
    topics TEXT NOT NULL,
    evidence_role TEXT NOT NULL,
    fulltext_verified INTEGER NOT NULL CHECK(fulltext_verified IN (0, 1)),
    formula_verified INTEGER NOT NULL CHECK(formula_verified IN (0, 1))
);
CREATE TABLE asset(
    asset_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document(document_id),
    stored_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    extraction_method TEXT NOT NULL,
    extraction_status TEXT NOT NULL,
    page_count INTEGER,
    extracted_char_count INTEGER NOT NULL CHECK(extracted_char_count >= 0)
);
CREATE TABLE chunk(
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document(document_id),
    asset_id TEXT NOT NULL REFERENCES asset(asset_id),
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    chunk_text TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    pdf_page_start INTEGER,
    pdf_page_end INTEGER,
    source_line_start INTEGER,
    source_line_end INTEGER,
    anchor_label TEXT NOT NULL,
    fulltext_verified INTEGER NOT NULL CHECK(fulltext_verified IN (0, 1)),
    formula_verified INTEGER NOT NULL CHECK(formula_verified IN (0, 1)),
    UNIQUE(asset_id, ordinal)
);
CREATE INDEX chunk_document_idx ON chunk(document_id, ordinal);
CREATE INDEX chunk_asset_idx ON chunk(asset_id, ordinal);
CREATE VIRTUAL TABLE chunk_fts USING fts5(
    chunk_text,
    content='chunk',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER chunk_ai AFTER INSERT ON chunk BEGIN
    INSERT INTO chunk_fts(rowid, chunk_text) VALUES (new.rowid, new.chunk_text);
END;
"""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _corpus_sha256(documents: Sequence[PreparedDocument]) -> str:
    identity = sorted(
        [
            {
                "document_id": document.document_id,
                "source_name": document.source_name,
                "source_sha256": document.source_sha256,
            }
            for document in documents
        ],
        key=lambda item: item["document_id"],
    )
    return _sha256_bytes(_canonical_json(identity))


def _manifest_corpus_sha256(sources: Sequence[dict[str, Any]]) -> str:
    identity = sorted(
        [
            {
                "document_id": source.get("document_id"),
                "source_name": source.get("source_name"),
                "source_sha256": source.get("source_sha256"),
            }
            for source in sources
        ],
        key=lambda item: str(item["document_id"]),
    )
    return _sha256_bytes(_canonical_json(identity))


def _chunk_id(document_id: str, ordinal: int, text: str, anchor: str) -> str:
    payload = f"{document_id}\0{ordinal}\0{anchor}\0{text}".encode("utf-8")
    return "chunk_" + _sha256_bytes(payload)[:24]


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


def _prepare_library(
    library: Path | LibraryRegistry,
) -> tuple[Path, Path]:
    root = _validated_root(library, create=True)
    assert root is not None
    snapshots = root / "snapshots"
    if snapshots.is_symlink():
        raise ImportPolicyError("snapshots 目录不能是符号链接。")
    snapshots.mkdir(exist_ok=True)
    return root, snapshots.resolve(strict=True)


def _populate_database(
    path: Path,
    documents: Sequence[PreparedDocument],
    corpus_sha256: str,
    objects_by_document: Mapping[str, dict[str, dict[str, Any]]],
) -> dict[str, int]:
    database = sqlite3.connect(path)
    try:
        database.execute("PRAGMA journal_mode=DELETE")
        database.executescript(SCHEMA_SQL)
        metadata = {
            "artifact_format": SNAPSHOT_FORMAT,
            "schema_version": str(SCHEMA_VERSION),
            "corpus_sha256": corpus_sha256,
            "software_version": __version__,
        }
        database.executemany(
            "INSERT INTO artifact_meta(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        for document in documents:
            material_type = "pdf" if document.media_type == "application/pdf" else "markdown"
            database.execute(
                """INSERT INTO document(
                    document_id,title,identifiers,source_name,source_type,media_type,
                    material_type,language,topics,evidence_role,fulltext_verified,
                    formula_verified
                ) VALUES(?,?,?,?,?,?,?,?,?,?,0,0)""",
                (
                    document.document_id,
                    document.title,
                    "{}",
                    document.source_name,
                    "local_file",
                    document.media_type,
                    material_type,
                    "und",
                    "[]",
                    "imported_source",
                ),
            )
            stored_path = objects_by_document[document.document_id]["source"]["path"]
            database.execute(
                """INSERT INTO asset(
                    asset_id,document_id,stored_path,source_sha256,byte_size,
                    extraction_method,extraction_status,page_count,extracted_char_count
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    document.asset_id,
                    document.document_id,
                    stored_path,
                    document.source_sha256,
                    document.byte_size,
                    document.extraction_method,
                    document.extraction_status,
                    document.page_count,
                    document.extracted_char_count,
                ),
            )
            for chunk in document.chunks:
                database.execute(
                    """INSERT INTO chunk(
                        chunk_id,document_id,asset_id,ordinal,chunk_text,heading_path,
                        pdf_page_start,pdf_page_end,source_line_start,source_line_end,
                        anchor_label,fulltext_verified,formula_verified
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0)""",
                    (
                        _chunk_id(
                            document.document_id,
                            chunk.ordinal,
                            chunk.text,
                            chunk.anchor_label,
                        ),
                        document.document_id,
                        document.asset_id,
                        chunk.ordinal,
                        chunk.text,
                        json.dumps(chunk.heading_path, ensure_ascii=False),
                        chunk.pdf_page_start,
                        chunk.pdf_page_end,
                        chunk.source_line_start,
                        chunk.source_line_end,
                        chunk.anchor_label,
                    ),
                )
        database.commit()
        integrity = database.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise SnapshotError("临时数据库完整性检查失败。")
        if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SnapshotError("临时数据库外键检查失败。")
        counts = {
            "documents": database.execute("SELECT count(*) FROM document").fetchone()[0],
            "assets": database.execute("SELECT count(*) FROM asset").fetchone()[0],
            "chunks": database.execute("SELECT count(*) FROM chunk").fetchone()[0],
            "fts_rows": database.execute("SELECT count(*) FROM chunk_fts").fetchone()[0],
        }
        if counts["chunks"] != counts["fts_rows"]:
            raise SnapshotError("临时数据库正文片段与 FTS 行数不一致。")
        return counts
    finally:
        database.close()


def _readonly_uri(path: Path) -> str:
    return path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"


def _open_readonly(path: Path) -> sqlite3.Connection:
    database: sqlite3.Connection | None = None
    try:
        database = sqlite3.connect(_readonly_uri(path), uri=True)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA temp_store=MEMORY")
        if database.execute("PRAGMA temp_store").fetchone()[0] != 2:
            raise SnapshotError("SQLite 未将临时排序数据限制在内存。")
        database.execute("PRAGMA query_only=ON")
        if database.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise SnapshotError("SQLite 未进入 query_only 只读状态。")
        return database
    except SnapshotError:
        if database is not None:
            database.close()
        raise
    except (OSError, ValueError, sqlite3.Error) as exc:
        if database is not None:
            database.close()
        raise SnapshotError("无法以只读方式打开冻结 SQLite 数据库。") from exc


def _sqlite_schema_sha256_from_connection(database: sqlite3.Connection) -> str:
    rows = database.execute(
        """SELECT type,name,tbl_name,sql FROM sqlite_schema
        WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
        ORDER BY type,name,tbl_name"""
    ).fetchall()
    normalized = [list(row) for row in rows]
    return _sha256_bytes(_canonical_json(normalized))


def _sqlite_schema_sha256(path: Path) -> str:
    database = _open_readonly(path)
    try:
        return _sqlite_schema_sha256_from_connection(database)
    finally:
        database.close()


@functools.lru_cache(maxsize=1)
def _expected_sqlite_schema_sha256() -> str:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    try:
        database.executescript(SCHEMA_SQL)
        return _sqlite_schema_sha256_from_connection(database)
    finally:
        database.close()


def _serialized_database(path: Path) -> bytes:
    database = _open_readonly(path)
    try:
        if not hasattr(database, "serialize"):
            raise SnapshotError(
                "当前 Python 的 SQLite 缺少 serialize 支持，无法绑定核验与检索镜像。"
            )
        return database.serialize()
    except sqlite3.Error as exc:
        raise SnapshotError("无法读取冻结 SQLite 数据库镜像。") from exc
    finally:
        database.close()


def _open_serialized_readonly(payload: bytes) -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    try:
        if not hasattr(database, "deserialize"):
            raise SnapshotError(
                "当前 Python 的 SQLite 缺少 deserialize 支持，无法打开已核验镜像。"
            )
        database.deserialize(payload)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA temp_store=MEMORY")
        if database.execute("PRAGMA temp_store").fetchone()[0] != 2:
            raise SnapshotError("内存 SQLite 镜像未使用内存临时区。")
        database.execute("PRAGMA query_only=ON")
        if database.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise SnapshotError("内存 SQLite 镜像未进入 query_only 只读状态。")
        return database
    except SnapshotError:
        database.close()
        raise
    except sqlite3.Error as exc:
        database.close()
        raise SnapshotError("无法打开已核验的 SQLite 镜像。") from exc
    except BaseException:
        database.close()
        raise


def _sha256_regular_file(path: Path, *, label: str) -> tuple[str, int]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"快照缺少{label}。") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SnapshotError(f"{label}必须是普通文件且不能是符号链接。")
    if before.st_nlink != 1:
        raise SnapshotError(f"{label}不能是多链接文件。")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            digest = hashlib.sha256()
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SnapshotError(f"无法读取{label}。") from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SnapshotError(f"核验期间{label}发生变化。")
    return digest.hexdigest(), after.st_size


def _schema_sql_sha256() -> str:
    return _sha256_bytes(SCHEMA_SQL.encode("utf-8"))


def _dependency_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _source_manifest(
    document: PreparedDocument,
    objects: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "asset_id": document.asset_id,
        "source_name": document.source_name,
        "stored_path": objects["source"]["path"],
        "media_type": document.media_type,
        "title": document.title,
        "source_sha256": document.source_sha256,
        "byte_size": document.byte_size,
        "extraction_method": document.extraction_method,
        "extraction_status": document.extraction_status,
        "page_count": document.page_count,
        "extracted_char_count": document.extracted_char_count,
        "chunk_count": len(document.chunks),
        "fulltext_verification": "unverified",
        "formula_verification": "unverified",
        "objects": objects,
    }


def _storage_statistics(
    objects_by_document: Mapping[str, dict[str, dict[str, Any]]],
) -> dict[str, int]:
    references = [
        objects[kind]
        for objects in objects_by_document.values()
        for kind in OBJECT_KINDS
    ]
    new_objects = [item for item in references if item["created_in_snapshot"]]
    created_identities = [
        (item["path"], item["sha256"]) for item in new_objects
    ]
    if len(created_identities) != len(set(created_identities)):
        raise SnapshotError("同一对象不能在一个快照中重复记为新增。")
    logical_bytes = sum(item["byte_size"] for item in references)
    new_bytes = sum(item["byte_size"] for item in new_objects)
    return {
        "object_references": len(references),
        "new_objects": len(new_objects),
        "reused_objects": len(references) - len(new_objects),
        "logical_object_bytes": logical_bytes,
        "new_object_bytes": new_bytes,
        "reused_object_bytes": logical_bytes - new_bytes,
    }


def _cleanup_build_directory(path: Path, snapshots_root: Path) -> None:
    try:
        inside = path.parent.resolve(strict=True) == snapshots_root.resolve(strict=True)
    except OSError:
        inside = False
    if inside and path.name.startswith(".building-") and path.exists() and not path.is_symlink():
        shutil.rmtree(path)


def _publish_directory_no_replace(source: Path, target: Path) -> None:
    """Atomically publish on macOS without replacing an existing snapshot."""
    if sys.platform == "darwin":
        renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(os.fsencode(source), os.fsencode(target), 0x00000004)
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise SnapshotError("目标快照已存在；未覆盖任何已有快照。")
        raise OSError(error_number, os.strerror(error_number), str(target))
    if target.exists():
        raise SnapshotError("目标快照已存在；未覆盖任何已有快照。")
    os.rename(source, target)


def _publish_prepared_snapshot(
    library_root: Path,
    snapshots_root: Path,
    documents: Sequence[PreparedDocument],
    *,
    reserved_snapshot_ids: set[str],
    required_existing_objects: set[tuple[str, str]],
) -> dict[str, Any]:
    """Build, verify, and no-replace publish one complete prepared snapshot."""
    corpus_sha256 = _corpus_sha256(documents)
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    snapshot_id = f"{timestamp}-{corpus_sha256[:12]}-{uuid.uuid4().hex[:8]}"
    if snapshot_id in reserved_snapshot_ids:
        raise SnapshotError("新快照 ID 已在目录册中使用；不会重复发布。")
    final_directory = snapshots_root / snapshot_id
    if final_directory.exists():
        raise SnapshotError("新快照 ID 意外冲突；未覆盖任何已有快照。")
    temporary_directory = Path(
        tempfile.mkdtemp(prefix=".building-", dir=snapshots_root)
    )

    try:
        objects_by_document: dict[str, dict[str, dict[str, Any]]] = {}
        for document in documents:
            objects, writes = ensure_document_objects(
                library_root,
                document,
                required_existing=required_existing_objects,
            )
            created = {kind: was_created for kind, was_created, _size in writes}
            objects_by_document[document.document_id] = {
                kind: {
                    **objects[kind],
                    "created_in_snapshot": created[kind],
                }
                for kind in OBJECT_KINDS
            }
        storage = _storage_statistics(objects_by_document)

        schema_path = temporary_directory / SCHEMA_NAME
        _write_new_file(schema_path, SCHEMA_SQL.encode("utf-8"))
        database_path = temporary_directory / DATABASE_NAME
        counts = _populate_database(
            database_path,
            documents,
            corpus_sha256,
            objects_by_document,
        )
        database_hash, database_size = _sha256_regular_file(
            database_path, label="SQLite 数据库"
        )
        sqlite_schema_hash = _sqlite_schema_sha256(database_path)

        manifest = {
            "format": SNAPSHOT_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "created_at": created_at,
            "corpus_sha256": corpus_sha256,
            "software": {
                "literature_evidence_mcp": __version__,
                "python": ".".join(map(str, sys.version_info[:3])),
                "sqlite": sqlite3.sqlite_version,
                "pypdf": _dependency_version("pypdf"),
            },
            "schema": {
                "path": SCHEMA_NAME,
                "sha256": _schema_sql_sha256(),
                "sqlite_schema_sha256": sqlite_schema_hash,
            },
            "database": {
                "path": DATABASE_NAME,
                "sha256": database_hash,
                "byte_size": database_size,
            },
            "counts": counts,
            "object_store": object_store_manifest(),
            "storage": storage,
            "sources": [
                _source_manifest(document, objects_by_document[document.document_id])
                for document in documents
            ],
            "limitations": [
                "BM25 uses SQLite FTS5 unicode61 without language-specific tokenization.",
                "Text extraction does not promote fulltext or formula verification.",
                "No result means only that this search returned no evidence.",
            ],
        }
        manifest_path = temporary_directory / MANIFEST_NAME
        manifest_payload = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8") + b"\n"
        _write_new_file(manifest_path, manifest_payload)
        _fsync_directory(temporary_directory)
        verified, _database_image = _verify_snapshot(
            temporary_directory, require_directory_name=False
        )
        _publish_directory_no_replace(temporary_directory, final_directory)
        _fsync_directory(snapshots_root)
    except BaseException:
        _cleanup_build_directory(temporary_directory, snapshots_root)
        raise

    return {
        "snapshot_id": snapshot_id,
        "snapshot_path": str(final_directory),
        "manifest_sha256": verified["manifest_sha256"],
        "corpus_sha256": corpus_sha256,
        "database_sha256": database_hash,
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
        "storage": storage,
    }


def _validated_document_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _DOCUMENT_ID.fullmatch(value) is None:
        raise ImportPolicyError(f"{label} 必须是有效的 document_id。")
    return value


def _base_documents(
    library_root: Path,
    snapshots_root: Path,
    catalog: dict[str, Any],
    base_snapshot_id: str,
) -> tuple[PreparedDocument, ...]:
    record = snapshot_record(catalog, base_snapshot_id)
    snapshot_directory = snapshots_root / base_snapshot_id
    status, _database_image = _verify_snapshot(
        snapshot_directory, require_directory_name=True
    )
    if status["manifest_sha256"] != record["manifest_sha256"]:
        raise SnapshotError("基础快照与目录册绑定的内容不一致。")
    manifest, manifest_sha256 = _load_manifest(snapshot_directory)
    if manifest_sha256 != record["manifest_sha256"]:
        raise SnapshotError("读取期间基础快照 manifest 发生变化。")

    documents: list[PreparedDocument] = []
    for source in manifest["sources"]:
        document = load_document(
            library_root,
            source,
            current_pipeline=True,
        )
        if (
            document.document_id != source.get("document_id")
            or document.asset_id != source.get("asset_id")
            or document.source_sha256 != source.get("source_sha256")
        ):
            raise SnapshotError("基础快照成员在继承读取期间发生变化。")
        documents.append(document)
    return tuple(sorted(documents, key=lambda item: item.document_id))


def _catalog_object_identities(
    snapshots_root: Path,
    catalog: dict[str, Any],
) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for record in catalog["snapshots"]:
        snapshot_directory = snapshots_root / record["snapshot_id"]
        try:
            status = snapshot_directory.lstat()
        except OSError as exc:
            raise SnapshotError("找不到已登记快照目录。") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise SnapshotError("已登记快照必须是普通目录且不能是符号链接。")
        manifest, manifest_sha256 = _load_manifest(snapshot_directory)
        if manifest_sha256 != record["manifest_sha256"]:
            raise SnapshotError("已登记快照的 manifest 与目录册绑定不一致。")
        if manifest.get("object_store") != object_store_manifest():
            raise SnapshotError("已登记快照缺少受支持的对象库声明。")
        if manifest.get("snapshot_id") != record["snapshot_id"]:
            raise SnapshotError("已登记快照目录名与 manifest 身份不一致。")
        sources = manifest.get("sources")
        if not isinstance(sources, list):
            raise SnapshotError("已登记快照的完整成员清单无效。")
        for source in sources:
            if not isinstance(source, dict):
                raise SnapshotError("已登记快照的成员记录无效。")
            identities.update(referenced_object_identities(source))
    return identities


def build_snapshot(
    library: Path | LibraryRegistry,
    sources: Sequence[Path],
    *,
    base_snapshot_id: str | None = None,
    replacements: Mapping[str, Path] | None = None,
    remove_document_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Publish one complete snapshot, optionally derived from an explicit base."""
    root_guard = _root_guard(library)
    if isinstance(remove_document_ids, (str, bytes)):
        raise ImportPolicyError("remove_document_ids 必须是 document_id 列表。")
    removed = tuple(
        _validated_document_id(value, label="待移除项")
        for value in remove_document_ids
    )
    if len(removed) != len(set(removed)):
        raise ImportPolicyError("待移除的 document_id 不能重复。")

    replacement_paths = {} if replacements is None else replacements
    if not isinstance(replacement_paths, Mapping):
        raise ImportPolicyError("replacements 必须是 document_id 到源文件的映射。")
    prepared_replacements: dict[str, PreparedDocument] = {}
    for raw_document_id, source in replacement_paths.items():
        document_id = _validated_document_id(raw_document_id, label="待替换项")
        if document_id in prepared_replacements:
            raise ImportPolicyError("待替换的 document_id 不能重复。")
        prepared_replacements[document_id] = prepare_document(source)
    if set(removed) & set(prepared_replacements):
        raise ImportPolicyError("同一 document_id 不能同时替换和移除。")

    additions = prepare_documents(sources) if sources else ()
    if base_snapshot_id is None:
        if removed or prepared_replacements:
            raise ImportPolicyError("替换或移除必须明确提供 base_snapshot_id。")
        if not additions:
            raise ImportPolicyError("请至少选择一个 Markdown 或 PDF 文件。")

    library_root, snapshots_root = _prepare_library(root_guard)
    with snapshot_catalog_lock(root_guard):
        catalog = load_snapshot_catalog(root_guard)
        if not snapshot_catalog_exists(root_guard):
            write_snapshot_catalog(root_guard, empty_snapshot_catalog())
            catalog = empty_snapshot_catalog()
        required_existing_objects = _catalog_object_identities(
            snapshots_root,
            catalog,
        )

        members: dict[str, PreparedDocument]
        if base_snapshot_id is None:
            members = {}
        else:
            members = {
                document.document_id: document
                for document in _base_documents(
                    library_root, snapshots_root, catalog, base_snapshot_id
                )
            }
            requested = set(removed) | set(prepared_replacements)
            missing = requested - set(members)
            if missing:
                raise ImportPolicyError("待替换或移除的 document_id 不属于基础快照。")
            for document_id in removed:
                del members[document_id]
            for document_id, document in prepared_replacements.items():
                del members[document_id]
                if document.document_id in members:
                    raise ImportPolicyError("替换文件与另一现有成员重复。")
                members[document.document_id] = document

        for document in additions:
            if document.document_id in members:
                raise ImportPolicyError("新增文件与快照中的成员重复。")
            members[document.document_id] = document
        if not members:
            raise ImportPolicyError("完整快照必须至少包含一个文件。")

        result = _publish_prepared_snapshot(
            library_root,
            snapshots_root,
            tuple(sorted(members.values(), key=lambda item: item.document_id)),
            reserved_snapshot_ids={
                item["snapshot_id"] for item in catalog["snapshots"]
            },
            required_existing_objects=required_existing_objects,
        )
        record = {
            "snapshot_id": result["snapshot_id"],
            "manifest_sha256": result["manifest_sha256"],
            "base_snapshot_id": base_snapshot_id,
        }
        next_catalog = {
            "format": catalog["format"],
            "version": catalog["version"],
            "current_snapshot_id": (
                catalog["current_snapshot_id"] or result["snapshot_id"]
            ),
            "last_successful_snapshot_id": result["snapshot_id"],
            "snapshots": [*catalog["snapshots"], record],
        }
        try:
            write_snapshot_catalog(root_guard, next_catalog)
        except BaseException:
            # An asynchronous interruption can arrive after the atomic catalog
            # replace but before the writer returns.  Never delete a directory
            # whose ID is already visible in the authoritative catalog.
            registered: bool | None = None
            try:
                persisted = load_snapshot_catalog(root_guard)
            except (SnapshotError, OSError):
                pass
            else:
                registered = any(
                    item["snapshot_id"] == result["snapshot_id"]
                    for item in persisted["snapshots"]
                )
            if registered is False:
                published = Path(result["snapshot_path"])
                try:
                    shutil.rmtree(published)
                    _fsync_directory(snapshots_root)
                except OSError:
                    pass
            raise

    result.update(
        {
            "base_snapshot_id": base_snapshot_id,
            "current_snapshot_id": next_catalog["current_snapshot_id"],
            "last_successful_snapshot_id": next_catalog[
                "last_successful_snapshot_id"
            ],
        }
    )
    return result


def _load_manifest(snapshot_directory: Path) -> tuple[dict[str, Any], str]:
    manifest_path = snapshot_directory / MANIFEST_NAME
    try:
        before = manifest_path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise SnapshotError("manifest.json 必须是普通文件且不能是符号链接。")
        if before.st_nlink != 1:
            raise SnapshotError("manifest.json 不能是多链接文件。")
        if before.st_size > 8 * 1024 * 1024:
            raise SnapshotError("manifest.json 超过 8 MiB 上限。")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(manifest_path, flags)
        try:
            raw = bytearray()
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                raw.extend(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SnapshotError("读取期间 manifest.json 发生变化。")
        digest = _sha256_bytes(bytes(raw))
        payload = json.loads(bytes(raw).decode("utf-8"))
    except SnapshotError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise SnapshotError("manifest.json 不是有效 UTF-8 JSON。") from exc
    if not isinstance(payload, dict):
        raise SnapshotError("manifest.json 顶层必须是对象。")
    return payload, digest


def _snapshot_member(snapshot_directory: Path, raw: object) -> Path:
    if not isinstance(raw, str):
        raise SnapshotError("manifest 中的快照相对路径无效。")
    relative = PurePosixPath(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts or "\\" in raw:
        raise SnapshotError("manifest 中的快照相对路径不安全。")
    target = snapshot_directory.joinpath(*relative.parts)
    try:
        resolved_parent = target.parent.resolve(strict=True)
        resolved_root = snapshot_directory.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise SnapshotError("manifest 中的快照路径越界或不存在。") from exc
    return target


def _snapshot_library_root(snapshot_directory: Path) -> Path:
    snapshots = snapshot_directory.parent
    library_root = snapshots.parent
    if snapshots.name != "snapshots":
        raise SnapshotError("快照不位于固定 snapshots 目录中。")
    for path, label in (
        (snapshots, "snapshots 目录"),
        (library_root, "资料库根目录"),
    ):
        try:
            status = path.lstat()
        except OSError as exc:
            raise SnapshotError(f"无法读取{label}状态。") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise SnapshotError(f"{label}必须是普通目录且不能是符号链接。")
    return library_root


def _database_counts(database: sqlite3.Connection) -> dict[str, int]:
    try:
        return {
            "documents": database.execute("SELECT count(*) FROM document").fetchone()[0],
            "assets": database.execute("SELECT count(*) FROM asset").fetchone()[0],
            "chunks": database.execute("SELECT count(*) FROM chunk").fetchone()[0],
            "fts_rows": database.execute("SELECT count(*) FROM chunk_fts").fetchone()[0],
        }
    except sqlite3.Error as exc:
        raise SnapshotError("SQLite schema 或计数不可读。") from exc


def _json_object_of_strings(raw: object, *, label: str) -> dict[str, str]:
    if not isinstance(raw, str):
        raise SnapshotError(f"SQLite 中的 {label} 不是有效 JSON 文本。")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SnapshotError(f"SQLite 中的 {label} 不是有效 JSON。") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise SnapshotError(
            f"SQLite 中的 {label} 必须是字符串到字符串的 JSON 对象。"
        )
    return value


def _json_string_list(raw: object, *, label: str) -> list[str]:
    if not isinstance(raw, str):
        raise SnapshotError(f"SQLite 中的 {label} 不是有效 JSON 文本。")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SnapshotError(f"SQLite 中的 {label} 不是有效 JSON。") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SnapshotError(f"SQLite 中的 {label} 必须是字符串 JSON 列表。")
    return value


def _validate_document_semantics(row: sqlite3.Row) -> None:
    if row["source_type"] != "local_file":
        raise SnapshotError("v0.1 SQLite document.source_type 必须是 local_file。")
    expected_material_type = {
        "text/markdown": "markdown",
        "application/pdf": "pdf",
    }.get(row["media_type"])
    if expected_material_type is None or row["material_type"] != expected_material_type:
        raise SnapshotError(
            "v0.1 SQLite document.material_type 与 media_type 不一致。"
        )


def _validate_chunk_semantics(chunks: Sequence[sqlite3.Row]) -> None:
    next_ordinal_by_asset: dict[str, int] = {}
    for row in chunks:
        asset_id = row["asset_id"]
        if row["document_id"] != row["asset_document_id"]:
            raise SnapshotError(
                "SQLite chunk 的 document_id 与 asset 的 document_id 不一致。"
            )
        expected_ordinal = next_ordinal_by_asset.get(asset_id, 1)
        if type(row["ordinal"]) is not int or row["ordinal"] != expected_ordinal:
            raise SnapshotError("v0.1 SQLite chunk 序号必须从 1 连续排列。")
        next_ordinal_by_asset[asset_id] = expected_ordinal + 1

        chunk_text = row["chunk_text"]
        if not isinstance(chunk_text, str) or not chunk_text.strip():
            raise SnapshotError("v0.1 SQLite chunk.chunk_text 不得为空。")
        if len(chunk_text) > MAX_CHUNK_CHARS:
            raise SnapshotError(
                f"v0.1 SQLite chunk.chunk_text 不得超过 {MAX_CHUNK_CHARS} 字符。"
            )
        heading_path = _json_string_list(row["heading_path"], label="heading_path")
        page_start = row["pdf_page_start"]
        page_end = row["pdf_page_end"]
        line_start = row["source_line_start"]
        line_end = row["source_line_end"]

        if row["media_type"] == "text/markdown":
            if page_start is not None or page_end is not None:
                raise SnapshotError("v0.1 Markdown chunk 不得包含 PDF 页码。")
            if (
                type(line_start) is not int
                or type(line_end) is not int
                or line_start < 1
                or line_start > line_end
            ):
                raise SnapshotError("v0.1 Markdown chunk 行号范围无效。")
            expected_anchor = f"Markdown lines {line_start}-{line_end}"
            if heading_path:
                expected_anchor += " · " + " / ".join(heading_path)
        elif row["media_type"] == "application/pdf":
            page_count = row["page_count"]
            if heading_path or line_start is not None or line_end is not None:
                raise SnapshotError(
                    "v0.1 PDF chunk 不得包含标题路径或 Markdown 行号。"
                )
            if (
                type(page_start) is not int
                or type(page_end) is not int
                or type(page_count) is not int
                or page_start < 1
                or page_start != page_end
                or page_end > page_count
            ):
                raise SnapshotError("v0.1 PDF chunk 页码范围无效。")
            expected_anchor = f"PDF page {page_start}"
        else:
            raise SnapshotError("v0.1 SQLite chunk 引用了不支持的媒体类型。")

        if row["anchor_label"] != expected_anchor:
            raise SnapshotError(
                "v0.1 SQLite chunk 的 anchor_label 与定位字段不一致。"
            )
        expected_chunk_id = _chunk_id(
            row["document_id"], row["ordinal"], chunk_text, row["anchor_label"]
        )
        if row["chunk_id"] != expected_chunk_id:
            raise SnapshotError("v0.1 SQLite chunk.chunk_id 与派生身份不一致。")


def _verify_snapshot(
    snapshot: Path, *, require_directory_name: bool
) -> tuple[dict[str, Any], bytes]:
    """Verify one exact database image plus its manifest and frozen sources."""
    directory = Path(snapshot).expanduser()
    if directory.is_symlink():
        raise SnapshotError("快照目录不能是符号链接。")
    try:
        directory = directory.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError("找不到快照目录。") from exc
    if not directory.is_dir():
        raise SnapshotError("快照路径不是目录。")
    manifest, manifest_hash = _load_manifest(directory)
    if manifest.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError("快照 format 不匹配。")
    if type(manifest.get("schema_version")) is not int or manifest.get(
        "schema_version"
    ) != SCHEMA_VERSION:
        raise SnapshotError("快照 schema_version 不受支持。")
    if require_directory_name and manifest.get("snapshot_id") != directory.name:
        raise SnapshotError("快照目录名与 snapshot_id 不一致。")
    library_root = _snapshot_library_root(directory)

    for suffix in ("-journal", "-wal", "-shm"):
        if (directory / (DATABASE_NAME + suffix)).exists():
            raise SnapshotError("冻结 SQLite 快照旁存在 sidecar 文件。")
    expected_root_entries = {MANIFEST_NAME, SCHEMA_NAME, DATABASE_NAME}
    actual_root_entries = {entry.name for entry in directory.iterdir()}
    if actual_root_entries != expected_root_entries:
        raise SnapshotError("快照根目录含未登记文件或缺少固定文件。")

    schema_info = manifest.get("schema")
    database_info = manifest.get("database")
    sources = manifest.get("sources")
    counts = manifest.get("counts")
    object_store = manifest.get("object_store")
    storage = manifest.get("storage")
    if not isinstance(schema_info, dict) or not isinstance(database_info, dict):
        raise SnapshotError("manifest 缺少 schema 或 database 记录。")
    if schema_info.get("path") != SCHEMA_NAME:
        raise SnapshotError("manifest schema.path 必须固定为 schema.sql。")
    if database_info.get("path") != DATABASE_NAME:
        raise SnapshotError("manifest database.path 必须固定为 evidence.sqlite。")
    if not isinstance(sources, list) or not isinstance(counts, dict):
        raise SnapshotError("manifest 缺少 sources 或 counts 记录。")
    if object_store != object_store_manifest():
        raise SnapshotError("manifest 对象库格式或版本不受支持。")
    if not isinstance(storage, dict) or set(storage) != {
        "object_references",
        "new_objects",
        "reused_objects",
        "logical_object_bytes",
        "new_object_bytes",
        "reused_object_bytes",
    } or any(type(value) is not int or value < 0 for value in storage.values()):
        raise SnapshotError("manifest storage 统计无效。")
    if set(counts) != {"documents", "assets", "chunks", "fts_rows"} or any(
        type(value) is not int or value < 0 for value in counts.values()
    ):
        raise SnapshotError("manifest counts 记录无效。")
    software = manifest.get("software")
    if not isinstance(software, dict) or not isinstance(
        software.get("literature_evidence_mcp"), str
    ):
        raise SnapshotError("manifest 缺少构建软件版本。")

    schema_path = _snapshot_member(directory, schema_info.get("path"))
    schema_hash, _ = _sha256_regular_file(schema_path, label="schema.sql")
    if schema_hash != schema_info.get("sha256") or schema_hash != _schema_sql_sha256():
        raise SnapshotError("schema.sql 哈希不匹配。")

    database_path = _snapshot_member(directory, database_info.get("path"))
    database_hash, database_size = _sha256_regular_file(
        database_path, label="SQLite 数据库"
    )
    if database_hash != database_info.get("sha256"):
        raise SnapshotError("SQLite 数据库 SHA-256 不匹配。")
    if database_size != database_info.get("byte_size"):
        raise SnapshotError("SQLite 数据库字节数不匹配。")
    database_image = _serialized_database(database_path)
    if _sha256_bytes(database_image) != database_hash or len(database_image) != database_size:
        raise SnapshotError("SQLite 数据库在哈希核验与读取之间发生变化。")

    expected_document_ids: set[str] = set()
    expected_asset_ids: set[str] = set()
    object_documents: dict[str, PreparedDocument] = {}
    objects_by_document: dict[str, dict[str, dict[str, Any]]] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise SnapshotError("manifest source 记录无效。")
        document_id = source.get("document_id")
        asset_id = source.get("asset_id")
        if not isinstance(document_id, str) or not isinstance(asset_id, str):
            raise SnapshotError("manifest source 缺少 document_id 或 asset_id。")
        expected_document_ids.add(document_id)
        expected_asset_ids.add(asset_id)
        if source.get("fulltext_verification") != "unverified" or source.get(
            "formula_verification"
        ) != "unverified":
            raise SnapshotError("v0.1 快照不得自动提升全文或公式核验状态。")
        document = load_document(library_root, source, current_pipeline=False)
        object_documents[document_id] = document
        objects_by_document[document_id] = source["objects"]
    if len(expected_document_ids) != len(sources) or len(expected_asset_ids) != len(
        sources
    ):
        raise SnapshotError("manifest 中的 document_id 或 asset_id 重复。")
    if _manifest_corpus_sha256(sources) != manifest.get("corpus_sha256"):
        raise SnapshotError("manifest corpus_sha256 与源记录不一致。")
    if _storage_statistics(objects_by_document) != storage:
        raise SnapshotError("manifest storage 统计与对象引用不一致。")

    database = _open_serialized_readonly(database_image)
    try:
        actual_sqlite_schema_hash = _sqlite_schema_sha256_from_connection(database)
        if actual_sqlite_schema_hash != schema_info.get("sqlite_schema_sha256"):
            raise SnapshotError("SQLite 实际 schema 哈希与 manifest 不匹配。")
        if actual_sqlite_schema_hash != _expected_sqlite_schema_sha256():
            raise SnapshotError(
                f"SQLite 实际 schema 不符合 schema_version {SCHEMA_VERSION}。"
            )
        integrity = database.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise SnapshotError("SQLite integrity_check 未通过。")
        if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SnapshotError("SQLite foreign_key_check 未通过。")
        actual_counts = _database_counts(database)
        metadata = dict(database.execute("SELECT key,value FROM artifact_meta"))
        database_sources = database.execute(
            """SELECT
                d.document_id,d.title,d.identifiers,d.source_name,d.source_type,
                d.media_type,d.material_type,d.topics,
                d.fulltext_verified,d.formula_verified,
                a.asset_id,a.stored_path,a.source_sha256,a.byte_size,
                a.extraction_method,a.extraction_status,a.page_count,
                a.extracted_char_count,
                (SELECT count(*) FROM chunk c WHERE c.asset_id=a.asset_id) AS chunk_count
            FROM document d JOIN asset a ON a.document_id=d.document_id
            ORDER BY d.document_id"""
        ).fetchall()
        database_chunks = database.execute(
            """SELECT
                c.chunk_id,c.document_id,c.asset_id,c.ordinal,c.chunk_text,c.heading_path,
                c.pdf_page_start,c.pdf_page_end,c.source_line_start,c.source_line_end,
                c.anchor_label,a.document_id AS asset_document_id,a.page_count,
                d.media_type
            FROM chunk c
            JOIN asset a ON a.asset_id=c.asset_id
            JOIN document d ON d.document_id=c.document_id
            ORDER BY c.asset_id,c.ordinal"""
        ).fetchall()
        promoted_chunk = database.execute(
            """SELECT 1 FROM chunk
            WHERE fulltext_verified != 0 OR formula_verified != 0 LIMIT 1"""
        ).fetchone()
    except sqlite3.Error as exc:
        raise SnapshotError("SQLite 完整性或元数据检查失败。") from exc
    finally:
        database.close()
    if actual_counts != counts:
        raise SnapshotError("SQLite 实际数量与 manifest 不一致。")
    if actual_counts["documents"] != len(sources) or actual_counts["assets"] != len(
        sources
    ):
        raise SnapshotError("v0.1 的 document/asset/source 一对一数量不一致。")
    if actual_counts["chunks"] != actual_counts["fts_rows"]:
        raise SnapshotError("SQLite chunk 与 FTS 行数不一致。")
    if metadata != {
        "artifact_format": SNAPSHOT_FORMAT,
        "schema_version": str(SCHEMA_VERSION),
        "corpus_sha256": manifest.get("corpus_sha256"),
        "software_version": software["literature_evidence_mcp"],
    }:
        raise SnapshotError("SQLite artifact_meta 与 manifest/软件版本不一致。")
    if promoted_chunk is not None:
        raise SnapshotError("v0.1 chunk 不得自动提升全文或公式核验状态。")
    _validate_chunk_semantics(database_chunks)
    for row in database_chunks:
        document = object_documents.get(row["document_id"])
        if document is None or not 1 <= row["ordinal"] <= len(document.chunks):
            raise SnapshotError("SQLite chunk 未对应到 chunks 对象。")
        expected = document.chunks[row["ordinal"] - 1]
        if (
            row["asset_id"] != document.asset_id
            or row["chunk_text"] != expected.text
            or _json_string_list(row["heading_path"], label="heading_path")
            != list(expected.heading_path)
            or row["pdf_page_start"] != expected.pdf_page_start
            or row["pdf_page_end"] != expected.pdf_page_end
            or row["source_line_start"] != expected.source_line_start
            or row["source_line_end"] != expected.source_line_end
            or row["anchor_label"] != expected.anchor_label
        ):
            raise SnapshotError("SQLite chunk 与内容寻址 chunks 对象不一致。")
    source_by_document = {source["document_id"]: source for source in sources}
    for row in database_sources:
        source = source_by_document.get(row["document_id"])
        if source is None:
            raise SnapshotError("SQLite document 未登记在 manifest sources。")
        comparable = {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "source_name": row["source_name"],
            "media_type": row["media_type"],
            "stored_path": row["stored_path"],
            "source_sha256": row["source_sha256"],
            "byte_size": row["byte_size"],
            "extraction_method": row["extraction_method"],
            "extraction_status": row["extraction_status"],
            "page_count": row["page_count"],
            "extracted_char_count": row["extracted_char_count"],
            "chunk_count": row["chunk_count"],
        }
        if any(source.get(key) != value for key, value in comparable.items()):
            raise SnapshotError("SQLite source 元数据与 manifest 不一致。")
        if row["chunk_count"] < 1:
            raise SnapshotError("v0.1 每个 asset 必须至少包含一个 chunk。")
        _validate_document_semantics(row)
        _json_object_of_strings(row["identifiers"], label="identifiers")
        _json_string_list(row["topics"], label="topics")
        if row["fulltext_verified"] != 0 or row["formula_verified"] != 0:
            raise SnapshotError("v0.1 SQLite 不得自动提升全文或公式核验状态。")
    for source in sources:
        document = object_documents[source["document_id"]]
        if _source_manifest(document, source["objects"]) != source:
            raise SnapshotError("manifest source 元数据与对象内容不一致。")

    status = {
        "verified": True,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_path": str(directory),
        "manifest_sha256": manifest_hash,
        "corpus_sha256": manifest["corpus_sha256"],
        "database_sha256": database_hash,
        "schema_version": SCHEMA_VERSION,
        "counts": actual_counts,
        "storage": storage,
        "readonly": True,
    }
    return status, database_image


def verify_snapshot(snapshot: Path) -> dict[str, Any]:
    """Recompute the frozen snapshot's hashes, schema identity, counts, and integrity."""
    status, _database_image = _verify_snapshot(
        snapshot, require_directory_name=True
    )
    return status


def _open_verified_snapshot(
    snapshot: Path,
) -> tuple[dict[str, Any], sqlite3.Connection]:
    status, database_image = _verify_snapshot(
        snapshot, require_directory_name=True
    )
    return status, _open_serialized_readonly(database_image)
