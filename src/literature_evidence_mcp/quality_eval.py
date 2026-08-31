from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .enhanced import (
    CANDIDATE_RERANK,
    QUERY_REWRITE,
    ROLE_ORDER,
    VECTOR_RECALL,
    EnhancedSearchConfig,
    EnhancedSearchService,
)
from .errors import EnhancedSearchError, LiteratureEvidenceError
from .library import FixedLibrary
from .registry import LibraryRegistry
from .snapshot import _open_verified_snapshot
from .vectors import (
    OfflineDeterministicFakeEmbedder,
    build_vectors,
    load_verified_vectors,
    offline_fake_profile,
)


REPORT_FORMAT = "literature-evidence-offline-quality-evaluation"
REPORT_VERSION = 1
TOP_K = 5
DISCLAIMER = "合成 fake 管线验收，不能推出真实模型质量提升。"


@dataclass(frozen=True)
class _SnapshotData:
    library_id: str
    library_root: Path
    snapshot_id: str
    documents: Mapping[str, str]
    chunks: Mapping[str, Mapping[str, Any]]
    vectors: Mapping[str, tuple[float, ...]]


@dataclass(frozen=True)
class _Question:
    case_id: str
    q0: str
    language: str
    route: str
    selection: str
    expected_source: str | None
    target_marker: str | None
    rewritten_query: str
    known_bm25_limitation: bool = False
    traceability: str | None = None
    forbidden_selection: str | None = None
    forbidden_source: str | None = None


