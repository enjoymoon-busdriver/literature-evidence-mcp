from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literature_evidence_mcp.aliyun import (
    AliyunEmbedder, AliyunTransport, RERANK_MODEL, REWRITE_MODEL,
    aliyun_profile, enhanced_config,
)
from literature_evidence_mcp.enhanced import EnhancedSearchService
from literature_evidence_mcp.errors import EnhancedSearchError, VectorError
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.registry import LibraryRegistry
from literature_evidence_mcp.vectors import build_vectors, preview_vectors, verify_vectors
from tests.test_real_aliyun import FakeConnection, embedding_response


class RealVectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lemcp-real-vectors-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library_root = self.root / "library"
        self.library = FixedLibrary(self.library_root)
        self.files = []
        for index in range(12):
            path = self.root / f"synthetic-{index}.md"
            path.write_text(f"# Synthetic {index}\n\nUnique synthetic evidence number {index}.\n")
            self.files.append(path)
        self.first = self.library.build(self.files)
        FakeConnection.responses = []
        FakeConnection.requests = []
        self.patch = mock.patch("literature_evidence_mcp.aliyun.http.client.HTTPSConnection", FakeConnection)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_preview_batches_reuse_and_real_labels(self):
        before = {p.relative_to(self.library_root): p.read_bytes()
                  for p in self.library_root.rglob("*") if p.is_file()}
        plan = preview_vectors(self.library_root, self.first["snapshot_id"], aliyun_profile())
        self.assertEqual(plan["new_inputs"], 12)
        self.assertEqual(plan["planned_calls"], 2)
        after = {p.relative_to(self.library_root): p.read_bytes()
                 for p in self.library_root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(FakeConnection.requests, [])
        FakeConnection.responses = [(200, embedding_response(10)), (200, embedding_response(2))]
        key = mock.Mock(return_value="synthetic")
        result = build_vectors(self.library_root, self.first["snapshot_id"], aliyun_profile(), AliyunEmbedder(key))
        self.assertFalse(result["simulated"])
        self.assertEqual(result["generation"]["mode"], "real_provider")
        self.assertEqual(result["embedder_calls"], 2)
        self.assertEqual(result["provider_audit"]["call_count"], 2)
        verified = verify_vectors(self.library_root, self.first["snapshot_id"], result["profile_id"])
        self.assertFalse(verified["offline"])
        path = self.root / "new.md"
        path.write_text("# New synthetic\n\nOne additional synthetic piece.\n")
        second = self.library.build([path], base_snapshot_id=self.first["snapshot_id"])
        plan2 = preview_vectors(self.library_root, second["snapshot_id"], aliyun_profile())
        self.assertEqual((plan2["new_inputs"], plan2["reused_inputs"], plan2["planned_calls"]), (1, 12, 1))
        FakeConnection.responses = [(200, embedding_response(1))]
        result2 = build_vectors(self.library_root, second["snapshot_id"], aliyun_profile(), AliyunEmbedder(key))
        self.assertEqual(result2["statistics"]["reused_objects"], 12)
        repeated = build_vectors(self.library_root, second["snapshot_id"], aliyun_profile(), AliyunEmbedder(key))
        self.assertEqual(repeated["embedder_calls"], 0)
        self.assertEqual(key.call_count, 3)

    def test_second_batch_failure_does_not_publish_partial_vectors(self):
        FakeConnection.responses = [(200, embedding_response(10)), (429, {"message": "secret"})]
        with self.assertRaises(VectorError) as caught:
            build_vectors(self.library_root, self.first["snapshot_id"], aliyun_profile(), AliyunEmbedder(lambda: "synthetic"))
        self.assertEqual(caught.exception.provider_audit["call_count"], 2)
        self.assertEqual(len(FakeConnection.requests), 2)
        self.assertFalse((self.library_root / "derived" / "vectors" / "catalog.json").exists())
        self.assertNotIn("secret", str(caught.exception))
        plan = preview_vectors(self.library_root, self.first["snapshot_id"], aliyun_profile())
        self.assertEqual(plan["new_inputs"], 12)

    def test_real_adapter_whole_enhanced_chain_and_first_error_stop(self):
        record = LibraryRegistry(self.root / "app").create("Synthetic only")
        library_root = Path(record["library_root"])
        library = FixedLibrary(library_root, library_id=record["library_id"])
        snapshot = library.build(self.files[:2])
        FakeConnection.responses = [(200, embedding_response(2))]
        build_vectors(library_root, snapshot["snapshot_id"], aliyun_profile(), AliyunEmbedder(lambda: "synthetic"))
        transport = AliyunTransport(lambda: "synthetic")
        service = EnhancedSearchService(enhanced_config(), transport)
        FakeConnection.responses = [
            (200, {"model": REWRITE_MODEL, "choices": [{"message": {"content": '{"query":"synthetic evidence"}'}}]}),
            (200, embedding_response(1)),
            (200, {"model": RERANK_MODEL, "results": [{"index": 0, "relevance_score": .9}]}),
        ]
        result = service.search(library, record["library_id"], snapshot["snapshot_id"], "evidence")
        self.assertTrue(result["found"])
        self.assertFalse(result["audit"]["simulated"])
        self.assertEqual(result["audit"]["call_count"], 3)
        FakeConnection.requests = []
        FakeConnection.responses = [(429, {})]
        with self.assertRaises(EnhancedSearchError) as caught:
            service.search(library, record["library_id"], snapshot["snapshot_id"], "evidence")
        self.assertEqual(caught.exception.audit["call_count"], 1)
        self.assertEqual(len(FakeConnection.requests), 1)


if __name__ == "__main__":
    unittest.main()
