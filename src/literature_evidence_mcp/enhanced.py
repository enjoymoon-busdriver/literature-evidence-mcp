from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import (
    EnhancedSearchError,
    LiteratureEvidenceError,
    SnapshotError,
    VectorError,
)
from .library import FixedLibrary
from .mcp_tools import _CHUNK_SOURCE_SQL, _chunk_source
from .retrieval import EMPTY_MESSAGE, _authorizer, _bounded_int, _validated_query
from .snapshot import _open_verified_snapshot
from .vectors import load_verified_vectors, profile_id, validate_profile


QUERY_REWRITE = "query_rewrite"
VECTOR_RECALL = "vector_recall"
CANDIDATE_RERANK = "candidate_rerank"
ROLE_ORDER = (QUERY_REWRITE, VECTOR_RECALL, CANDIDATE_RERANK)

MAX_REWRITE_CHARS = 400
MAX_RERANK_CANDIDATES = 10
MAX_CANDIDATE_TEXT_CHARS = 600
MAX_RERANK_TOTAL_CHARS = 4000

_LIBRARY_ID = re.compile(r"\Alib_[0-9a-f]{32}\Z")


def _identity_text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} 必须是 1-200 个可见字符。")
    return value


@dataclass(frozen=True)
class EnhancedSearchConfig:
    provider: str
    query_rewrite_model_id: str
    vector_recall_model_id: str
    candidate_rerank_model_id: str
    vector_profile: Mapping[str, Any]
    max_rewrite_chars: int = MAX_REWRITE_CHARS
    max_rerank_candidates: int = MAX_RERANK_CANDIDATES
    max_candidate_text_chars: int = MAX_CANDIDATE_TEXT_CHARS
    max_rerank_total_chars: int = MAX_RERANK_TOTAL_CHARS

    def __post_init__(self) -> None:
        provider = _identity_text(self.provider, "provider")
        model_ids = {
            QUERY_REWRITE: _identity_text(
                self.query_rewrite_model_id, "query_rewrite_model_id"
            ),
            VECTOR_RECALL: _identity_text(
                self.vector_recall_model_id, "vector_recall_model_id"
            ),
            CANDIDATE_RERANK: _identity_text(
                self.candidate_rerank_model_id, "candidate_rerank_model_id"
            ),
        }
        profile = validate_profile(self.vector_profile)
        if (
            profile["provider"] != provider
            or profile["model_id"] != model_ids[VECTOR_RECALL]
        ):
            raise ValueError(
                "vector_recall provider/model_id 必须与精确 vector profile 匹配。"
            )
        for label, value, maximum in (
            ("max_rewrite_chars", self.max_rewrite_chars, MAX_REWRITE_CHARS),
            (
                "max_rerank_candidates",
                self.max_rerank_candidates,
                MAX_RERANK_CANDIDATES,
            ),
            (
                "max_candidate_text_chars",
                self.max_candidate_text_chars,
                MAX_CANDIDATE_TEXT_CHARS,
            ),
            (
                "max_rerank_total_chars",
                self.max_rerank_total_chars,
                MAX_RERANK_TOTAL_CHARS,
            ),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{label} 必须是 1-{maximum} 的整数。")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self, "query_rewrite_model_id", model_ids[QUERY_REWRITE]
        )
        object.__setattr__(self, "vector_recall_model_id", model_ids[VECTOR_RECALL])
        object.__setattr__(
            self, "candidate_rerank_model_id", model_ids[CANDIDATE_RERANK]
        )
        object.__setattr__(self, "vector_profile", profile)

    @property
    def vector_profile_id(self) -> str:
        return profile_id(self.vector_profile)

    def model_id(self, role: str) -> str:
        return getattr(self, f"{role}_model_id")


class EnhancedTransport(Protocol):
    simulated: bool

    def invoke(
        self,
        *,
        role: str,
        provider: str,
        model_id: str,
        payload: Mapping[str, Any],
    ) -> object: ...


def _audit(
    calls: Sequence[Mapping[str, Any]], simulated: bool
) -> dict[str, Any]:
    return {
        "simulated": simulated,
        "call_count": len(calls),
        "calls": [dict(call) for call in calls],
    }


def _validated_library_id(value: object) -> str:
    if not isinstance(value, str) or _LIBRARY_ID.fullmatch(value) is None:
        raise EnhancedSearchError("增强搜索的 library_id 格式无效。")
    return value


