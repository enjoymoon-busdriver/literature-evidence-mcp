from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from .errors import SearchInputError, SnapshotError
from .snapshot import _open_verified_snapshot


EMPTY_MESSAGE = "本次搜索未返回证据，不代表资料库中不存在。"
_ENGLISH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}

_DENIED_SQLITE_ACTIONS = {
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_SAVEPOINT,
    sqlite3.SQLITE_PRAGMA,
}


def _authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    _database: str | None,
    _source: str | None,
) -> int:
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "data_version" and arg2 is None:
        return sqlite3.SQLITE_OK
    if action in _DENIED_SQLITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _bounded_int(value: int, *, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise SearchInputError(f"{name} 必须是 1-{maximum} 的整数。")
    return value


def _validated_query(query: str) -> str:
    if not isinstance(query, str):
        raise SearchInputError("query 必须是文本。")
    normalized = re.sub(r"\s+", " ", query).strip()
    if not normalized or len(normalized) > 400:
        raise SearchInputError("query 必须是 1-400 个字符。")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise SearchInputError("query 不能包含控制字符。")
    return normalized


def _fts_expression(query: str) -> str | None:
    tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not tokens:
        return None
    content_tokens = [token for token in tokens if token.casefold() not in _ENGLISH_STOPWORDS]
    selected = content_tokens or tokens
    return " OR ".join('"' + token.replace('"', "") + '"' for token in selected)


def _empty(snapshot_id: str) -> dict[str, Any]:
    return {
        "found": False,
        "results": [],
        "snapshot_id": snapshot_id,
        "retrieval_mode": "bm25",
        "message": EMPTY_MESSAGE,
    }


def _parse_json_list(raw: str, *, label: str) -> list[str]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"SQLite 中的 {label} 不是有效 JSON。") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SnapshotError(f"SQLite 中的 {label} 不是字符串列表。")
    return value


def search_snapshot(
    snapshot: Path,
    query: str,
    *,
    top_k: int = 5,
    excerpt_chars: int = 1000,
) -> dict[str, Any]:
    """Verify a snapshot, then perform a bounded, local, read-only BM25 search."""
    query = _validated_query(query)
    top_k = _bounded_int(top_k, name="top_k", maximum=10)
    excerpt_chars = _bounded_int(
        excerpt_chars, name="excerpt_chars", maximum=1200
    )
    status, database = _open_verified_snapshot(snapshot)
    expression = _fts_expression(query)
    if expression is None:
        database.close()
        return _empty(status["snapshot_id"])

    database.set_authorizer(_authorizer)
    try:
        rows = database.execute(
            """SELECT
                c.chunk_id,c.document_id,c.asset_id,c.chunk_text,c.heading_path,
                c.pdf_page_start,c.pdf_page_end,c.source_line_start,c.source_line_end,
                c.anchor_label,c.fulltext_verified,c.formula_verified,
                d.title,d.identifiers,d.source_name,d.media_type,d.material_type,
                d.language,d.topics,d.evidence_role,bm25(chunk_fts) AS score
            FROM chunk_fts
            JOIN chunk c ON c.rowid=chunk_fts.rowid
            JOIN document d ON d.document_id=c.document_id
            WHERE chunk_fts MATCH ?
            ORDER BY score,c.chunk_id
            LIMIT ?""",
            (expression, top_k),
        ).fetchall()
    except sqlite3.Error as exc:
        raise SnapshotError("只读 BM25 查询失败。") from exc
    finally:
        database.close()

    if not rows:
        return _empty(status["snapshot_id"])
    results = []
    for row in rows:
        results.append(
            {
                "document_id": row["document_id"],
                "chunk_id": row["chunk_id"],
                "title": row["title"],
                "identifiers": json.loads(row["identifiers"]),
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
                "score": row["score"],
                "excerpt": row["chunk_text"][:excerpt_chars],
            }
        )
    return {
        "found": True,
        "results": results,
        "snapshot_id": status["snapshot_id"],
        "retrieval_mode": "bm25",
    }