def _pdf_bytes(pages: Sequence[str]) -> bytes:
    """Create a tiny shareable text-layer PDF with only the standard library."""
    objects: dict[int, bytes] = {}
    page_numbers = [4 + index * 2 for index in range(len(pages))]
    content_numbers = [number + 1 for number in page_numbers]
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for page_object, content_object, text in zip(
        page_numbers, content_numbers, pages, strict=True
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
    for number in range(1, max(objects) + 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(objects[number])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _load_snapshot(
    library_id: str,
    library_root: Path,
    snapshot_id: str,
    vector_profile_id: str,
) -> _SnapshotData:
    library = FixedLibrary(library_root)
    documents = {
        member["source_name"]: member["document_id"]
        for member in library.snapshot_members(snapshot_id)
    }
    status, database = _open_verified_snapshot(
        library_root / "snapshots" / snapshot_id
    )
    try:
        rows = database.execute(
            """SELECT
                chunk_id,document_id,chunk_text,anchor_label,
                pdf_page_start,pdf_page_end,source_line_start,source_line_end
            FROM chunk ORDER BY chunk_id"""
        ).fetchall()
    finally:
        database.close()
    vectors = load_verified_vectors(library_root, snapshot_id, vector_profile_id)
    return _SnapshotData(
        library_id=library_id,
        library_root=library_root,
        snapshot_id=status["snapshot_id"],
        documents=documents,
        chunks={
            row["chunk_id"]: {
                "document_id": row["document_id"],
                "chunk_text": row["chunk_text"],
                "anchor_label": row["anchor_label"],
                "pdf_page_start": row["pdf_page_start"],
                "pdf_page_end": row["pdf_page_end"],
                "source_line_start": row["source_line_start"],
                "source_line_end": row["source_line_end"],
            }
            for row in rows
        },
        vectors={
            item["chunk_id"]: tuple(item["vector"])
            for item in vectors["vectors"]
        },
    )


def _build_fixture(root: Path) -> tuple[
    dict[str, _SnapshotData], dict[str, Any], dict[str, set[str]]
]:
    application_root = root / "application"
    registry = LibraryRegistry(application_root)
    library_a_record = registry.create("合成库 A")
    library_b_record = registry.create("合成库 B")
    library_a_id = library_a_record["library_id"]
    library_b_id = library_b_record["library_id"]
    library_a_root = Path(library_a_record["library_root"])
    library_b_root = Path(library_b_record["library_root"])

    source_a = root / "sources" / "library-a"
    source_b = root / "sources" / "library-b"
    shared_a = _write_text(
        source_a / "shared.md",
        """# Shared Calibration

alpha_library_marker shared calibration thermodynamic evidence.

## English Local Evidence

english_local_marker entropy balance turbine record.

## Markdown Provenance

markdown_anchor_marker provenance chain line evidence.
""",
    )
    chinese = _write_text(
        source_a / "chinese.md",
        """# 中文本地证据

中文 本地 标记 温度梯度 驱动 导热 过程。
""",
    )
    pdf = source_a / "pages.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(
        _pdf_bytes(
            [
                "synthetic page one introduction",
                "pdf_page_marker traceable page evidence",
            ]
        )
    )
    new_source = _write_text(
        source_a / "new.md",
        "# New Snapshot\n\nsnapshot_new_marker appears only in snapshot S2.\n",
    )
    shared_b = _write_text(
        source_b / "shared.md",
        "# Shared Calibration\n\nbeta_library_marker shared calibration independent evidence.\n",
    )

    library_a = FixedLibrary(library_a_root)
    a_s1 = library_a.build([shared_a, chinese, pdf])
    a_s2 = library_a.build([new_source], base_snapshot_id=a_s1["snapshot_id"])
    b_s1 = FixedLibrary(library_b_root).build([shared_b])

    profile = offline_fake_profile(dimensions=8)
    profile_id: str | None = None
    for library_root, snapshot_id in (
        (library_a_root, a_s1["snapshot_id"]),
        (library_a_root, a_s2["snapshot_id"]),
        (library_b_root, b_s1["snapshot_id"]),
    ):
        built = build_vectors(
            library_root,
            snapshot_id,
            profile,
            OfflineDeterministicFakeEmbedder(),
        )
        if profile_id is None:
            profile_id = built["profile_id"]
        elif built["profile_id"] != profile_id:
            raise RuntimeError("离线评测向量 profile 身份不一致。")
    assert profile_id is not None

    snapshots = {
        "A_S1": _load_snapshot(
            library_a_id, library_a_root, a_s1["snapshot_id"], profile_id
        ),
        "A_S2": _load_snapshot(
            library_a_id, library_a_root, a_s2["snapshot_id"], profile_id
        ),
        "B_S1": _load_snapshot(
            library_b_id, library_b_root, b_s1["snapshot_id"], profile_id
        ),
    }
    document_owners: dict[str, set[str]] = {}
    for snapshot in snapshots.values():
        for document_id in snapshot.documents.values():
            document_owners.setdefault(document_id, set()).add(snapshot.library_id)
    return snapshots, profile, document_owners


QUESTIONS = (
    _Question(
        "LIB-A",
        "alpha_library_marker shared calibration",
        "en",
        "library_isolation_a",
        "A_S2",
        "shared.md",
        "alpha_library_marker",
        "alpha_library_marker shared calibration",
    ),
    _Question(
        "LIB-B",
        "beta_library_marker shared calibration",
        "en",
        "library_isolation_b",
        "B_S1",
        "shared.md",
        "beta_library_marker",
        "beta_library_marker shared calibration",
    ),
    _Question(
        "SNAPSHOT-EMPTY",
        "snapshot_new_marker",
        "en",
        "snapshot_isolation_empty",
        "A_S1",
        None,
        None,
        "snapshot_new_marker",
        forbidden_selection="A_S2",
        forbidden_source="new.md",
    ),
    _Question(
        "SNAPSHOT-S2",
        "snapshot_new_marker",
        "en",
        "snapshot_isolation_positive",
        "A_S2",
        "new.md",
        "snapshot_new_marker",
        "snapshot_new_marker",
    ),
    _Question(
        "LOCAL-ZH",
        "中文 本地 标记",
        "zh",
        "local_zh",
        "A_S2",
        "chinese.md",
        "中文 本地 标记",
        "中文 本地 标记",
    ),
    _Question(
        "LOCAL-EN",
        "english_local_marker entropy balance",
        "en",
        "local_en",
        "A_S2",
        "shared.md",
        "english_local_marker",
        "english_local_marker entropy balance",
    ),
    _Question(
        "CROSS-ZH-EN",
        "涡轮机的熵平衡证据在哪里？",
        "zh_to_en",
        "cross_language_zh_to_en",
        "A_S2",
        "shared.md",
        "english_local_marker",
        "english_local_marker entropy balance",
        known_bm25_limitation=True,
    ),
    _Question(
        "CROSS-EN-ZH",
        "Where is the temperature-gradient conduction evidence?",
        "en_to_zh",
        "cross_language_en_to_zh",
        "A_S2",
        "chinese.md",
        "温度梯度",
        "中文 本地 标记 温度梯度 导热",
        known_bm25_limitation=True,
    ),
    _Question(
        "LEXICAL-HARD",
        "热量怎样扩散？",
        "zh",
        "lexical_difficulty",
        "A_S2",
        "chinese.md",
        "温度梯度",
        "温度梯度 驱动 导热",
        known_bm25_limitation=True,
    ),
    _Question(
        "TRUE-EMPTY",
        "no_such_evidence_7f3a9d",
        "en",
        "true_negative",
        "A_S2",
        None,
        None,
        "no_such_evidence_7f3a9d",
    ),
    _Question(
        "TRACE-MD",
        "markdown_anchor_marker provenance chain",
        "en",
        "trace_markdown",
        "A_S2",
        "shared.md",
        "markdown_anchor_marker",
        "markdown_anchor_marker provenance chain",
        traceability="markdown_anchor_line",
    ),
    _Question(
        "TRACE-PDF",
        "pdf_page_marker traceable page",
        "en",
        "trace_pdf",
        "A_S2",
        "pages.pdf",
        "pdf_page_marker",
        "pdf_page_marker traceable page",
        traceability="pdf_page_2",
    ),
)


class _ScriptedTransport:
    simulated = True

    def __init__(
        self,
        q0: str,
        rewritten_query: str,
        query_vector: Sequence[float],
        target_chunk_id: str | None,
    ) -> None:
        self._q0 = q0
        self._rewritten_query = rewritten_query
        self._query_vector = list(query_vector)
        self._target_chunk_id = target_chunk_id
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        role: str,
        provider: str,
        model_id: str,
        payload: Mapping[str, Any],
    ) -> object:
        if len(self.calls) >= len(ROLE_ORDER) or role != ROLE_ORDER[len(self.calls)]:
            raise RuntimeError("scripted role order mismatch")
        self.calls.append(
            {
                "role": role,
                "provider": provider,
                "model_id": model_id,
                "payload": json.loads(
                    json.dumps(payload, ensure_ascii=False, allow_nan=False)
                ),
            }
        )
        if role == QUERY_REWRITE:
            if set(payload) != {"query"} or payload["query"] != self._q0:
                raise RuntimeError("scripted rewrite payload mismatch")
            return self._rewritten_query
        if role == VECTOR_RECALL:
            if set(payload) != {"query"} or payload["query"] != self._rewritten_query:
                raise RuntimeError("scripted vector payload mismatch")
            return list(self._query_vector)
        if set(payload) != {"query", "candidates"}:
            raise RuntimeError("scripted rerank payload mismatch")
        if payload["query"] != self._rewritten_query:
            raise RuntimeError("scripted rerank query mismatch")
        candidate_ids = [item["chunk_id"] for item in payload["candidates"]]
        if self._target_chunk_id is None:
            return []
        if self._target_chunk_id not in candidate_ids:
            return []
        return [{"chunk_id": self._target_chunk_id, "score": 1.0}]


