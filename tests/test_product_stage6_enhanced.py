from __future__ import annotations

import copy
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mcp.types import CallToolRequestParams, TextContent
from starlette.testclient import TestClient

from literature_evidence_mcp.enhanced import (
    CANDIDATE_RERANK,
    QUERY_REWRITE,
    VECTOR_RECALL,
    EnhancedSearchConfig,
    EnhancedSearchService,
    MAX_CANDIDATE_TEXT_CHARS,
    MAX_RERANK_CANDIDATES,
    MAX_RERANK_TOTAL_CHARS,
    _cosine,
)
from literature_evidence_mcp.errors import EnhancedSearchError
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.mcp_server import TOOLS, create_server
from literature_evidence_mcp.mcp_tools import ReadOnlyEvidenceTools
from literature_evidence_mcp.registry import LibraryRegistry
from literature_evidence_mcp.snapshot import _open_verified_snapshot
from literature_evidence_mcp.vectors import (
    OfflineDeterministicFakeEmbedder,
    build_vectors,
    load_verified_vectors,
    offline_fake_profile,
)
from literature_evidence_mcp.web import create_app


PORT = 19226
ORIGIN = f"http://127.0.0.1:{PORT}"
_DEFAULT = object()


def _tree_identity(root: Path) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", str(path.readlink()))
        elif path.is_dir():
            result[relative] = ("directory", "")
        elif path.is_file():
            result[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return result


def _mcp_payload(result: Any) -> dict[str, Any]:
    if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
        raise AssertionError("MCP result must contain one JSON text block")
    value = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        raise AssertionError("MCP result must be one JSON object")
    return value


class RecordingFake:
    simulated = True

    def __init__(
        self,
        query_vector: list[float],
        *,
        rewritten: str = "rewritten alpha question",
        fail_role: str | None = None,
        outputs: dict[str, object] | None = None,
    ) -> None:
        self.query_vector = query_vector
        self.rewritten = rewritten
        self.fail_role = fail_role
        self.outputs = {} if outputs is None else outputs
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        role: str,
        provider: str,
        model_id: str,
        payload: dict[str, Any],
    ) -> object:
        recorded = {
            "role": role,
            "provider": provider,
            "model_id": model_id,
            "payload": copy.deepcopy(payload),
        }
        self.calls.append(recorded)
        if role == self.fail_role:
            raise RuntimeError("transport-secret-must-not-leak")
        configured = self.outputs.get(role, _DEFAULT)
        if configured is not _DEFAULT:
            return configured(payload) if callable(configured) else configured
        if role == QUERY_REWRITE:
            return self.rewritten
        if role == VECTOR_RECALL:
            return list(self.query_vector)
        return [
            {"chunk_id": item["chunk_id"], "score": 1.0 - index / 10}
            for index, item in enumerate(payload["candidates"])
        ]


