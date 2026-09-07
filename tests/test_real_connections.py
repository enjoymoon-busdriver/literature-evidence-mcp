from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from literature_evidence_mcp.aliyun import DIMENSIONS, EMBEDDING_MODEL
from literature_evidence_mcp.connections import Connections
from literature_evidence_mcp.credentials import CredentialError
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.web import create_app


PORT = 19136  # TestClient runs in process and does not bind this port.
ORIGIN = f"http://127.0.0.1:{PORT}"
SYNTHETIC_KEY = "SYNTHETIC_CONNECTION_KEY_NEVER_REAL"
TUNNEL_ID = "tunnel_" + "1" * 32


class RealConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lemcp-connections-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.application_root = self.root / "application"
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.root / "fake-home")}),
            mock.patch(
                "literature_evidence_mcp.credentials._SecurityBridge",
                side_effect=AssertionError("No real Keychain in tests"),
            ),
            mock.patch("socket.create_connection", side_effect=AssertionError("No network in tests")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.credentials = mock.Mock()
        self.credentials.get.return_value = SYNTHETIC_KEY
        self.tunnel = mock.Mock()
        self.tunnel.status.return_value = {"state": "stopped", "passed": True}
        self.tunnel.start.return_value = {"state": "starting", "passed": True}
        self.tunnel.health.return_value = {"state": "running", "passed": True}
        self.tunnel.stop.return_value = {"state": "stopped", "passed": True}
        self.connections = Connections(
            self.application_root, credentials=self.credentials, tunnel=self.tunnel
        )
        self.http = mock.Mock()
        self.responses: list[tuple[int, dict]] = []

        def getresponse():
            status, body = self.responses.pop(0)
            response = mock.Mock(status=status)
            response.read.return_value = json.dumps(body).encode("utf-8")
            return response

        self.http.getresponse.side_effect = getresponse
        patch = mock.patch(
            "literature_evidence_mcp.aliyun.http.client.HTTPSConnection",
            return_value=self.http,
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.client = TestClient(
            create_app(self.application_root, port=PORT, connections=self.connections),
            base_url=ORIGIN,
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        status = self.client.get("/api/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.csrf = status.json()["csrf_token"]

    def headers(self, intent: str) -> dict[str, str]:
        return {
            "Origin": ORIGIN,
            "X-CSRF-Token": self.csrf,
            "X-Action-Intent": intent,
        }

    def post(self, path: str, intent: str, body: dict | None = None):
        arguments = {} if body is None else {"json": body}
        return self.client.post(path, headers=self.headers(intent), **arguments)

    def create_snapshot(self) -> tuple[str, str, FixedLibrary]:
        record = self.connections.registry.create("合成连接测试库")
        library_id = record["library_id"]
        library = FixedLibrary(
            self.connections.registry.library_path(library_id), library_id=library_id
        )
        source = self.root / "synthetic.md"
        source.write_text("# Synthetic\n\nsynthetic_marker local evidence.\n", encoding="utf-8")
        built = library.build([source])
        return library_id, built["snapshot_id"], library

    def save_key(self, kind: str = "aliyun"):
        response = self.post(
            "/api/connections/credentials", "save-credential",
            {"kind": kind, "key": SYNTHETIC_KEY},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def assert_secret_absent(self, response) -> None:
        self.assertNotIn(SYNTHETIC_KEY, response.text)
        self.assertNotIn("Authorization", response.text)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SYNTHETIC_KEY.encode(), path.read_bytes(), str(path))

    def prepare_vectors(self, library_id: str, snapshot_id: str) -> None:
        self.save_key()
        self.responses.append((200, {"model": EMBEDDING_MODEL, "data": [
            {"index": 0, "embedding": [1.0] + [0.0] * (DIMENSIONS - 1)}
        ]}))
        result = self.post(
            f"/api/libraries/{library_id}/vectors/build", "vectors-build",
            {"snapshot_id": snapshot_id},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.credentials.get.reset_mock()
        self.http.reset_mock()

    def test_construct_status_and_default_bm25_do_not_read_keys(self) -> None:
        self.assertFalse(self.application_root.exists())
        self.assertFalse(self.connections.status()["aliyun_configured"])
        library_id, snapshot_id, _library = self.create_snapshot()
        with mock.patch.object(
            self.connections, "settings", side_effect=AssertionError("BM25 must not load settings")
        ), mock.patch(
            "literature_evidence_mcp.aliyun.enhanced_config",
            side_effect=AssertionError("BM25 must not load model configuration"),
        ):
            result = self.post(
                f"/api/libraries/{library_id}/search", "search",
                {"snapshot_id": snapshot_id, "query": "synthetic_marker"},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["results"])
        self.credentials.get.assert_not_called()
        self.credentials.set.assert_not_called()
        self.http.request.assert_not_called()

    def test_saving_key_only_calls_store_set_and_never_returns_or_writes_secret(self) -> None:
        for kind in ("aliyun", "openai_tunnel"):
            response = self.save_key(kind)
            self.assertTrue(response.json()["connections"][kind + "_configured"])
            self.assert_secret_absent(response)
        self.assertEqual(self.credentials.set.call_args_list, [
            mock.call("aliyun", SYNTHETIC_KEY), mock.call("openai_tunnel", SYNTHETIC_KEY)
        ])
        self.credentials.get.assert_not_called()
        self.http.request.assert_not_called()
        self.assert_secret_absent(self.client.get("/api/status"))

    def test_failed_save_does_not_mark_credentials_configured(self) -> None:
        self.credentials.set.side_effect = CredentialError("denied")
        response = self.post(
            "/api/connections/credentials", "save-credential",
            {"kind": "aliyun", "key": SYNTHETIC_KEY},
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertFalse(self.connections.settings()["aliyun_configured"])
        self.assertFalse((self.application_root / "connections.json").exists())
        self.assert_secret_absent(response)
        self.credentials.get.assert_not_called()

    def test_all_connection_actions_require_origin_csrf_and_correct_intent(self) -> None:
        routes = (
            ("/api/connections/credentials", "save-credential", {"kind": "aliyun", "key": SYNTHETIC_KEY}),
            ("/api/connections/tunnel", "save-tunnel-settings", {"tunnel_id": TUNNEL_ID, "accept_backoff": True}),
            ("/api/tunnel/production-start", "tunnel-production-start", None),
            ("/api/tunnel/production/health", "tunnel-production-health", None),
            ("/api/tunnel/production/stop", "tunnel-production-stop", None),
            ("/api/libraries/lib_synthetic/vectors/preview", "vectors-preview", {"snapshot_id": "synthetic"}),
            ("/api/libraries/lib_synthetic/vectors/build", "vectors-build", {"snapshot_id": "synthetic"}),
        )
        for path, intent, body in routes:
            for header, wrong, expected in (
                ("Origin", "https://untrusted.invalid", 403),
                ("X-CSRF-Token", "incorrect", 403),
                ("X-Action-Intent", "unrelated-action", 400),
            ):
                with self.subTest(path=path, header=header):
                    headers = {**self.headers(intent), header: wrong}
                    arguments = {} if body is None else {"json": body}
                    response = self.client.post(path, headers=headers, **arguments)
                    self.assertEqual(response.status_code, expected, response.text)
        self.credentials.set.assert_not_called()
        self.credentials.get.assert_not_called()
        self.tunnel.start.assert_not_called()
        self.tunnel.health.assert_not_called()
        self.tunnel.stop.assert_not_called()
        self.http.request.assert_not_called()
        self.assertFalse(self.application_root.exists())

    def test_vector_preview_is_read_only_and_requires_no_key(self) -> None:
        library_id, snapshot_id, _library = self.create_snapshot()
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        response = self.post(
            f"/api/libraries/{library_id}/vectors/preview", "vectors-preview",
            {"snapshot_id": snapshot_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["vectors"]["planned_calls"], 1)
        self.assertEqual(response.json()["vectors"]["new_inputs"], 1)
        after = {path.relative_to(self.root): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.credentials.get.assert_not_called()
        self.http.request.assert_not_called()

    def test_vector_failure_reports_actual_calls_without_publishing(self) -> None:
        library_id, snapshot_id, library = self.create_snapshot()
        self.save_key()
        self.responses.append((429, {"message": SYNTHETIC_KEY}))
        response = self.post(
            f"/api/libraries/{library_id}/vectors/build", "vectors-build",
            {"snapshot_id": snapshot_id},
        )
        self.assertGreaterEqual(response.status_code, 400, response.text)
        audit = response.json()["provider_audit"]
        self.assertEqual(audit["call_count"], 1)
        self.assertEqual(audit["calls"][0]["status"], "failed")
        self.assertFalse(response.json().get("published", False))
        self.assertEqual(self.connections.preview_vectors(library_id, snapshot_id)["new_inputs"], 1)
        self.assertTrue(library.verify(snapshot_id))
        self.http.request.assert_called_once()
        self.credentials.get.assert_called_once_with("aliyun")
        self.assert_secret_absent(response)

        self.credentials.get.reset_mock()
        self.credentials.get.side_effect = CredentialError("denied")
        self.http.reset_mock()
        blocked = self.post(
            f"/api/libraries/{library_id}/vectors/build", "vectors-build",
            {"snapshot_id": snapshot_id},
        )
        self.assertGreaterEqual(blocked.status_code, 400, blocked.text)
        self.assertEqual(blocked.json()["provider_audit"]["call_count"], 0)
        self.http.request.assert_not_called()

    def test_model_failure_and_key_denial_report_one_and_zero_calls(self) -> None:
        library_id, snapshot_id, _library = self.create_snapshot()
        self.prepare_vectors(library_id, snapshot_id)
        self.responses.append((500, {"message": SYNTHETIC_KEY}))
        response = self.post(
            f"/api/libraries/{library_id}/search", "search",
            {"snapshot_id": snapshot_id, "query": "synthetic_marker", "mode": "enhanced"},
        )
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["audit"]["call_count"], 1)
        self.assertNotIn("results", response.json())
        self.http.request.assert_called_once()
        self.credentials.get.assert_called_once_with("aliyun")
        self.assert_secret_absent(response)

        self.credentials.get.side_effect = CredentialError("denied")
        self.http.reset_mock()
        blocked = self.post(
            f"/api/libraries/{library_id}/search", "search",
            {"snapshot_id": snapshot_id, "query": "synthetic_marker", "mode": "enhanced"},
        )
        self.assertGreaterEqual(blocked.status_code, 400, blocked.text)
        self.assertEqual(blocked.json()["audit"]["call_count"], 0)
        self.assertEqual(blocked.json()["audit"]["calls"], [])
        self.http.request.assert_not_called()

    def test_tunnel_starts_only_after_saved_identity_key_and_backoff_acceptance(self) -> None:
        missing = self.post("/api/tunnel/production-start", "tunnel-production-start")
        self.assertEqual(missing.status_code, 503, missing.text)
        self.save_key("openai_tunnel")
        settings = self.post(
            "/api/connections/tunnel", "save-tunnel-settings",
            {"tunnel_id": TUNNEL_ID, "accept_backoff": False},
        )
        self.assertEqual(settings.status_code, 200, settings.text)
        blocked = self.post("/api/tunnel/production-start", "tunnel-production-start")
        self.assertEqual(blocked.status_code, 503, blocked.text)
        self.tunnel.start.assert_not_called()
        accepted = self.post(
            "/api/connections/tunnel", "save-tunnel-settings",
            {"tunnel_id": TUNNEL_ID, "accept_backoff": True},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        started = self.post("/api/tunnel/production-start", "tunnel-production-start")
        self.assertEqual(started.status_code, 200, started.text)
        self.tunnel.start.assert_called_once_with(TUNNEL_ID)
        self.assertTrue(started.json()["tunnel"]["passed"])
        self.tunnel.health.return_value = {"state": "unknown", "passed": False}
        failed = self.post("/api/tunnel/production/health", "tunnel-production-health")
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertFalse(failed.json()["tunnel"]["passed"])

    def test_production_mcp_default_bm25_does_not_load_model_settings_or_keys(self) -> None:
        from literature_evidence_mcp import mcp_server

        library_id, snapshot_id, _library = self.create_snapshot()
        captured = {}

        def create_server(service):
            captured["service"] = service
            return object()

        async def serve(_server):
            result = captured["service"].search_documents(
                library_id, snapshot_id, "synthetic_marker"
            )
            self.assertTrue(result["results"])

        with mock.patch("literature_evidence_mcp.credentials.MacOSKeychain", return_value=self.credentials), mock.patch(
            "literature_evidence_mcp.real_tunnel.RealTunnel", return_value=self.tunnel
        ), mock.patch.object(Connections, "settings", side_effect=AssertionError("No model settings for BM25")), mock.patch(
            "literature_evidence_mcp.aliyun.enhanced_config", side_effect=AssertionError("No model config for BM25")
        ), mock.patch.object(mcp_server, "create_server", side_effect=create_server), mock.patch.object(
            mcp_server, "_serve_stdio", side_effect=serve
        ):
            code = mcp_server.main(["--application-root", str(self.application_root)])
        self.assertEqual(code, 0)
        self.credentials.get.assert_not_called()
        self.http.request.assert_not_called()

    def test_app_shutdown_closes_its_tunnel(self) -> None:
        tunnel = mock.Mock()
        connections = Connections(self.root / "shutdown-application", credentials=self.credentials, tunnel=tunnel)
        app = create_app(connections.root, port=PORT + 1, connections=connections)
        with TestClient(app, base_url=f"http://127.0.0.1:{PORT + 1}"):
            tunnel.close.assert_not_called()
        tunnel.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