def _validated_vector(value: object, dimensions: int) -> tuple[float, ...]:
    if (
        isinstance(value, (str, bytes, bytearray))
        or not isinstance(value, Sequence)
        or len(value) != dimensions
    ):
        raise ValueError("query vector shape")
    result: list[float] = []
    for item in value:
        if type(item) not in {int, float}:
            raise ValueError("query vector type")
        converted = float(item)
        if not math.isfinite(converted):
            raise ValueError("query vector finite")
        result.append(converted)
    return tuple(result)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_scale = max(abs(value) for value in left)
    if left_scale == 0.0:
        raise ValueError("zero query vector")
    right_scale = max(abs(value) for value in right)
    if right_scale == 0.0:
        return 0.0
    scaled_left = [value / left_scale for value in left]
    scaled_right = [value / right_scale for value in right]
    left_norm = math.sqrt(math.fsum(value * value for value in scaled_left))
    right_norm = math.sqrt(math.fsum(value * value for value in scaled_right))
    return math.fsum(
        (a / left_norm) * (b / right_norm)
        for a, b in zip(scaled_left, scaled_right, strict=True)
    )


def _validated_rerank(
    value: object, candidate_ids: set[str]
) -> list[tuple[str, float]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("rerank shape")
    if len(value) > len(candidate_ids):
        raise ValueError("rerank count")
    results: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"chunk_id", "score"}:
            raise ValueError("rerank item")
        chunk_id = item["chunk_id"]
        score = item["score"]
        if not isinstance(chunk_id, str) or chunk_id not in candidate_ids:
            raise ValueError("rerank chunk")
        if chunk_id in seen:
            raise ValueError("rerank duplicate")
        if type(score) not in {int, float}:
            raise ValueError("rerank score type")
        converted = float(score)
        if not math.isfinite(converted):
            raise ValueError("rerank score finite")
        seen.add(chunk_id)
        results.append((chunk_id, converted))
    return sorted(results, key=lambda item: (-item[1], item[0]))