def _target(
    question: _Question,
    snapshot: _SnapshotData,
) -> tuple[str | None, str | None]:
    if question.expected_source is None:
        return None, None
    document_id = snapshot.documents[question.expected_source]
    matches = [
        chunk_id
        for chunk_id, chunk in snapshot.chunks.items()
        if chunk["document_id"] == document_id
        and question.target_marker in chunk["chunk_text"]
    ]
    if not matches:
        raise RuntimeError("合成题集的目标 marker 未进入所选快照。")
    return document_id, sorted(matches)[0]


def _public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "document_id": result["document_id"],
        "asset_id": result["asset_id"],
        "chunk_id": result["chunk_id"],
        "anchor_label": result["anchor_label"],
        "pdf_page_start": result["pdf_page_start"],
        "pdf_page_end": result["pdf_page_end"],
        "source_line_start": result["source_line_start"],
        "source_line_end": result["source_line_end"],
        "fulltext_verified": result["fulltext_verified"],
        "formula_verified": result["formula_verified"],
    }


def _bm25_search(
    snapshot: _SnapshotData, query: str
) -> tuple[dict[str, Any], str]:
    library = FixedLibrary(snapshot.library_root)
    result = library.search(
        snapshot.snapshot_id,
        query,
        top_k=TOP_K,
        excerpt_chars=240,
    )
    return result, library._root.name


