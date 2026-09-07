from __future__ import annotations

import json
import unittest
from unittest import mock

from literature_evidence_mcp.aliyun import (
    AliyunEmbedder, AliyunError, AliyunTransport, DIMENSIONS, EMBEDDING_MODEL,
    HOST, PROVIDER, RERANK_MODEL, REWRITE_MODEL, aliyun_profile,
)
from literature_evidence_mcp.enhanced import CANDIDATE_RERANK, QUERY_REWRITE, VECTOR_RECALL


def embedding_response(count):
    return {"model": EMBEDDING_MODEL, "data": [
        {"index": index, "embedding": [1.0] + [0.0] * (DIMENSIONS - 1)}
        for index in reversed(range(count))], "usage": {"total_tokens": 12}}


class FakeConnection:
    responses = []
    requests = []

    def __init__(self, host, timeout):
        self.host, self.timeout = host, timeout

    def request(self, method, path, body, headers):
        self.requests.append({"host": self.host, "path": path, "method": method,
                              "body": json.loads(body), "headers": headers})

    def getresponse(self):
        status, value = self.responses.pop(0)
        response = mock.Mock(status=status)
        response.read.return_value = json.dumps(value).encode()
        return response

    def close(self):
        pass


class RealAliyunTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.responses = []
        FakeConnection.requests = []
        self.patch = mock.patch("literature_evidence_mcp.aliyun.http.client.HTTPSConnection", FakeConnection)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_three_roles_exact_requests_and_index_mapping(self):
        key = mock.Mock(return_value="synthetic-test-key")
        transport = AliyunTransport(key)
        self.assertEqual(key.call_count, 0)
        FakeConnection.responses = [
            (200, {"model": REWRITE_MODEL, "choices": [{"message": {
                "content": '{"query":"synthetic question"}'}}], "usage": {"prompt_tokens": 9}}),
            (200, embedding_response(1)),
            (200, {"model": RERANK_MODEL, "results": [
                {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .2}],
                "usage": {"total_tokens": 15}}),
        ]
        def invoke(role, model, payload):
            return transport.invoke(role=role, provider=PROVIDER, model_id=model, payload=payload)
        self.assertEqual(invoke(QUERY_REWRITE, REWRITE_MODEL, {"query": "original"}), "synthetic question")
        self.assertEqual(len(invoke(VECTOR_RECALL, EMBEDDING_MODEL, {"query": "synthetic question"})), 1024)
        rows = invoke(CANDIDATE_RERANK, RERANK_MODEL, {"query": "synthetic question", "candidates": [
            {"chunk_id": "a", "text": "first synthetic"}, {"chunk_id": "b", "text": "second synthetic"}]})
        self.assertEqual(rows[0], {"chunk_id": "b", "score": .9})
        self.assertEqual(key.call_count, 3)
        self.assertEqual([r["host"] for r in FakeConnection.requests], [HOST] * 3)
        self.assertEqual(FakeConnection.requests[0]["body"]["reasoning_effort"], "none")
        self.assertEqual(FakeConnection.requests[1]["body"]["dimensions"], 1024)
        self.assertEqual(FakeConnection.requests[2]["path"], "/compatible-api/v1/reranks")
        self.assertEqual(FakeConnection.requests[2]["body"]["top_n"], 2)
        audit = json.dumps(transport.last_audit)
        self.assertNotIn("synthetic", audit)
        self.assertNotIn(HOST, audit)
        self.assertTrue(all(c["status"] == "succeeded" for c in transport.last_audit["calls"]))

    def test_redirect_rate_limit_and_errors_stop_without_retry_and_redact(self):
        for status in (302, 401, 429, 500):
            with self.subTest(status=status):
                FakeConnection.requests = []
                FakeConnection.responses = [(status, {"message": "secret-request-content"})]
                embedder = AliyunEmbedder(lambda: "synthetic-test-key")
                with self.assertRaises(AliyunError) as caught:
                    embedder(["private synthetic"], aliyun_profile())
                self.assertEqual(len(FakeConnection.requests), 1)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(embedder.last_audit["call_count"], 1)

    def test_bad_dimensions_duplicate_indices_and_profile_fail_closed(self):
        for mutate in (lambda v: v["data"][0].update(embedding=[1.]),
                       lambda v: v["data"][0].update(index=True),
                       lambda v: v.update(model="different")):
            value = embedding_response(1)
            mutate(value)
            FakeConnection.responses = [(200, value)]
            with self.assertRaises(AliyunError):
                AliyunEmbedder(lambda: "synthetic")( ["text"], aliyun_profile())
        key = mock.Mock(return_value="synthetic")
        profile = aliyun_profile()
        profile["dimensions"] = 768
        with self.assertRaises(AliyunError):
            AliyunEmbedder(key)(["text"], profile)
        key.assert_not_called()

    def test_key_failure_has_no_network_call(self):
        def missing():
            raise RuntimeError("secret-path")
        embedder = AliyunEmbedder(missing)
        with self.assertRaises(AliyunError) as caught:
            embedder(["text"], aliyun_profile())
        self.assertNotIn("secret-path", str(caught.exception))
        self.assertEqual(FakeConnection.requests, [])
        self.assertEqual(embedder.last_audit["call_count"], 0)


if __name__ == "__main__":
    unittest.main()