class EnhancedSearchService:
    """One fail-closed rewrite, local vector recall, and rerank chain."""

    def __init__(
        self, config: EnhancedSearchConfig, transport: EnhancedTransport
    ) -> None:
        if not isinstance(config, EnhancedSearchConfig):
            raise ValueError("config 必须是 EnhancedSearchConfig。")
        if not callable(getattr(transport, "invoke", None)):
            raise ValueError("transport 必须实现 invoke。")
        simulated = getattr(transport, "simulated", None)
        if type(simulated) is not bool:
            raise ValueError("transport.simulated 必须是明确的 bool。")
        self._config = config
        self._transport = transport
        self._simulated = simulated

    def public_summary(self) -> dict[str, Any]:
        return {
            "vector_profile_id": self._config.vector_profile_id,
            "simulated": self._simulated,
            "roles": [
                {
                    "role": role,
                    "provider": self._config.provider,
                    "model_id": self._config.model_id(role),
                }
                for role in ROLE_ORDER
            ],
        }

    def _invoke(
        self,
        role: str,
        payload: Mapping[str, Any],
        audit_item: dict[str, Any],
        calls: list[dict[str, Any]],
    ) -> object:
        calls.append(
            {
                "role": role,
                "provider": self._config.provider,
                "model_id": self._config.model_id(role),
                **audit_item,
            }
        )
        try:
            return self._transport.invoke(
                role=role,
                provider=self._config.provider,
                model_id=self._config.model_id(role),
                payload=payload,
            )
        except Exception as exc:
            raise EnhancedSearchError(
                f"增强搜索的 {role} 调用失败；未重试。",
                _audit(calls, self._simulated),
            ) from exc

    def _preflight(
        self, library: FixedLibrary, snapshot_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, tuple[float, ...]]]:
        loaded = load_verified_vectors(
            library._root, snapshot_id, self._config.vector_profile_id
        )
        profile = loaded["profile"]
        if profile != self._config.vector_profile:
            raise VectorError("查询向量模型与固定 artifact profile 不匹配。")

        vectors = {
            item["chunk_id"]: tuple(item["vector"]) for item in loaded["vectors"]
        }
        if len(vectors) != len(loaded["vectors"]):
            raise VectorError("固定向量 artifact 含重复 chunk_id。")

        snapshot, record, _catalog = library._published_snapshot(snapshot_id)
        status, database = _open_verified_snapshot(snapshot)
        if status["manifest_sha256"] != record["manifest_sha256"]:
            database.close()
            raise SnapshotError("快照内容与目录册绑定不一致。")
        database.set_authorizer(_authorizer)
        try:
            rows = database.execute(
                _CHUNK_SOURCE_SQL + " ORDER BY c.chunk_id"
            ).fetchall()
        finally:
            database.close()
        by_id = {row["chunk_id"]: row for row in rows}
        if len(by_id) != len(rows) or set(by_id) != set(vectors):
            raise VectorError("固定向量映射与所选快照的完整 chunk 集合不一致。")
        return loaded, by_id, vectors

    def search(
        self,
        library: FixedLibrary,
        library_id: str,
        snapshot_id: str,
        query: str,
        *,
        top_k: int = 5,
        excerpt_chars: int = 1000,
    ) -> dict[str, Any]:
        calls: list[dict[str, Any]] = []
        library_id = _validated_library_id(library_id)
        try:
            if (
                library._library_id != library_id
                or library._root.name != library_id
            ):
                raise ValueError("library identity mismatch")
            query = _validated_query(query)
            top_k = _bounded_int(top_k, name="top_k", maximum=10)
            excerpt_chars = _bounded_int(
                excerpt_chars, name="excerpt_chars", maximum=1200
            )
            loaded, rows, vectors = self._preflight(library, snapshot_id)
        except EnhancedSearchError:
            raise
        except Exception as exc:
            raise EnhancedSearchError(
                "增强搜索本地核验失败：向量 artifact 缺失、不匹配或不可用。",
                _audit(calls, self._simulated),
            ) from exc

        rewrite = self._invoke(
            QUERY_REWRITE,
            {"query": query},
            {"sent": "original_question", "query_chars": len(query)},
            calls,
        )
        try:
            rewritten = _validated_query(rewrite)
            if len(rewritten) > self._config.max_rewrite_chars:
                raise ValueError("rewrite length")
        except (LiteratureEvidenceError, TypeError, ValueError) as exc:
            raise EnhancedSearchError(
                "增强搜索的 query_rewrite 输出无效；未重试。",
                _audit(calls, self._simulated),
            ) from exc

        raw_query_vector = self._invoke(
            VECTOR_RECALL,
            {"query": rewritten},
            {"sent": "rewritten_question", "query_chars": len(rewritten)},
            calls,
        )
        try:
            query_vector = _validated_vector(
                raw_query_vector, loaded["profile"]["dimensions"]
            )
            ranked = sorted(
                (
                    (_cosine(query_vector, vector), chunk_id)
                    for chunk_id, vector in vectors.items()
                ),
                key=lambda item: (-item[0], item[1]),
            )[: self._config.max_rerank_candidates]
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise EnhancedSearchError(
                "增强搜索的 vector_recall 输出无效；未重试。",
                _audit(calls, self._simulated),
            ) from exc

        candidates: list[dict[str, str]] = []
        vector_scores: dict[str, float] = {}
        remaining = self._config.max_rerank_total_chars
        for score, chunk_id in ranked:
            text = rows[chunk_id]["chunk_text"][: min(
                self._config.max_candidate_text_chars, remaining
            )]
            if not text:
                continue
            candidates.append({"chunk_id": chunk_id, "text": text})
            vector_scores[chunk_id] = score
            remaining -= len(text)
            if remaining == 0:
                break
        candidate_ids = [item["chunk_id"] for item in candidates]
        total_chars = sum(len(item["text"]) for item in candidates)
        raw_rerank = self._invoke(
            CANDIDATE_RERANK,
            {"query": rewritten, "candidates": candidates},
            {
                "sent": "candidate_evidence",
                "query_chars": len(rewritten),
                "candidate_count": len(candidates),
                "candidate_ids": candidate_ids,
                "candidate_chars": total_chars,
            },
            calls,
        )
        try:
            reranked = _validated_rerank(raw_rerank, set(candidate_ids))[:top_k]
        except (TypeError, ValueError) as exc:
            raise EnhancedSearchError(
                "增强搜索的 candidate_rerank 输出无效；未重试。",
                _audit(calls, self._simulated),
            ) from exc

        audit = _audit(calls, self._simulated)
        if not reranked:
            return {
                "found": False,
                "results": [],
                "library_id": library_id,
                "snapshot_id": loaded["snapshot_id"],
                "retrieval_mode": "enhanced",
                "message": EMPTY_MESSAGE,
                "audit": audit,
            }
        try:
            results = []
            for chunk_id, score in reranked:
                result = _chunk_source(
                    rows[chunk_id],
                    rows[chunk_id]["chunk_text"][:excerpt_chars],
                    score=score,
                )
                result["vector_score"] = vector_scores[chunk_id]
                results.append(result)
        except Exception as exc:
            raise EnhancedSearchError(
                "增强搜索无法投影已核验候选证据。", audit
            ) from exc
        return {
            "found": True,
            "results": results,
            "library_id": library_id,
            "snapshot_id": loaded["snapshot_id"],
            "retrieval_mode": "enhanced",
            "audit": audit,
        }


__all__ = [
    "CANDIDATE_RERANK",
    "EnhancedSearchConfig",
    "EnhancedSearchService",
    "EnhancedTransport",
    "MAX_CANDIDATE_TEXT_CHARS",
    "MAX_RERANK_CANDIDATES",
    "MAX_RERANK_TOTAL_CHARS",
    "QUERY_REWRITE",
    "ROLE_ORDER",
    "VECTOR_RECALL",
]