def _evaluate_case(
    question: _Question,
    mode: str,
    snapshots: Mapping[str, _SnapshotData],
    profile: Mapping[str, Any],
    document_owners: Mapping[str, set[str]],
) -> dict[str, Any]:
    snapshot = snapshots[question.selection]
    expected_document_id, target_chunk_id = _target(question, snapshot)
    forbidden_document_id = None
    if question.forbidden_selection and question.forbidden_source:
        forbidden_document_id = snapshots[question.forbidden_selection].documents[
            question.forbidden_source
        ]

    expected_trace = (
        {
            key: snapshot.chunks[target_chunk_id][key]
            for key in (
                "anchor_label",
                "pdf_page_start",
                "pdf_page_end",
                "source_line_start",
                "source_line_end",
            )
        }
        if target_chunk_id is not None
        else None
    )
    transport: _ScriptedTransport | None = None
    simulated = mode == "enhanced"
    call_count = 0
    error: str | None = None
    actual_library_id: str | None = None
    actual_snapshot_id: str | None = None
    result: dict[str, Any] = {"found": None, "results": []}
    try:
        if mode == "bm25":
            result, actual_library_id = _bm25_search(snapshot, question.q0)
            returned_snapshot_id = result.get("snapshot_id")
            if isinstance(returned_snapshot_id, str):
                actual_snapshot_id = returned_snapshot_id
        else:
            vector_chunk_id = target_chunk_id or sorted(snapshot.vectors)[0]
            transport = _ScriptedTransport(
                question.q0,
                question.rewritten_query,
                snapshot.vectors[vector_chunk_id],
                target_chunk_id,
            )
            service = EnhancedSearchService(
                EnhancedSearchConfig(
                    provider=profile["provider"],
                    query_rewrite_model_id="offline-stage7-rewrite-v1",
                    vector_recall_model_id=profile["model_id"],
                    candidate_rerank_model_id="offline-stage7-rerank-v1",
                    vector_profile=profile,
                    max_rerank_candidates=5,
                    max_candidate_text_chars=240,
                    max_rerank_total_chars=1000,
                ),
                transport,
            )
            result = FixedLibrary(
                snapshot.library_root,
                library_id=snapshot.library_id,
                enhanced_search=service,
            ).search(
                snapshot.snapshot_id,
                question.q0,
                top_k=TOP_K,
                excerpt_chars=240,
                mode="enhanced",
            )
            returned_library_id = result.get("library_id")
            returned_snapshot_id = result.get("snapshot_id")
            if isinstance(returned_library_id, str):
                actual_library_id = returned_library_id
            if isinstance(returned_snapshot_id, str):
                actual_snapshot_id = returned_snapshot_id
            call_count = result["audit"]["call_count"]
            simulated = result["audit"]["simulated"] is True
    except EnhancedSearchError as exc:
        call_count = int(exc.audit["call_count"])
        simulated = exc.audit["simulated"] is True
        error = "simulated enhanced case failed closed"
    except LiteratureEvidenceError:
        error = "offline evaluation search failed closed"

    results = list(result["results"])
    rank = next(
        (
            index
            for index, item in enumerate(results, start=1)
            if item["document_id"] == expected_document_id
        ),
        None,
    )
    selected_documents = set(snapshot.documents.values())
    selected_chunks = set(snapshot.chunks)
    library_violations = int(actual_library_id != snapshot.library_id) + sum(
        1
        for item in results
        if snapshot.library_id
        not in document_owners.get(item["document_id"], set())
    )
    snapshot_violations = int(actual_snapshot_id != snapshot.snapshot_id) + sum(
        1
        for item in results
        if item["document_id"] not in selected_documents
        or item["chunk_id"] not in selected_chunks
    )
    consistent_empty = (
        error is None and result["found"] is False and results == []
    )
    transport_ok = (
        mode == "bm25"
        or (
            transport is not None
            and [item["role"] for item in transport.calls] == list(ROLE_ORDER)
            and call_count == 3
            and simulated
        )
    )
    if expected_document_id is None:
        behavior_pass = consistent_empty
        acceptance = "true_empty"
    elif mode == "bm25" and question.known_bm25_limitation:
        behavior_pass = True
        acceptance = "observational_known_bm25_limitation"
    else:
        behavior_pass = rank is not None
        acceptance = "expected_document_hit"

    traceability_pass: bool | None = None
    if question.traceability is not None:
        assert target_chunk_id is not None and expected_trace is not None
        traceability_pass = any(
            item["chunk_id"] == target_chunk_id
            and all(item[key] == expected_trace[key] for key in expected_trace)
            for item in results
        )

    case_pass = (
        error is None
        and behavior_pass
        and transport_ok
        and library_violations == 0
        and snapshot_violations == 0
        and traceability_pass is not False
    )
    return {
        "case_id": f"{question.case_id}-{mode.upper()}",
        "q0": question.q0,
        "language": question.language,
        "route": question.route,
        "mode": mode,
        "selection": {
            "library_id": snapshot.library_id,
            "snapshot_id": snapshot.snapshot_id,
        },
        "expected": {
            "document_id": expected_document_id,
            "chunk_id": target_chunk_id,
            "empty": expected_document_id is None,
            "forbidden_document_id": forbidden_document_id,
            "traceability": question.traceability,
            "trace": expected_trace,
        },
        "observed": {
            "status": "ok" if error is None else "error",
            "library_id": actual_library_id,
            "snapshot_id": actual_snapshot_id,
            "found": result["found"],
            "results": [_public_result(item) for item in results],
            "first_expected_rank": rank,
        },
        "acceptance": acceptance,
        "known_limitation": (
            "BM25 不做语义改写或跨语言映射；本例只记录真实观测。"
            if mode == "bm25" and question.known_bm25_limitation
            else None
        ),
        "simulated": simulated,
        "call_count": call_count,
        "transport_order_ok": transport_ok,
        "library_isolation_violations": library_violations,
        "snapshot_isolation_violations": snapshot_violations,
        "traceability_pass": traceability_pass,
        "metric_hit": (
            error is None
            and rank is not None
            and library_violations == 0
            and snapshot_violations == 0
        ),
        "case_pass": case_pass,
        "error": error,
    }