class ExplodingEnhanced:
    def search(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("BM25 touched enhanced service")


class ProductStageSixEnhancedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lemcp-product-stage6-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.application_root = self.root / "application"
        registry = LibraryRegistry(self.application_root)
        record = registry.create("阶段六合成库")
        self.library_id = str(record["library_id"])
        self.library_root = Path(str(record["library_root"]))

        sources = self.root / "inputs"
        sources.mkdir()
        alpha = sources / "alpha.md"
        beta = sources / "beta.md"
        alpha.write_text(
            "# Alpha\n\nalpha_marker evidence with a deliberately bounded candidate body.\n",
            encoding="utf-8",
        )
        beta.write_text(
            "# Beta\n\nbeta_marker evidence from the same frozen snapshot only.\n",
            encoding="utf-8",
        )
        snapshot = FixedLibrary(self.library_root).build([alpha, beta])
        self.snapshot_id = str(snapshot["snapshot_id"])
        self.profile = offline_fake_profile(dimensions=4)
        self.vector_build = build_vectors(
            self.library_root,
            self.snapshot_id,
            self.profile,
            OfflineDeterministicFakeEmbedder(),
        )
        loaded = load_verified_vectors(
            self.library_root,
            self.snapshot_id,
            str(self.vector_build["profile_id"]),
        )
        self.query_vector = list(loaded["vectors"][0]["vector"])
        self.vectors = {
            item["chunk_id"]: tuple(item["vector"])
            for item in loaded["vectors"]
        }
        status, database = _open_verified_snapshot(
            self.library_root / "snapshots" / self.snapshot_id
        )
        self.assertEqual(status["snapshot_id"], self.snapshot_id)
        try:
            rows = database.execute(
                "SELECT chunk_id,chunk_text FROM chunk ORDER BY chunk_id"
            ).fetchall()
        finally:
            database.close()
        self.chunk_text = {row["chunk_id"]: row["chunk_text"] for row in rows}
        self.config = EnhancedSearchConfig(
            provider=self.profile["provider"],
            query_rewrite_model_id="offline-rewrite-v1",
            vector_recall_model_id=self.profile["model_id"],
            candidate_rerank_model_id="offline-rerank-v1",
            vector_profile=self.profile,
        )

    def _service(
        self,
        fake: RecordingFake,
        *,
        config: EnhancedSearchConfig | None = None,
    ) -> EnhancedSearchService:
        return EnhancedSearchService(self.config if config is None else config, fake)

    def _library(self, service: object | None) -> FixedLibrary:
        return FixedLibrary(
            self.library_root,
            library_id=self.library_id,
            enhanced_search=service,
        )

    def _enhanced(
        self,
        fake: RecordingFake,
        *,
        query: str = "Where is alpha evidence?",
        config: EnhancedSearchConfig | None = None,
    ) -> dict[str, Any]:
        service = self._service(fake, config=config)
        return self._library(service).search(
            self.snapshot_id,
            query,
            mode="enhanced",
            top_k=2,
            excerpt_chars=18,
        )

    def _expected_candidates(self) -> list[dict[str, str]]:
        ranked = sorted(
            (
                (_cosine(self.query_vector, vector), chunk_id)
                for chunk_id, vector in self.vectors.items()
            ),
            key=lambda item: (-item[0], item[1]),
        )[:MAX_RERANK_CANDIDATES]
        remaining = MAX_RERANK_TOTAL_CHARS
        expected: list[dict[str, str]] = []
        for _score, chunk_id in ranked:
            text = self.chunk_text[chunk_id][
                : min(MAX_CANDIDATE_TEXT_CHARS, remaining)
            ]
            if text:
                expected.append({"chunk_id": chunk_id, "text": text})
                remaining -= len(text)
            if remaining == 0:
                break
        return expected

    def test_config_bounds_only_tighten_and_transport_truth_is_explicit(self) -> None:
        base = {
            "provider": self.profile["provider"],
            "query_rewrite_model_id": "offline-rewrite-v1",
            "vector_recall_model_id": self.profile["model_id"],
            "candidate_rerank_model_id": "offline-rerank-v1",
            "vector_profile": self.profile,
        }
        for name, value in (
            ("max_rewrite_chars", 401),
            ("max_rerank_candidates", 11),
            ("max_candidate_text_chars", 601),
            ("max_rerank_total_chars", 4001),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                EnhancedSearchConfig(**base, **{name: value})

        class MissingTruth:
            def invoke(self, **_kwargs: object) -> object:
                return None

        class InvalidTruth(MissingTruth):
            simulated = "yes"

        for transport in (MissingTruth(), InvalidTruth()):
            with self.subTest(transport=type(transport).__name__), self.assertRaises(
                ValueError
            ):
                EnhancedSearchService(self.config, transport)

        explicit_real = RecordingFake(self.query_vector)
        explicit_real.simulated = False
        summary = self._service(explicit_real).public_summary()
        self.assertIs(summary["simulated"], False)

    def test_success_exact_payload_audit_empty_and_readonly_tree(self) -> None:
        before = _tree_identity(self.application_root)
        fake = RecordingFake(self.query_vector)
        result = self._enhanced(fake)

        self.assertEqual(
            fake.calls,
            [
                {
                    "role": QUERY_REWRITE,
                    "provider": self.config.provider,
                    "model_id": self.config.query_rewrite_model_id,
                    "payload": {"query": "Where is alpha evidence?"},
                },
                {
                    "role": VECTOR_RECALL,
                    "provider": self.config.provider,
                    "model_id": self.config.vector_recall_model_id,
                    "payload": {"query": "rewritten alpha question"},
                },
                {
                    "role": CANDIDATE_RERANK,
                    "provider": self.config.provider,
                    "model_id": self.config.candidate_rerank_model_id,
                    "payload": {
                        "query": "rewritten alpha question",
                        "candidates": self._expected_candidates(),
                    },
                },
            ],
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["library_id"], self.library_id)
        self.assertEqual(result["snapshot_id"], self.snapshot_id)
        self.assertEqual(result["retrieval_mode"], "enhanced")
        self.assertEqual(result["audit"]["call_count"], 3)
        self.assertIs(result["audit"]["simulated"], True)
        self.assertEqual(
            [item["role"] for item in result["audit"]["calls"]],
            [QUERY_REWRITE, VECTOR_RECALL, CANDIDATE_RERANK],
        )
        third = result["audit"]["calls"][2]
        self.assertEqual(third["candidate_count"], len(self._expected_candidates()))
        expected_chars = sum(len(item["text"]) for item in self._expected_candidates())
        self.assertEqual(third["candidate_chars"], expected_chars)
        self.assertLessEqual(third["candidate_count"], MAX_RERANK_CANDIDATES)
        self.assertLessEqual(third["candidate_chars"], MAX_RERANK_TOTAL_CHARS)
        self.assertTrue(
            all(
                len(item["text"]) <= MAX_CANDIDATE_TEXT_CHARS
                for item in self._expected_candidates()
            )
        )
        self.assertEqual(
            third["candidate_ids"],
            [item["chunk_id"] for item in self._expected_candidates()],
        )
        serialized_audit = json.dumps(result["audit"], ensure_ascii=False)
        self.assertNotIn("Where is alpha evidence?", serialized_audit)
        self.assertNotIn("rewritten alpha question", serialized_audit)
        self.assertNotIn(str(self.root), serialized_audit)
        for candidate in self._expected_candidates():
            self.assertNotIn(candidate["text"], serialized_audit)
        self.assertEqual(before, _tree_identity(self.application_root))

        empty_fake = RecordingFake(
            self.query_vector, outputs={CANDIDATE_RERANK: []}
        )
        empty = self._enhanced(empty_fake)
        self.assertFalse(empty["found"])
        self.assertEqual(empty["results"], [])
        self.assertEqual(empty["audit"]["call_count"], 3)
        self.assertIs(empty["audit"]["simulated"], True)

    def test_first_error_stops_after_one_two_or_three_attempts(self) -> None:
        for expected, role in enumerate(
            (QUERY_REWRITE, VECTOR_RECALL, CANDIDATE_RERANK), start=1
        ):
            with self.subTest(role=role):
                fake = RecordingFake(self.query_vector, fail_role=role)
                with self.assertRaises(EnhancedSearchError) as raised:
                    self._enhanced(fake)
                self.assertEqual(len(fake.calls), expected)
                self.assertEqual(raised.exception.audit["call_count"], expected)
                self.assertIs(raised.exception.audit["simulated"], True)
                self.assertEqual(
                    [item["role"] for item in raised.exception.audit["calls"]],
                    list((QUERY_REWRITE, VECTOR_RECALL, CANDIDATE_RERANK)[:expected]),
                )
                self.assertNotIn("transport-secret", str(raised.exception))
                self.assertNotIn(
                    "transport-secret",
                    json.dumps(raised.exception.audit, ensure_ascii=False),
                )

    def test_malformed_rewrite_vector_and_rerank_fail_closed(self) -> None:
        malformed = [
            (QUERY_REWRITE, "", 1),
            (QUERY_REWRITE, "x" * 401, 1),
            (QUERY_REWRITE, {"query": "not text"}, 1),
            (VECTOR_RECALL, [1.0, 2.0], 2),
            (VECTOR_RECALL, [True, 0.0, 0.0, 0.0], 2),
            (VECTOR_RECALL, [math.nan, 0.0, 0.0, 0.0], 2),
            (VECTOR_RECALL, [0.0, 0.0, 0.0, 0.0], 2),
            (
                CANDIDATE_RERANK,
                [{"chunk_id": "chunk_ffffffffffffffffffffffff", "score": 1.0}],
                3,
            ),
            (
                CANDIDATE_RERANK,
                lambda payload: [
                    {"chunk_id": payload["candidates"][0]["chunk_id"], "score": 1.0},
                    {"chunk_id": payload["candidates"][0]["chunk_id"], "score": 0.5},
                ],
                3,
            ),
            (
                CANDIDATE_RERANK,
                lambda payload: [
                    {"chunk_id": payload["candidates"][0]["chunk_id"], "score": math.inf}
                ],
                3,
            ),
        ]
        for role, output, expected_calls in malformed:
            with self.subTest(role=role, output=repr(output)):
                fake = RecordingFake(
                    self.query_vector,
                    outputs={role: output},
                )
                with self.assertRaises(EnhancedSearchError) as raised:
                    self._enhanced(fake)
                self.assertEqual(len(fake.calls), expected_calls)
                self.assertEqual(raised.exception.audit["call_count"], expected_calls)

    def test_missing_or_wrong_profile_and_invalid_input_are_zero_call(self) -> None:
        wrong_profile = offline_fake_profile(
            dimensions=4,
            instruction="A different exact document-vector identity.",
        )
        wrong_config = EnhancedSearchConfig(
            provider=wrong_profile["provider"],
            query_rewrite_model_id="offline-rewrite-v1",
            vector_recall_model_id=wrong_profile["model_id"],
            candidate_rerank_model_id="offline-rerank-v1",
            vector_profile=wrong_profile,
        )
        for config, query in ((wrong_config, "valid question"), (self.config, "")):
            with self.subTest(profile=config.vector_profile_id, query=query):
                fake = RecordingFake(self.query_vector)
                with self.assertRaises(EnhancedSearchError) as raised:
                    self._enhanced(fake, query=query, config=config)
                self.assertEqual(fake.calls, [])
                self.assertEqual(
                    raised.exception.audit,
                    {"simulated": True, "call_count": 0, "calls": []},
                )

    def test_missing_or_tampered_artifact_is_zero_call_and_not_repaired(self) -> None:
        artifact = self.library_root / str(self.vector_build["artifact_path"])
        artifact.unlink()
        damaged = _tree_identity(self.application_root)
        fake = RecordingFake(self.query_vector)
        with self.assertRaises(EnhancedSearchError) as raised:
            self._enhanced(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(raised.exception.audit["call_count"], 0)
        self.assertEqual(damaged, _tree_identity(self.application_root))

    def test_tampered_vector_payload_and_cross_identity_are_zero_call(self) -> None:
        manifest = json.loads(
            (
                self.library_root / str(self.vector_build["artifact_path"])
            ).read_text(encoding="utf-8")
        )
        payload = self.library_root / manifest["objects"][0]["path"]
        raw = bytearray(payload.read_bytes())
        raw[0] ^= 1
        payload.write_bytes(bytes(raw))
        fake = RecordingFake(self.query_vector)
        with self.assertRaises(EnhancedSearchError) as raised:
            self._enhanced(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(raised.exception.audit["call_count"], 0)

        clean_fake = RecordingFake(self.query_vector)
        clean_service = self._service(clean_fake)
        mismatched = FixedLibrary(
            self.library_root,
            library_id="lib_ffffffffffffffffffffffffffffffff",
            enhanced_search=clean_service,
        )
        with self.assertRaises(EnhancedSearchError) as cross:
            mismatched.search(
                self.snapshot_id,
                "valid question",
                mode="enhanced",
            )
        self.assertEqual(clean_fake.calls, [])
        self.assertEqual(cross.exception.audit["call_count"], 0)

    def test_default_and_explicit_bm25_are_identical_and_never_touch_enhanced(self) -> None:
        library = self._library(ExplodingEnhanced())
        implicit = library.search(self.snapshot_id, "alpha_marker")
        explicit = library.search(self.snapshot_id, "alpha_marker", mode="bm25")
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit["retrieval_mode"], "bm25")
        with self.assertRaises(EnhancedSearchError) as unavailable:
            self._library(None).search(
                self.snapshot_id,
                "alpha_marker",
                mode="enhanced",
            )
        self.assertEqual(unavailable.exception.audit["call_count"], 0)
        self.assertIsNone(unavailable.exception.audit["simulated"])

    def test_http_modes_status_simulation_audit_ui_and_no_leak(self) -> None:
        fake = RecordingFake(self.query_vector)
        service = self._service(fake)
        with TestClient(
            create_app(
                self.application_root,
                port=PORT,
                enhanced_search=service,
            ),
            base_url=ORIGIN,
            raise_server_exceptions=False,
        ) as client:
            status = client.get("/api/status")
            self.assertEqual(status.status_code, 200, status.text)
            status_payload = status.json()
            self.assertTrue(status_payload["enhanced_available"])
            self.assertTrue(status_payload["enhanced"]["simulated"])
            self.assertEqual(
                [item["role"] for item in status_payload["enhanced"]["roles"]],
                [QUERY_REWRITE, VECTOR_RECALL, CANDIDATE_RERANK],
            )
            headers = {
                "Origin": ORIGIN,
                "X-CSRF-Token": status_payload["csrf_token"],
                "X-Action-Intent": "search",
                "Content-Type": "application/json",
            }
            base = {
                "snapshot_id": self.snapshot_id,
                "query": "alpha_marker",
                "top_k": 2,
                "excerpt_chars": 18,
            }
            path = f"/api/libraries/{self.library_id}/search"
            for body in (
                {**base, "mode": "enhanced", "unexpected": "do-not-leak"},
                {
                    "snapshot_id": self.snapshot_id,
                    "mode": "enhanced",
                },
            ):
                with self.subTest(http_body=body):
                    invalid = client.post(
                        path,
                        headers=headers,
                        content=json.dumps(body),
                    )
                    self.assertEqual(invalid.status_code, 422, invalid.text)
                    self.assertEqual(
                        invalid.json()["audit"],
                        {"simulated": True, "call_count": 0, "calls": []},
                    )
                    self.assertNotIn("do-not-leak", invalid.text)
                    self.assertEqual(fake.calls, [])
            implicit = client.post(path, headers=headers, content=json.dumps(base))
            explicit = client.post(
                path,
                headers=headers,
                content=json.dumps({**base, "mode": "bm25"}),
            )
            self.assertEqual(implicit.status_code, 200, implicit.text)
            self.assertEqual(implicit.json(), explicit.json())
            self.assertEqual(fake.calls, [])

            enhanced = client.post(
                path,
                headers=headers,
                content=json.dumps({**base, "mode": "enhanced"}),
            )
            self.assertEqual(enhanced.status_code, 200, enhanced.text)
            self.assertEqual(enhanced.json()["audit"]["call_count"], 3)
            self.assertIs(enhanced.json()["audit"]["simulated"], True)
            self.assertEqual(len(fake.calls), 3)
            serialized = enhanced.text
            self.assertNotIn(str(self.root), serialized)
            self.assertNotIn("transport-secret", serialized)

            page = client.get("/")
            script = client.get("/static/app.js")
            self.assertNotIn("https://", page.text + script.text)
            self.assertNotIn("innerHTML", script.text)
            self.assertIn("增强搜索（离线模拟）", script.text)
            self.assertIn("enhancedSimulated: null", script.text)
            self.assertIn("离线模拟增强搜索", script.text)
            self.assertIn("联网增强搜索", script.text)
            self.assertIn("增强状态未知", script.text)
            self.assertIn("error.payload.audit", script.text)

        failing = RecordingFake(self.query_vector, fail_role=VECTOR_RECALL)
        with TestClient(
            create_app(
                self.application_root,
                port=PORT + 1,
                enhanced_search=self._service(failing),
            ),
            base_url=f"http://127.0.0.1:{PORT + 1}",
            raise_server_exceptions=False,
        ) as client:
            status = client.get("/api/status").json()
            response = client.post(
                f"/api/libraries/{self.library_id}/search",
                headers={
                    "Origin": f"http://127.0.0.1:{PORT + 1}",
                    "X-CSRF-Token": status["csrf_token"],
                    "X-Action-Intent": "search",
                    "Content-Type": "application/json",
                },
                content=json.dumps(
                    {
                        "snapshot_id": self.snapshot_id,
                        "query": "valid question",
                        "mode": "enhanced",
                    }
                ),
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["audit"]["call_count"], 2)
            self.assertIs(response.json()["audit"]["simulated"], True)
            self.assertNotIn("transport-secret", response.text)
            self.assertNotIn(str(self.root), response.text)

    async def test_mcp_fake_chain_schema_bm25_isolation_and_failure_audit(self) -> None:
        fake = RecordingFake(self.query_vector)
        tools = ReadOnlyEvidenceTools(
            self.application_root,
            enhanced_search=self._service(fake),
        )
        server = create_server(tools)
        handler = server._request_handlers["tools/call"].handler
        base = {
            "library_id": self.library_id,
            "snapshot_id": self.snapshot_id,
            "query": "alpha_marker",
            "top_k": 2,
            "excerpt_chars": 18,
        }
        for arguments in (
            {
                "library_id": self.library_id,
                "snapshot_id": self.snapshot_id,
                "mode": "enhanced",
            },
            {**base, "mode": "enhanced", "unexpected": "do-not-leak"},
        ):
            with self.subTest(mcp_arguments=arguments):
                invalid_protocol = await handler(
                    None,
                    CallToolRequestParams(
                        name="search_documents", arguments=arguments
                    ),
                )
                self.assertTrue(invalid_protocol.is_error)
                invalid_payload = _mcp_payload(invalid_protocol)
                self.assertEqual(
                    invalid_payload["error"]["code"], "LEMCP_E_INVALID_INPUT"
                )
                self.assertEqual(
                    invalid_payload["audit"],
                    {"simulated": True, "call_count": 0, "calls": []},
                )
                self.assertNotIn("do-not-leak", json.dumps(invalid_payload))
                self.assertEqual(fake.calls, [])
        implicit = await handler(None, CallToolRequestParams(name="search_documents", arguments=base))
        explicit = await handler(
            None,
            CallToolRequestParams(
                name="search_documents", arguments={**base, "mode": "bm25"}
            ),
        )
        self.assertFalse(implicit.is_error)
        self.assertEqual(_mcp_payload(implicit), _mcp_payload(explicit))
        self.assertEqual(fake.calls, [])

        enhanced = await handler(
            None,
            CallToolRequestParams(
                name="search_documents", arguments={**base, "mode": "enhanced"}
            ),
        )
        self.assertFalse(enhanced.is_error)
        self.assertEqual(_mcp_payload(enhanced)["audit"]["call_count"], 3)
        self.assertIs(_mcp_payload(enhanced)["audit"]["simulated"], True)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(len(TOOLS), 8)
        self.assertEqual(
            [tool.name for tool in TOOLS],
            [
                "search_documents",
                "get_excerpt",
                "get_multiple_excerpts",
                "get_document_metadata",
                "get_document_toc",
                "read_document_section",
                "find_in_document",
                "retrieval_status",
            ],
        )
        for tool in TOOLS:
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertEqual(
                tool.annotations.open_world_hint,
                tool.name == "search_documents",
            )

        failing = RecordingFake(self.query_vector, fail_role=CANDIDATE_RERANK)
        failure_handler = create_server(
            ReadOnlyEvidenceTools(
                self.application_root,
                enhanced_search=self._service(failing),
            )
        )._request_handlers["tools/call"].handler
        failure = await failure_handler(
            None,
            CallToolRequestParams(
                name="search_documents", arguments={**base, "mode": "enhanced"}
            ),
        )
        self.assertTrue(failure.is_error)
        failure_payload = _mcp_payload(failure)
        self.assertEqual(failure_payload["audit"]["call_count"], 3)
        self.assertIs(failure_payload["audit"]["simulated"], True)
        self.assertNotIn("transport-secret", json.dumps(failure_payload))
        self.assertNotIn(str(self.root), json.dumps(failure_payload))

        invalid = await failure_handler(
            None,
            CallToolRequestParams(
                name="search_documents",
                arguments={
                    "library_id": self.library_id,
                    "snapshot_id": self.snapshot_id,
                    "query": "",
                    "mode": "enhanced",
                },
            ),
        )
        self.assertTrue(invalid.is_error)
        self.assertEqual(_mcp_payload(invalid)["audit"]["call_count"], 0)
        self.assertIs(_mcp_payload(invalid)["audit"]["simulated"], True)


if __name__ == "__main__":
    unittest.main()
