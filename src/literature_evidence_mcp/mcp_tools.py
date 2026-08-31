from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .errors import (
    EnhancedSearchError,
    LiteratureEvidenceError,
    SearchInputError,
    SnapshotError,
)
from .library import FixedLibrary
from .registry import LibraryRegistry
from .retrieval import (
    EMPTY_MESSAGE,
    _authorizer,
    _bounded_int,
    _fts_expression,
    _parse_json_list,
    _validated_query,
)
from .snapshot import _open_verified_snapshot


_DOCUMENT_ID = re.compile(r"\Adoc_[0-9a-f]{24}\Z")
_CHUNK_ID = re.compile(r"\Achunk_[0-9a-f]{24}\Z")
_SECTION_ID = re.compile(r"\Asec_[0-9a-f]{24}\Z")
_BATCH_CHUNK_LIMIT = 5

_CHUNK_SOURCE_SQL = """SELECT
    c.chunk_id,c.document_id,c.asset_id,c.ordinal,c.chunk_text,c.heading_path,
    c.pdf_page_start,c.pdf_page_end,c.source_line_start,c.source_line_end,
    c.anchor_label,c.fulltext_verified,c.formula_verified,
    d.title,d.identifiers,d.source_name,d.media_type,d.material_type,
    d.language,d.topics,d.evidence_role
FROM chunk c
JOIN document d ON d.document_id=c.document_id"""


@dataclass(frozen=True)
class _Section:
    section_id: str
    document_id: str
    asset_id: str
    title: str
    source_name: str
    section_kind: str
    heading_path: tuple[str, ...]
    rows: tuple[sqlite3.Row, ...]


def _validated_identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SearchInputError(f"{label} 格式无效。")
    return value


def _validated_document_id(value: str) -> str:
    return _validated_identifier(value, _DOCUMENT_ID, "document_id")


def _validated_chunk_id(value: str) -> str:
    return _validated_identifier(value, _CHUNK_ID, "chunk_id")


def _validated_section_id(value: str) -> str:
    return _validated_identifier(value, _SECTION_ID, "section_id")