def _ratio(numerator: int | float, denominator: int) -> float:
    return round(float(numerator) / denominator, 6) if denominator else 1.0


def _metrics(cases: Sequence[Mapping[str, Any]], mode: str) -> dict[str, Any]:
    selected = [case for case in cases if case["mode"] == mode]
    positives = [case for case in selected if not case["expected"]["empty"]]
    negatives = [case for case in selected if case["expected"]["empty"]]
    hits = [case for case in positives if case["metric_hit"]]
    reciprocal_rank = sum(
        1.0 / case["observed"]["first_expected_rank"] for case in hits
    )
    hit_ranks = [case["observed"]["first_expected_rank"] for case in hits]
    trace_cases = [
        case for case in selected if case["expected"]["traceability"] is not None
    ]
    applicable = [
        case
        for case in positives
        if case["acceptance"] != "observational_known_bm25_limitation"
    ]
    return {
        "case_count": len(selected),
        "positive_case_count": len(positives),
        "positive_hit_at_k": _ratio(len(hits), len(positives)),
        "positive_recall_at_k": _ratio(len(hits), len(positives)),
        "mrr": _ratio(reciprocal_rank, len(positives)),
        "first_hit_rank_mean": (
            round(sum(hit_ranks) / len(hit_ranks), 6) if hit_ranks else None
        ),
        "applicable_positive_hit_at_k": _ratio(
            sum(1 for case in applicable if case["metric_hit"]), len(applicable)
        ),
        "true_empty_accuracy": _ratio(
            sum(
                1
                for case in negatives
                if case["error"] is None
                and case["observed"]["found"] is False
                and case["observed"]["results"] == []
            ),
            len(negatives),
        ),
        "library_isolation_violations": sum(
            case["library_isolation_violations"] for case in selected
        ),
        "snapshot_isolation_violations": sum(
            case["snapshot_isolation_violations"] for case in selected
        ),
        "traceability_coverage": _ratio(
            sum(case["traceability_pass"] is True for case in trace_cases),
            len(trace_cases),
        ),
        "contract_passed_case_count": sum(
            case["case_pass"] is True for case in selected
        ),
    }


