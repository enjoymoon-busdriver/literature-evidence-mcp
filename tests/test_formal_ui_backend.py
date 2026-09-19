from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from literature_evidence_mcp.aliyun import AliyunError
from literature_evidence_mcp.connections import Connections, MODEL_ROLES
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.web import create_app


PORT = 19148
ORIGIN = f"http://127.0.0.1:{PORT}"


class FakeTransport:
    simulated = True

    def __init__(self, key_loader, config, calls, fail_role=None):
        self.key_loader = key_loader
        self.config = config
        self.calls = calls
        self.fail_role = fail_role
        self.last_audit = {"call_count": 0, "calls": []}

    def invoke(self, *, role, provider, model_id, payload):
        self.key_loader()
        item = {"role": role, "model_id": model_id, "status": "succeeded"}
        self.last_audit = {"call_count": 1, "calls": [item]}
        self.calls.append((role, model_id, payload))
        if role == self.fail_role:
            item.update(status="failed", http_status=429)
            raise AliyunError("阿里云请求失败（HTTP 429）；未重试。")
        return object()


class FakeEmbedder:
    simulated = False
    offline = False
    batch_size = 10
    adapter_identity = {"implementation": "formal_ui_fake_provider", "version": 1}

    def __init__(self, key_loader, config, calls):
        self.key_loader = key_loader
        self.config = config
        self.calls = calls
        self.last_audit = {"call_count": 0, "calls": []}

    def __call__(self, texts, profile):
        self.key_loader()
        self.calls.append(profile["model_id"])
        self.last_audit = {"call_count": 1, "calls": [{
            "role": "document_embedding",
            "model_id": self.config.vector_recall_model_id,
            "status": "succeeded",
        }]}
        return [[1.0] + [0.0] * 1023 for _text in texts]


class FormalUiBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="foliohook-formal-backend-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.credentials = mock.Mock()
        self.credentials.get.return_value = "SYNTHETIC_NOT_REAL"
        self.tunnel = mock.Mock()
        self.tunnel.status.return_value = {"state": "stopped", "passed": True}
        self.provider_calls = []
        self.fail_role = None

        def transport_factory(key_loader, config):
            return FakeTransport(
                key_loader, config, self.provider_calls, self.fail_role
            )

        self.connections = Connections(
            self.root / "application",
            credentials=self.credentials,
            tunnel=self.tunnel,
            transport_factory=transport_factory,
        )
        self.client = TestClient(
            create_app(
                self.connections.root,
                port=PORT,
                connections=self.connections,
            ),
            base_url=ORIGIN,
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf_token"]

    def post(self, path: str, intent: str, body=None):
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": self.csrf,
            "X-Action-Intent": intent,
        }
        kwargs = {} if body is None else {"json": body}
        return self.client.post(path, headers=headers, **kwargs)

    def snapshot(self):
        record = self.connections.registry.create("原名称", description="原描述")
        library_id = record["library_id"]
        source = self.root / "synthetic.md"
        source.write_text("# 第一节\n\nformal_ui_unique evidence.\n", encoding="utf-8")
        library = FixedLibrary(
            self.connections.registry.library_path(library_id), library_id=library_id
        )
        built = library.build([source])
        return library_id, built["snapshot_id"]

    def test_document_list_detail_and_section_keep_exact_ids(self) -> None:
        library_id, snapshot_id = self.snapshot()
        base = f"/api/libraries/{library_id}/snapshots/{snapshot_id}/documents"
        listed = self.client.get(base)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["library_id"], library_id)
        self.assertEqual(listed.json()["snapshot_id"], snapshot_id)
        document = listed.json()["documents"][0]
        self.assertNotIn("path", str(document).lower())

        detail = self.client.get(f"{base}/{document['document_id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["document"]["document_id"], document["document_id"])
        section_id = detail.json()["toc"]["items"][0]["section_id"]
        evidence = self.client.get(
            f"{base}/{document['document_id']}/sections/{section_id}"
        )
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertEqual(evidence.json()["library_id"], library_id)
        self.assertEqual(evidence.json()["snapshot_id"], snapshot_id)
        self.assertIn("formal_ui_unique", evidence.text)

    def test_library_metadata_actions_reuse_registry_writes(self) -> None:
        library_id, _snapshot_id = self.snapshot()
        renamed = self.post(
            f"/api/libraries/{library_id}/rename",
            "rename-library",
            {"name": "新名称"},
        )
        described = self.post(
            f"/api/libraries/{library_id}/description",
            "update-library-description",
            {"description": "新描述"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(described.status_code, 200, described.text)
        self.assertEqual(described.json()["library"]["name"], "新名称")
        self.assertEqual(described.json()["library"]["description"], "新描述")

    def test_new_write_and_check_routes_require_their_exact_intents(self) -> None:
        library_id, _snapshot_id = self.snapshot()
        recommended = self.connections.model_settings()["recommended_model_ids"]
        requests = (
            (f"/api/libraries/{library_id}/rename", {"name": "不应写入"}),
            (f"/api/libraries/{library_id}/description", {"description": "不应写入"}),
            ("/api/connections/models", {"enabled": True, "model_ids": recommended}),
            ("/api/connections/models/recommended", None),
            ("/api/connections/models/check", None),
        )
        for path, body in requests:
            with self.subTest(path=path):
                response = self.post(path, "wrong-intent", body)
                self.assertEqual(response.status_code, 400, response.text)
        library = self.connections.registry.list_libraries()[0]
        self.assertEqual((library["name"], library["description"]), ("原名称", "原描述"))
        self.assertFalse(self.connections.model_settings()["enabled"])
        self.assertEqual(self.provider_calls, [])
        self.credentials.get.assert_not_called()

    def test_model_settings_restore_and_explicit_classified_checks(self) -> None:
        initial = self.client.get("/api/status").json()
        self.assertFalse(initial["connections"]["model_settings"]["enabled"])
        self.assertFalse(initial["enhanced_available"])
        self.credentials.get.assert_not_called()

        saved_key = self.post(
            "/api/connections/credentials",
            "save-credential",
            {"kind": "aliyun", "key": "SYNTHETIC_NOT_REAL"},
        )
        self.assertEqual(saved_key.status_code, 200, saved_key.text)
        self.assertFalse(saved_key.json()["connections"]["model_settings"]["enabled"])
        custom = {role: f"custom-{role}" for role in MODEL_ROLES}
        saved = self.post(
            "/api/connections/models",
            "save-model-settings",
            {"enabled": True, "model_ids": custom},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["connections"]["model_settings"]["model_ids"], custom)

        settings_path = self.connections.root / "connections.json"
        before_invalid = settings_path.read_bytes()
        invalid = self.post(
            "/api/connections/models",
            "save-model-settings",
            {"enabled": True, "model_ids": {**custom, "unexpected": "model"}},
        )
        self.assertEqual(invalid.status_code, 503, invalid.text)
        self.assertEqual(settings_path.read_bytes(), before_invalid)

        self.credentials.reset_mock()
        restored = self.post(
            "/api/connections/models/recommended", "restore-recommended-models"
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertTrue(restored.json()["connections"]["aliyun_configured"])
        self.assertTrue(restored.json()["connections"]["model_settings"]["enabled"])
        self.credentials.set.assert_not_called()
        self.credentials.get.assert_not_called()

        self.fail_role = "vector_recall"
        checked = self.post(
            "/api/connections/models/check", "check-model-connections"
        )
        self.assertEqual(checked.status_code, 200, checked.text)
        results = checked.json()["connection_check"]["results"]
        self.assertEqual([item["role"] for item in results], list(MODEL_ROLES))
        self.assertEqual(results[1]["classification"], "rate_limited")
        self.assertEqual(len(self.provider_calls), 3)
        self.assertEqual(self.credentials.get.call_count, 3)
        self.assertNotIn("SYNTHETIC_NOT_REAL", checked.text)

    def test_legacy_settings_are_read_without_writing_a_migration(self) -> None:
        application_root = self.root / "legacy-application"
        application_root.mkdir()
        legacy = {
            "version": 1,
            "aliyun_configured": True,
            "openai_tunnel_configured": False,
            "tunnel_id": "",
            "tunnel_backoff_accepted": False,
        }
        path = application_root / "connections.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        before = path.read_bytes()
        runtime = Connections(
            application_root, credentials=self.credentials, tunnel=self.tunnel
        )
        settings = runtime.settings()
        self.assertEqual(settings["version"], 2)
        self.assertFalse(settings["models_enabled"])
        self.assertEqual(path.read_bytes(), before)
        self.credentials.get.assert_not_called()

    def test_vector_model_change_uses_exact_profile_without_auto_build(self) -> None:
        calls = []

        def embedder_factory(key_loader, config):
            return FakeEmbedder(key_loader, config, calls)

        runtime = Connections(
            self.root / "vector-application",
            credentials=self.credentials,
            tunnel=self.tunnel,
            embedder_factory=embedder_factory,
        )
        record = runtime.registry.create("向量配置测试")
        library_id = record["library_id"]
        source = self.root / "vector.md"
        source.write_text("# Vector\n\nexact profile evidence.\n", encoding="utf-8")
        library = FixedLibrary(
            runtime.registry.library_path(library_id), library_id=library_id
        )
        snapshot_id = library.build([source])["snapshot_id"]
        runtime.save_key("aliyun", "SYNTHETIC_NOT_REAL")
        first_ids = {
            "query_rewrite": "custom-rewrite",
            "vector_recall": "custom-vector-one",
            "candidate_rerank": "custom-rerank",
        }
        runtime.save_models(True, first_ids)
        built = runtime.build_vectors(library_id, snapshot_id)
        self.assertFalse(built["already_exists"])
        self.assertEqual(calls, ["custom-vector-one"])

        second_ids = {**first_ids, "vector_recall": "custom-vector-two"}
        runtime.save_models(True, second_ids)
        self.credentials.get.reset_mock()
        preview = runtime.preview_vectors(library_id, snapshot_id)
        self.assertFalse(preview["already_exists"])
        self.assertEqual(preview["planned_calls"], 1)
        self.assertEqual(calls, ["custom-vector-one"])
        self.credentials.get.assert_not_called()
        summary = runtime.enhanced.public_summary()
        self.assertEqual(
            [item["model_id"] for item in summary["roles"]],
            ["custom-rewrite", "custom-vector-two", "custom-rerank"],
        )


if __name__ == "__main__":
    unittest.main()
