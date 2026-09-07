from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from literature_evidence_mcp.tunnel_wizard import (
    OfflineTunnelAdapter,
    TunnelSimulation,
    production_boundary_report,
    tunnel_wizard_guide,
)
from literature_evidence_mcp.web import create_app


PORT = 19129
ORIGIN = f"http://127.0.0.1:{PORT}"


class RecordingOfflineAdapter:
    simulated = True

    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[str] = []
        self.running = False

    def start(self) -> None:
        self.calls.append("start")
        self.running = True
        if self.fail_at == "start":
            raise RuntimeError("SYNTHETIC_SECRET /private/tmp/stage9-start")

    def health(self) -> bool:
        self.calls.append("health")
        if self.fail_at == "health":
            raise RuntimeError("SYNTHETIC_SECRET /private/tmp/stage9-health")
        return self.running

    def stop(self) -> None:
        self.calls.append("stop")
        if self.fail_at == "stop":
            raise RuntimeError("SYNTHETIC_SECRET /private/tmp/stage9-stop")
        self.running = False


class StageNineTunnelStateTests(unittest.TestCase):
    def test_guide_is_a_nonsecret_demo_and_production_is_unavailable(self) -> None:
        guide = tunnel_wizard_guide()
        production = production_boundary_report()

        self.assertEqual(guide["stage"], 9)
        self.assertEqual(guide["mode"], "offline_simulation_only")
        self.assertEqual(guide["demo_profile"]["kind"], "演示配置模板")
        self.assertFalse(guide["demo_profile"]["tunnel_id_configured"])
        self.assertIn("未来", guide["demo_profile"]["tunnel_id_template"])
        self.assertFalse(guide["runtime_api_key_collected"])
        self.assertEqual(production["state"], "real-approval-required")
        self.assertFalse(production["available"])
        self.assertFalse(production["real_connected"])
        self.assertEqual(production["network_calls"], 0)
        rendered = json.dumps(guide, ensure_ascii=False)
        self.assertIn("Read + Manage", rendered)
        self.assertIn("Read + Use", rendered)
        self.assertIn("ChatGPT 工作区", rendered)
        self.assertNotIn("OPENAI_API_KEY", rendered)
        self.assertNotIn("/Users/", rendered)

    def test_explicit_start_health_stop_never_reports_real_connection(self) -> None:
        adapter = RecordingOfflineAdapter()
        tunnel = TunnelSimulation(adapter)

        initial = tunnel.snapshot()
        self.assertEqual(initial["state"], "模拟停止")

        status, not_running = tunnel.health()
        self.assertEqual(status, 409)
        self.assertEqual(not_running["state"], "模拟停止")

        status, started = tunnel.start()
        self.assertEqual(status, 200)
        self.assertEqual(started["state"], "模拟运行")

        status, healthy = tunnel.health()
        self.assertEqual(status, 200)
        self.assertEqual(healthy["state"], "模拟通过")
        self.assertTrue(healthy["local_health_checked"])
        self.assertFalse(healthy["chatgpt_tool_discovery_checked"])

        status, stopped = tunnel.stop()
        self.assertEqual(status, 200)
        self.assertEqual(stopped["state"], "模拟停止")
        self.assertEqual(adapter.calls, ["start", "health", "stop"])
        for report in (initial, not_running, started, healthy, stopped):
            self.assertTrue(report["simulated"])
            self.assertFalse(report["real_connected"])
            self.assertEqual(report["network_calls"], 0)
            self.assertEqual(report["model_calls"], 0)
            self.assertEqual(report["api_keys_collected"], 0)
            self.assertEqual(report["external_config_writes"], 0)
            self.assertEqual(report["retry_count"], 0)

    def test_first_adapter_error_is_redacted_and_cleanup_stops(self) -> None:
        for fail_at, expected_calls, expected_state in (
            ("start", ["start", "stop"], "模拟停止"),
            ("health", ["start", "health", "stop"], "模拟停止"),
            ("stop", ["start", "stop"], "模拟状态未知"),
        ):
            with self.subTest(fail_at=fail_at):
                adapter = RecordingOfflineAdapter(fail_at)
                tunnel = TunnelSimulation(adapter)
                if fail_at != "start":
                    self.assertEqual(tunnel.start()[0], 200)
                status, report = getattr(tunnel, fail_at)()
                self.assertEqual(status, 503)
                self.assertFalse(report["passed"])
                self.assertEqual(report["state"], expected_state)
                self.assertEqual(report["retry_count"], 0)
                self.assertEqual(adapter.calls, expected_calls)
                self.assertEqual(tunnel.snapshot()["passed"], expected_state != "模拟状态未知")
                self.assertEqual(adapter.running, expected_state == "模拟状态未知")
                rendered = json.dumps(report, ensure_ascii=False)
                self.assertNotIn("SYNTHETIC_SECRET", rendered)
                self.assertNotIn("/private/tmp", rendered)
                self.assertNotIn("Traceback", rendered)

    def test_failed_cleanup_does_not_claim_the_fake_was_stopped(self) -> None:
        class StartAndCleanupFailure(RecordingOfflineAdapter):
            def start(self) -> None:
                self.calls.append("start")
                self.running = True
                raise RuntimeError("first secret")

            def stop(self) -> None:
                self.calls.append("stop")
                raise RuntimeError("cleanup secret")

        adapter = StartAndCleanupFailure()
        simulation = TunnelSimulation(adapter)
        status, report = simulation.start()
        self.assertEqual(status, 503)
        self.assertEqual(adapter.calls, ["start", "stop"])
        self.assertEqual(report["state"], "模拟状态未知")
        self.assertIsNone(report["simulated_running"])
        self.assertFalse(report["passed"])
        self.assertFalse(simulation.snapshot()["passed"])
        self.assertTrue(adapter.running)
        self.assertNotIn("secret", json.dumps(report, ensure_ascii=False))

    def test_failed_health_cleanup_is_unknown_and_does_not_retry(self) -> None:
        adapter = RecordingOfflineAdapter("stop")
        simulation = TunnelSimulation(adapter)
        simulation.start()
        with mock.patch.object(adapter, "health", return_value=False) as health:
            status, report = simulation.health()
        self.assertEqual(status, 503)
        health.assert_called_once_with()
        self.assertEqual(adapter.calls, ["start", "stop"])
        self.assertTrue(adapter.running)
        self.assertEqual(report["state"], "模拟状态未知")
        self.assertFalse(simulation.snapshot()["passed"])


class StageNineTunnelWebTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lemcp-stage9-web-")
        self.addCleanup(temporary.cleanup)
        self.application_root = Path(temporary.name) / "app"
        app = create_app(self.application_root, port=PORT)
        self.client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        status = self.client.get("/api/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.status = status.json()
        self.csrf = self.status["csrf_token"]

    def _headers(self, intent: str) -> dict[str, str]:
        return {
            "Origin": ORIGIN,
            "X-CSRF-Token": self.csrf,
            "X-Action-Intent": intent,
        }

    def _post(self, action: str):
        return self.client.post(
            f"/api/tunnel/simulated/{action}",
            headers=self._headers(f"tunnel-simulated-{action}"),
        )

    def test_status_and_simulated_flow_are_session_isolated(self) -> None:
        wizard = self.status["tunnel_wizard"]
        self.assertEqual(wizard["simulation"]["state"], "模拟停止")
        self.assertEqual(wizard["guide"], tunnel_wizard_guide())

        started = self._post("start")
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["tunnel"]["state"], "模拟运行")
        healthy = self._post("health")
        self.assertEqual(healthy.status_code, 200, healthy.text)
        self.assertEqual(healthy.json()["tunnel"]["state"], "模拟通过")

        with TestClient(
            self.client.app,
            base_url=ORIGIN,
            raise_server_exceptions=False,
        ) as other:
            other_status = other.get("/api/status").json()
            self.assertEqual(
                other_status["tunnel_wizard"]["simulation"]["state"],
                "模拟停止",
            )

        stopped = self._post("stop")
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json()["tunnel"]["state"], "模拟停止")
        self.assertFalse(stopped.json()["tunnel"]["real_connected"])

    def test_parameter_intent_and_production_boundaries_fail_closed(self) -> None:
        missing_intent = self.client.post(
            "/api/tunnel/simulated/start",
            headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(missing_intent.status_code, 400)

        injected = self.client.post(
            "/api/tunnel/simulated/start?command=/private/tmp/evil",
            headers={
                **self._headers("tunnel-simulated-start"),
                "Content-Type": "application/json",
            },
            content='{"key":"SYNTHETIC_SECRET","env":"bad"}',
        )
        self.assertEqual(injected.status_code, 400)
        self.assertNotIn("SYNTHETIC_SECRET", injected.text)
        self.assertNotIn("/private/tmp", injected.text)

        production = self.client.post(
            "/api/tunnel/production-start",
            headers=self._headers("tunnel-production-start"),
        )
        self.assertEqual(production.status_code, 409, production.text)
        payload = production.json()
        self.assertEqual(payload["code"], "real-approval-required")
        self.assertFalse(payload["real_connected"])
        self.assertEqual(payload["network_calls"], 0)

    def test_web_adapter_failure_is_fixed_and_leaves_simulation_stopped(self) -> None:
        self.assertEqual(self._post("start").status_code, 200)
        raw_error = "SYNTHETIC_SECRET /private/tmp/stage9-web-health"
        with mock.patch.object(
            OfflineTunnelAdapter,
            "health",
            side_effect=RuntimeError(raw_error),
        ):
            failed = self._post("health")

        self.assertEqual(failed.status_code, 503, failed.text)
        report = failed.json()["tunnel"]
        self.assertEqual(report["state"], "模拟停止")
        self.assertFalse(report["real_connected"])
        self.assertNotIn("SYNTHETIC_SECRET", failed.text)
        self.assertNotIn("/private/tmp", failed.text)
        refreshed = self.client.get("/api/status").json()
        self.assertEqual(refreshed["tunnel_wizard"]["simulation"]["state"], "模拟停止")

    def test_stop_failure_refresh_remains_unknown_not_passed(self) -> None:
        self.assertEqual(self._post("start").status_code, 200)
        with mock.patch.object(OfflineTunnelAdapter, "stop", side_effect=RuntimeError("secret")) as stop:
            failed = self._post("stop")
        stop.assert_called_once_with()
        self.assertEqual(failed.status_code, 503)
        report = self.client.get("/api/status").json()["tunnel_wizard"]["simulation"]
        self.assertEqual(report["state"], "模拟状态未知")
        self.assertFalse(report["passed"])
        self.assertIsNone(report["simulated_running"])
        self.assertEqual(self._post("start").status_code, 409)
        self.assertEqual(self._post("stop").status_code, 200)


if __name__ == "__main__":
    unittest.main()