def run_evaluation() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="lemcp-stage7-offline-") as temporary:
        snapshots, profile, document_owners = _build_fixture(Path(temporary))
        cases = [
            _evaluate_case(question, mode, snapshots, profile, document_owners)
            for question in QUESTIONS
            for mode in ("bm25", "enhanced")
        ]
    metrics = {
        mode: _metrics(cases, mode) for mode in ("bm25", "enhanced")
    }
    passed = all(case["case_pass"] for case in cases) and all(
        values["library_isolation_violations"] == 0
        and values["snapshot_isolation_violations"] == 0
        and values["true_empty_accuracy"] == 1.0
        and values["traceability_coverage"] == 1.0
        for values in metrics.values()
    )
    return {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "evidence_level": "offline_simulated",
        "claim_boundary": DISCLAIMER,
        "real_model_calls": 0,
        "network_calls": 0,
        "passed": passed,
        "dataset": {
            "fixture_policy": "synthetic_shareable",
            "library_count": 2,
            "snapshot_count": 3,
            "logical_question_count": len(QUESTIONS),
            "case_count": len(cases),
            "top_k": TOP_K,
            "modes": ["bm25", "enhanced"],
            "languages": ["zh", "en", "zh_to_en", "en_to_zh"],
            "same_name_source": "shared.md",
        },
        "metrics": metrics,
        "cases": cases,
        "user_explanation": {
            "summary_zh": (
                "这份报告检查本地 BM25 与离线脚本 fake 的检索管线是否按合同工作。"
            ),
            "case_pass_hint_zh": (
                "case_pass 表示该病例按预定合同完成或如实记录已知 BM25 局限，"
                "不等于预期文档命中；检索命中必须看 positive hit@k 和 MRR。"
            ),
            "metric_hint_zh": (
                "hit@k 看预期文档是否进入前 k 条，MRR 越接近 1 表示首次命中越靠前；"
                "true-empty accuracy 检查不存在证据时是否真实返回空。"
            ),
            "limitations_zh": [
                DISCLAIMER,
                "跨语言 enhanced 命中由逐题脚本 fake 提供，不是模型能力证据。",
                "BM25 跨语言或语义同义词失败会如实计入 positive hit@k 与 MRR。",
            ],
        },
    }


class _ArgumentParseError(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentParseError from None


def _parser() -> argparse.ArgumentParser:
    return _SafeArgumentParser(
        prog="python -m literature_evidence_mcp.quality_eval",
        description="运行 Stage 7 合成离线搜索质量验收并把 JSON 报告写到 stdout。",
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _parser().parse_args(argv)
        report = run_evaluation()
    except _ArgumentParseError:
        sys.stderr.write("错误：Stage 7 离线评测参数无效。\n")
        return 2
    except Exception:
        sys.stderr.write("错误：Stage 7 离线评测未完成；未生成质量结论。\n")
        return 2
    sys.stdout.write(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_evaluation"]