def _validated_tool_query(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 400:
        raise SearchInputError("query 必须是 1-400 个字符。")
    return _validated_query(value)


def _validated_chunk_ids(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= _BATCH_CHUNK_LIMIT:
        raise SearchInputError(
            f"chunk_ids 必须包含 1-{_BATCH_CHUNK_LIMIT} 个 chunk_id。"
        )
    validated = [_validated_chunk_id(value) for value in values]
    if len(validated) != len(set(validated)):
        raise SearchInputError("chunk_ids 不能重复。")
    return validated


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
        raise SnapshotError(f"SQLite 中的 {label} 必须是字符串到字符串的对象。")
    return value


def _empty(snapshot_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "found": False,
        "results": [],
        "snapshot_id": snapshot_id,
        "message": EMPTY_MESSAGE,
        **extra,
    }


def _chunk_source(
    row: sqlite3.Row,
    excerpt: str,
    *,
    score: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "document_id": row["document_id"],
        "asset_id": row["asset_id"],
        "chunk_id": row["chunk_id"],
        "title": row["title"],
        "identifiers": _json_object_of_strings(
            row["identifiers"], label="identifiers"
        ),
        "source_name": row["source_name"],
        "media_type": row["media_type"],
        "material_type": row["material_type"],
        "language": row["language"],
        "topics": _parse_json_list(row["topics"], label="topics"),
        "evidence_role": row["evidence_role"],
        "heading_path": _parse_json_list(
            row["heading_path"], label="heading_path"
        ),
        "pdf_page_start": row["pdf_page_start"],
        "pdf_page_end": row["pdf_page_end"],
        "source_line_start": row["source_line_start"],
        "source_line_end": row["source_line_end"],
        "anchor_label": row["anchor_label"],
        "fulltext_verified": bool(row["fulltext_verified"]),
        "formula_verified": bool(row["formula_verified"]),
        "excerpt": excerpt,
    }
    if score is not None:
        result["score"] = score
    return result


def _make_section(
    library_id: str, snapshot_id: str, rows: list[sqlite3.Row]
) -> _Section:
    first = rows[0]
    heading_path = tuple(
        _parse_json_list(first["heading_path"], label="heading_path")
    )
    section_kind = "pdf_page" if first["material_type"] == "pdf" else "heading"
    identity = json.dumps(
        [
            library_id,
            snapshot_id,
            first["document_id"],
            first["asset_id"],
            section_kind,
            list(heading_path),
            first["pdf_page_start"],
            first["chunk_id"],
            rows[-1]["chunk_id"],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _Section(
        section_id="sec_" + hashlib.sha256(identity).hexdigest()[:24],
        document_id=first["document_id"],
        asset_id=first["asset_id"],
        title=first["title"],
        source_name=first["source_name"],
        section_kind=section_kind,
        heading_path=heading_path,
        rows=tuple(rows),
    )


def _sections(
    library_id: str, snapshot_id: str, rows: list[sqlite3.Row]
) -> list[_Section]:
    sections: list[_Section] = []
    pending: list[sqlite3.Row] = []
    pending_key: tuple[object, ...] | None = None
    for row in rows:
        heading_path = tuple(
            _parse_json_list(row["heading_path"], label="heading_path")
        )
        if row["material_type"] == "pdf":
            key: tuple[object, ...] = (
                row["asset_id"],
                "pdf_page",
                row["pdf_page_start"],
            )
        else:
            key = (row["asset_id"], "heading", heading_path)
        if pending and key != pending_key:
            sections.append(_make_section(library_id, snapshot_id, pending))
            pending = []
        pending_key = key
        pending.append(row)
    if pending:
        sections.append(_make_section(library_id, snapshot_id, pending))
    return sections


def _section_item(section: _Section) -> dict[str, Any]:
    page_starts = [
        row["pdf_page_start"]
        for row in section.rows
        if row["pdf_page_start"] is not None
    ]
    page_ends = [
        row["pdf_page_end"]
        for row in section.rows
        if row["pdf_page_end"] is not None
    ]
    line_starts = [
        row["source_line_start"]
        for row in section.rows
        if row["source_line_start"] is not None
    ]
    line_ends = [
        row["source_line_end"]
        for row in section.rows
        if row["source_line_end"] is not None
    ]
    return {
        "section_id": section.section_id,
        "document_id": section.document_id,
        "asset_id": section.asset_id,
        "section_kind": section.section_kind,
        "heading_path": list(section.heading_path),
        "pdf_page_start": min(page_starts) if page_starts else None,
        "pdf_page_end": max(page_ends) if page_ends else None,
        "source_line_start": min(line_starts) if line_starts else None,
        "source_line_end": max(line_ends) if line_ends else None,
        "anchor_label": section.rows[0]["anchor_label"],
        "first_chunk_id": section.rows[0]["chunk_id"],
        "last_chunk_id": section.rows[-1]["chunk_id"],
        "chunk_count": len(section.rows),
    }


class ReadOnlyEvidenceTools:
    """Eight path-free operations over the fixed local library registry."""

    def __init__(
        self,
        application_root: Path | None = None,
        *,
        enhanced_search: Any | None = None,
    ) -> None:
        self._registry = LibraryRegistry(application_root)
        self._enhanced_search = enhanced_search

    def _library(self, library_id: str) -> FixedLibrary:
        return FixedLibrary(
            self._registry.library_path(library_id),
            library_id=library_id,
            enhanced_search=self._enhanced_search,
        )

    @contextlib.contextmanager
    def _database(
        self, library_id: str, snapshot_id: str
    ) -> Iterator[tuple[dict[str, Any], sqlite3.Connection]]:
        snapshot, record, _catalog = self._library(library_id)._published_snapshot(
            snapshot_id
        )
        status, database = _open_verified_snapshot(snapshot)
        if status["manifest_sha256"] != record["manifest_sha256"]:
            database.close()
            raise SnapshotError("快照内容与目录册绑定不一致。")
        try:
            database.set_authorizer(_authorizer)
            yield status, database
        finally:
            database.close()

    @staticmethod
    def _document_rows(
        database: sqlite3.Connection, document_id: str
    ) -> list[sqlite3.Row]:
        return database.execute(
            _CHUNK_SOURCE_SQL
            + " WHERE c.document_id=? ORDER BY c.asset_id,c.ordinal,c.chunk_id",
            (document_id,),
        ).fetchall()

    def search_documents(
        self,
        library_id: str,
        snapshot_id: str,
        query: str,
        *,
        top_k: int = 5,
        excerpt_chars: int = 1000,
        mode: str = "bm25",
    ) -> dict[str, Any]:
        if mode == "bm25":
            query = _validated_tool_query(query)
            result = self._library(library_id).search(
                snapshot_id,
                query,
                top_k=top_k,
                excerpt_chars=excerpt_chars,
            )
        elif mode == "enhanced":
            try:
                result = self._library(library_id).search(
                    snapshot_id,
                    query,
                    top_k=top_k,
                    excerpt_chars=excerpt_chars,
                    mode=mode,
                )
            except EnhancedSearchError:
                raise
            except LiteratureEvidenceError as exc:
                raise EnhancedSearchError(
                    "增强搜索本地核验失败：资料库或快照不可用。"
                ) from exc
        else:
            raise SearchInputError("mode 必须是 bm25 或 enhanced。")
        result["library_id"] = library_id
        return result

    def get_excerpt(
        self,
        library_id: str,
        snapshot_id: str,
        document_id: str,
        chunk_id: str,
        *,
        max_chars: int = 600,
    ) -> dict[str, Any]:
        document_id = _validated_document_id(document_id)
        chunk_id = _validated_chunk_id(chunk_id)
        max_chars = _bounded_int(max_chars, name="max_chars", maximum=1200)
        with self._database(library_id, snapshot_id) as (status, database):
            row = database.execute(
                _CHUNK_SOURCE_SQL + " WHERE c.chunk_id=?",
                (chunk_id,),
            ).fetchone()
        if row is None:
            raise SearchInputError("所选快照中不存在该 chunk_id。")
        if row["document_id"] != document_id:
            raise SearchInputError("chunk_id 不属于指定的 document_id。")
        return {
            "found": True,
            "library_id": library_id,
            "snapshot_id": status["snapshot_id"],
            "result": _chunk_source(row, row["chunk_text"][:max_chars]),
            "truncated": len(row["chunk_text"]) > max_chars,
        }

    def get_multiple_excerpts(
        self,
        library_id: str,
        snapshot_id: str,
        document_id: str,
        chunk_ids: list[str],
        *,
        per_item_chars: int = 600,
    ) -> dict[str, Any]:
        document_id = _validated_document_id(document_id)
        chunk_ids = _validated_chunk_ids(chunk_ids)
        per_item_chars = _bounded_int(
            per_item_chars, name="per_item_chars", maximum=1200
        )
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._database(library_id, snapshot_id) as (status, database):
            rows = database.execute(
                _CHUNK_SOURCE_SQL + f" WHERE c.chunk_id IN ({placeholders})",
                tuple(chunk_ids),
            ).fetchall()
        by_id = {row["chunk_id"]: row for row in rows}
        for row in rows:
            if row["document_id"] != document_id:
                raise SearchInputError(
                    "至少一个 chunk_id 不属于指定的 document_id。"
                )
        if any(chunk_id not in by_id for chunk_id in chunk_ids):
            raise SearchInputError("所选快照中至少缺少一个 chunk_id。")
        results = []
        for row in (by_id[chunk_id] for chunk_id in chunk_ids):
            result = _chunk_source(row, row["chunk_text"][:per_item_chars])
            result["truncated"] = len(row["chunk_text"]) > per_item_chars
            results.append(result)
        return {
            "found": True,
            "library_id": library_id,
            "snapshot_id": status["snapshot_id"],
            "document_id": document_id,
            "results": results,
        }

    def get_document_metadata(
        self, library_id: str, snapshot_id: str, document_id: str
    ) -> dict[str, Any]:
        document_id = _validated_document_id(document_id)
        with self._database(library_id, snapshot_id) as (status, database):
            document = database.execute(
                """SELECT document_id,title,identifiers,source_name,source_type,
                media_type,material_type,language,topics,evidence_role,
                fulltext_verified,formula_verified
                FROM document WHERE document_id=?""",
                (document_id,),
            ).fetchone()
            if document is None:
                raise SearchInputError("所选快照中不存在该 document_id。")
            assets = database.execute(
                """SELECT a.asset_id,a.extraction_method,a.extraction_status,
                a.page_count,a.extracted_char_count,count(c.chunk_id) AS chunk_count
                FROM asset a LEFT JOIN chunk c ON c.asset_id=a.asset_id
                WHERE a.document_id=?
                GROUP BY a.asset_id,a.extraction_method,a.extraction_status,
                a.page_count,a.extracted_char_count ORDER BY a.asset_id""",
                (document_id,),
            ).fetchall()
        return {
            "found": True,
            "library_id": library_id,
            "snapshot_id": status["snapshot_id"],
            "document": {
                "document_id": document["document_id"],
                "title": document["title"],
                "identifiers": _json_object_of_strings(
                    document["identifiers"], label="identifiers"
                ),
                "source_name": document["source_name"],
                "source_type": document["source_type"],
                "media_type": document["media_type"],
                "material_type": document["material_type"],
                "language": document["language"],
                "topics": _parse_json_list(document["topics"], label="topics"),
                "evidence_role": document["evidence_role"],
                "fulltext_verified": bool(document["fulltext_verified"]),
                "formula_verified": bool(document["formula_verified"]),
                "assets": [
                    {
                        "asset_id": asset["asset_id"],
                        "extraction_method": asset["extraction_method"],
                        "extraction_status": asset["extraction_status"],
                        "page_count": asset["page_count"],
                        "extracted_char_count": asset["extracted_char_count"],
                        "chunk_count": asset["chunk_count"],
                    }
                    for asset in assets
                ],
            },
        }

    def get_document_toc(
        self,
        library_id: str,
        snapshot_id: str,
        document_id: str,
        *,
        max_items: int = 100,
    ) -> dict[str, Any]:
        document_id = _validated_document_id(document_id)
        max_items = _bounded_int(max_items, name="max_items", maximum=100)
        with self._database(library_id, snapshot_id) as (status, database):
            rows = self._document_rows(database, document_id)
        if not rows:
            raise SearchInputError("所选快照中不存在该 document_id。")
        sections = _sections(library_id, status["snapshot_id"], rows)
        return {
            "found": True,
            "library_id": library_id,
            "snapshot_id": status["snapshot_id"],
            "document_id": document_id,
            "title": rows[0]["title"],
            "source_name": rows[0]["source_name"],
            "items": [_section_item(section) for section in sections[:max_items]],
            "truncated": len(sections) > max_items,
            "navigation_semantics": (
                "Markdown 标题路径或 PDF 页码派生导航；不宣称是原文目录。"
            ),
        }

    def read_document_section(
        self,
        library_id: str,
        snapshot_id: str,
        document_id: str,
        section_id: str,
        *,
        max_chars: int = 1200,
    ) -> dict[str, Any]:
        document_id = _validated_document_id(document_id)
        section_id = _validated_section_id(section_id)
        max_chars = _bounded_int(max_chars, name="max_chars", maximum=1200)
        with self._database(library_id, snapshot_id) as (status, database):
            rows = self._document_rows(database, document_id)
        if not rows:
            raise SearchInputError("所选快照中不存在该 document_id。")
        selected = next(
            (
                section
                for section in _sections(library_id, status["snapshot_id"], rows)
                if section.section_id == section_id
            ),
            None,
        )
        if selected is None:
            raise SearchInputError(
                "section_id 不属于指定的 snapshot_id 与 document_id。"
            )

        remaining = max_chars
        results: list[dict[str, Any]] = []
        for row in selected.rows:
            if remaining <= 0:
                break
            excerpt = row["chunk_text"][:remaining]
            results.append(_chunk_source(row, excerpt))
            remaining -= len(excerpt)
        source_chars = sum(len(row["chunk_text"]) for row in selected.rows)
        returned_chars = max_chars - remaining
        return {
            "found": True,
            "library_id": library_id,
            "snapshot_id": status["snapshot_id"],
            "document_id": document_id,
            "section": _section_item(selected),
            "results": results,
            "returned_chars": returned_chars,
            "truncated": returned_chars < source_chars,
        }

    def find_in_document(
        self,
        library_id: str,
        snapshot_id: str,
        document_id: str,
        query: str,
        *,
        top_k: int = 5,
        excerpt_chars: int = 600,
    ) -> dict[str, Any]:
        document_id = _validated_document_id(document_id)
        query = _validated_tool_query(query)
        top_k = _bounded_int(top_k, name="top_k", maximum=10)
        excerpt_chars = _bounded_int(
            excerpt_chars, name="excerpt_chars", maximum=1200
        )
        expression = _fts_expression(query)
        with self._database(library_id, snapshot_id) as (status, database):
            document = database.execute(
                "SELECT 1 FROM document WHERE document_id=?", (document_id,)
            ).fetchone()
            if document is None:
                raise SearchInputError("所选快照中不存在该 document_id。")
            if expression is None:
                return _empty(
                    status["snapshot_id"],
                    library_id=library_id,
                    document_id=document_id,
                    retrieval_mode="bm25",
                )
            rows = database.execute(
                _CHUNK_SOURCE_SQL.replace(
                    "FROM chunk c",
                    ",bm25(chunk_fts) AS score FROM chunk_fts "
                    "JOIN chunk c ON c.rowid=chunk_fts.rowid",
                )
                + " WHERE chunk_fts MATCH ? AND c.document_id=? "
                "ORDER BY score,c.chunk_id LIMIT ?",
                (expression, document_id, top_k),
            ).fetchall()
        if not rows:
            return _empty(
                status["snapshot_id"],
                library_id=library_id,
                document_id=document_id,
                retrieval_mode="bm25",
            )
        return {
            "found": True,
            "library_id": library_id,
            "snapshot_id": status["snapshot_id"],
            "document_id": document_id,
            "retrieval_mode": "bm25",
            "results": [
                _chunk_source(
                    row,
                    row["chunk_text"][:excerpt_chars],
                    score=row["score"],
                )
                for row in rows
            ],
        }

    def retrieval_status(
        self,
        library_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        common = {
            "readonly": True,
            "closed_world": True,
            "transport": "stdio",
            "service_version": __version__,
            "mcp_sdk_version": importlib.metadata.version("mcp"),
        }
        if library_id is None and snapshot_id is not None:
            raise SearchInputError("只提供 snapshot_id 无法确定所属资料库。")
        if library_id is None:
            libraries = []
            for item in self._registry.list_libraries():
                status = FixedLibrary(Path(item["library_root"])).catalog_status()
                libraries.append(
                    {
                        "library_id": item["library_id"],
                        "name": item["name"],
                        "description": item["description"],
                        "current_snapshot_id": status["current_snapshot_id"],
                        "last_successful_snapshot_id": status[
                            "last_successful_snapshot_id"
                        ],
                    }
                )
            return {"scope": "libraries", "libraries": libraries, **common}

        library = self._library(library_id)
        if snapshot_id is None:
            status = library.catalog_status()
            return {
                "scope": "snapshots",
                "library_id": library_id,
                "current_snapshot_id": status["current_snapshot_id"],
                "last_successful_snapshot_id": status[
                    "last_successful_snapshot_id"
                ],
                "snapshots": library.list_snapshots(),
                **common,
            }

        status = library.verify(snapshot_id)
        return {
            "scope": "snapshot",
            "found": True,
            "ready": True,
            "library_id": library_id,
            "snapshot_id": status["snapshot_id"],
            "verified": status["verified"],
            "current": status["current"],
            "last_successful": status["last_successful"],
            "base_snapshot_id": status["base_snapshot_id"],
            "retrieval_mode": "bm25",
            "schema_version": status["schema_version"],
            "counts": status["counts"],
            "manifest_sha256": status["manifest_sha256"],
            "corpus_sha256": status["corpus_sha256"],
            "database_sha256": status["database_sha256"],
            "known_limitations": [
                "SQLite FTS5 unicode61 没有专门中文分词。",
                "空结果只表示本次检索未返回证据。",
                "PDF 导航按已索引页码派生，不是作者提供的目录。",
                "本服务不会提升全文或公式核验状态。",
            ],
            **common,
        }


__all__ = ["ReadOnlyEvidenceTools"]
