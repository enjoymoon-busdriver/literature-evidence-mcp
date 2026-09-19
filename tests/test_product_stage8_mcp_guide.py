from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from literature_evidence_mcp import mcp_selfcheck
from literature_evidence_mcp.web import create_app
from tests.test_formal_ui_frontend import run_scenario


PORT = 19128
ORIGIN = f"http://127.0.0.1:{PORT}"
STATIC_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "literature_evidence_mcp"
    / "static"
)


class StageEightSelfCheckTests(unittest.TestCase):
    def test_copy_only_config_is_stable_path_free_and_key_free(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lemcp-stage8-guide-") as raw:
            application_root = Path(raw) / "Library/Application Support/literature-evidence-mcp"
            application_root.mkdir(parents=True)
            shim = application_root / "mcp-server"
            shim.write_text(mcp_selfcheck._temporary_mcp_shim_text(application_root), encoding="utf-8")
            shim.chmod(0o700)
            with mock.patch.object(
                mcp_selfcheck,
                "default_application_root",
                return_value=application_root,
            ):
                guide = mcp_selfcheck.local_mcp_guide(application_root)

        self.assertEqual(guide["state"], "copy_ready_not_configured")
        self.assertEqual(guide["tool_count"], 8)
        self.assertIs(guide["api_key_required"], False)
        self.assertEqual(
            guide["cli"],
            "codex mcp add literature-evidence -- /bin/zsh -fc "
            "'exec \"$HOME/Library/Application Support/"
            "literature-evidence-mcp/mcp-server\"'",
        )
        self.assertEqual(
            guide["toml"],
            '[mcp_servers.literature-evidence]\n'
            'command = "/bin/zsh"\n'
            'args = ["-fc", "exec \\"$HOME/Library/Application Support/'
            'literature-evidence-mcp/mcp-server\\""]\n',
        )
        rendered = json.dumps(guide, ensure_ascii=False).lower()
        self.assertNotIn("api_key", guide["cli"])
        self.assertNotIn("api_key", guide["toml"])
        self.assertNotIn("/users/", rendered)
        self.assertNotIn(".venv", rendered)
        self.assertNotIn("pythonpath", rendered)
        self.assertNotIn(" env", rendered)
        self.assertIn("ChatGPT web 不会读取", guide["web_boundary_zh"])
        self.assertIn("Stage 9", guide["web_boundary_zh"])

    def test_custom_root_missing_or_unsafe_shim_is_not_copy_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lemcp-stage8-unready-") as raw:
            root = Path(raw)
            standard = root / "home/Library/Application Support/literature-evidence-mcp"
            standard.mkdir(parents=True)
            with mock.patch.object(
                mcp_selfcheck,
                "default_application_root",
                return_value=standard,
            ):
                self.assertEqual(
                    mcp_selfcheck.local_mcp_guide(root / "custom")["state"],
                    "unavailable",
                )
                self.assertEqual(
                    mcp_selfcheck.local_mcp_guide(standard)["state"],
                    "unavailable",
                )
                shim = standard / "mcp-server"
                shim.write_text("unsafe\n", encoding="utf-8")
                shim.chmod(0o755)
                self.assertEqual(
                    mcp_selfcheck.local_mcp_guide(standard)["state"],
                    "unavailable",
                )
                shim.chmod(0o700)
                self.assertEqual(mcp_selfcheck.local_mcp_guide(standard)["state"], "unavailable")

    def test_real_stdio_self_check_is_offline_readonly_and_does_not_touch_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lemcp-stage8-home-") as raw_home:
            fake_home = Path(raw_home)
            config = fake_home / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text("SENTINEL_CONFIG_DO_NOT_CHANGE\n", encoding="utf-8")
            before = config.read_bytes()

            real_socket = socket.socket

            def no_network(
                family: int = socket.AF_INET,
                *args: object,
                **kwargs: object,
            ) -> object:
                if family in {socket.AF_INET, socket.AF_INET6}:
                    raise AssertionError("self-check attempted a network socket")
                return real_socket(family, *args, **kwargs)

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(fake_home),
                    "OPENAI_API_KEY": "SYNTHETIC_SECRET_MUST_NOT_BE_READ",
                },
                clear=False,
            ), mock.patch.object(socket, "socket", side_effect=no_network):
                report = mcp_selfcheck.run_stdio_self_check()

            self.assertTrue(report["passed"], report)
            self.assertEqual(report["tool_count"], 8)
            self.assertIsNone(report["network_calls"])
            self.assertEqual(report["model_calls"], 0)
            self.assertEqual(report["api_keys_used"], 0)
            self.assertIsNone(report["external_config_writes"])
            self.assertEqual(report["retry_count"], 0)
            self.assertEqual(len(report["steps"]), 6)
            self.assertTrue(all(step["passed"] for step in report["steps"]))
            self.assertIn("/bin/zsh -fc", report["steps"][1]["message"])
            self.assertIn("不代表", report["message"])
            self.assertEqual(config.read_bytes(), before)
            rendered = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(str(fake_home), rendered)
            self.assertNotIn("SYNTHETIC_SECRET", rendered)

    def test_first_protocol_error_stops_without_retry_or_raw_error(self) -> None:
        secret = "SYNTHETIC_SECRET /private/tmp/stage8-secret"
        with mock.patch.object(
            mcp_selfcheck,
            "_tool_contract",
            side_effect=RuntimeError(secret),
        ):
            report = mcp_selfcheck.run_stdio_self_check()

        self.assertFalse(report["passed"])
        self.assertEqual(report["retry_count"], 0)
        self.assertEqual(len(report["steps"]), 2)
        self.assertTrue(report["steps"][0]["passed"])
        self.assertFalse(report["steps"][1]["passed"])
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("SYNTHETIC_SECRET", rendered)
        self.assertNotIn("/private/tmp", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_copy_template_uses_minimal_path_and_detects_fake_home_write(self) -> None:
        original = mcp_selfcheck.StdioServerParameters
        observed = []

        def mutate_then_launch(**kwargs):
            observed.append(kwargs)
            self.assertEqual(kwargs["command"], "/bin/zsh")
            self.assertEqual(kwargs["args"], ["-fc", mcp_selfcheck._SHELL_COMMAND])
            self.assertEqual(kwargs["env"]["PATH"], "/usr/bin:/bin")
            self.assertEqual(set(kwargs["env"]), {"HOME", "PATH"})
            kwargs["args"] = ["-fc", 'printf changed > "$HOME/.codex/config.toml"; ' + mcp_selfcheck._SHELL_COMMAND]
            return original(**kwargs)

        with mock.patch.object(mcp_selfcheck, "StdioServerParameters", side_effect=mutate_then_launch):
            report = mcp_selfcheck.run_stdio_self_check()
        self.assertEqual(len(observed), 1)
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["steps"]), 6)
        self.assertFalse(report["steps"][-1]["passed"])
        self.assertIsNone(report["network_calls"])
        self.assertIsNone(report["external_config_writes"])


class StageEightWebBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lemcp-stage8-web-")
        self.addCleanup(temporary.cleanup)
        self.application_root = Path(temporary.name) / "unused-user-root"
        self.application_root.mkdir()
        self.shim = self.application_root / "mcp-server"
        self.shim.write_text(mcp_selfcheck._temporary_mcp_shim_text(self.application_root), encoding="utf-8")
        self.shim.chmod(0o700)
        default_root = mock.patch(
            "literature_evidence_mcp.mcp_selfcheck.default_application_root",
            return_value=self.application_root,
        )
        default_root.start()
        self.addCleanup(default_root.stop)
        app = create_app(self.application_root, port=PORT)
        self.client = TestClient(
            app,
            base_url=ORIGIN,
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200, response.text)
        self.status = response.json()
        self.csrf = self.status["csrf_token"]

    def _headers(self) -> dict[str, str]:
        return {
            "Origin": ORIGIN,
            "X-CSRF-Token": self.csrf,
            "X-Action-Intent": "mcp-self-check",
        }

    def test_status_guide_and_parameterless_endpoint_use_real_stdio(self) -> None:
        guide = self.status["mcp_guide"]
        self.assertEqual(guide, mcp_selfcheck.local_mcp_guide(self.application_root))
        before = mcp_selfcheck._tree_identity(self.application_root)

        missing_intent = self.client.post(
            "/api/mcp-self-check",
            headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(missing_intent.status_code, 400)

        injected = self.client.post(
            "/api/mcp-self-check",
            headers={**self._headers(), "Content-Type": "application/json"},
            content='{"command":"/private/tmp/evil","env":{"TOKEN":"secret"}}',
        )
        self.assertEqual(injected.status_code, 400)
        self.assertNotIn("/private/tmp/evil", injected.text)
        self.assertNotIn("TOKEN", injected.text)

        query_injected = self.client.post(
            "/api/mcp-self-check?command=/private/tmp/evil&env=secret",
            headers=self._headers(),
        )
        self.assertEqual(query_injected.status_code, 400)
        self.assertNotIn("/private/tmp/evil", query_injected.text)
        self.assertNotIn("secret", query_injected.text)

        checked = self.client.post("/api/mcp-self-check", headers=self._headers())
        self.assertEqual(checked.status_code, 200, checked.text)
        report = checked.json()["self_check"]
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["tool_count"], 8)
        self.assertIsNone(report["network_calls"])
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["api_keys_used"], 0)
        self.assertIsNone(report["external_config_writes"])
        self.assertEqual(before, mcp_selfcheck._tree_identity(self.application_root))

    def test_unexpected_endpoint_failure_is_redacted(self) -> None:
        raw_error = "SYNTHETIC_SECRET /private/tmp/stage8-hidden-path"
        with mock.patch(
            "literature_evidence_mcp.web.run_stdio_self_check",
            side_effect=RuntimeError(raw_error),
        ):
            response = self.client.post(
                "/api/mcp-self-check", headers=self._headers()
            )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC_SECRET", response.text)
        self.assertNotIn("/private/tmp", response.text)
        self.assertNotIn("Traceback", response.text)

    def test_removed_entry_disables_guide_and_selfcheck(self) -> None:
        self.shim.unlink()
        self.assertEqual(self.client.get("/api/status").json()["mcp_guide"]["state"], "unavailable")
        with mock.patch("literature_evidence_mcp.web.run_stdio_self_check") as run:
            result = self.client.post("/api/mcp-self-check", headers=self._headers())
        self.assertEqual(result.status_code, 503)
        run.assert_not_called()

    def test_beginner_guide_copy_boundary_and_no_remote_assets(self) -> None:
        html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        connections = (STATIC_ROOT / "connections.js").read_text(encoding="utf-8")
        ui_assets = html + script + connections

        for text in (
            "本机 MCP · 八个只读工具",
            "复制不会执行命令或写配置",
            "本地 MCP 无需 OpenAI Key",
            "ChatGPT desktop",
            "ChatGPT Secure MCP Tunnel（离线模拟）",
            "不收集真实 Key",
            "通过不代表客户端已配置",
        ):
            self.assertIn(text, ui_assets)
        self.assertIn('id="content"', html)
        self.assertIn('data-action="apps"', html)
        self.assertNotIn("https://", ui_assets.lower())
        self.assertNotIn("http://", ui_assets.lower())
        self.assertIn("navigator.clipboard.writeText", connections)
        self.assertIn("mcpSelfCheck", script + connections)
        self.assertIn("requestId", connections)
        self.assertIn("FolioApp", script)
        self.assertIn("FolioConnections", connections)

    def test_newer_self_check_request_wins_even_if_old_response_arrives_last(
        self,
    ) -> None:
        run_scenario(self, r'''
state.csrfToken = 'synthetic-csrf';
state.mcpGuide = {state:'copy_ready_not_configured', server_name:'literature-evidence', tool_count:8, api_key_required:false, cli:'cli', toml:'toml'};
let pending = [];
route = (path, options) => path === '/api/mcp-self-check'
  ? new Promise(resolve => pending.push({resolve, options})) : undefined;
const first = FolioConnections.runMcpSelfCheck();
const second = FolioConnections.runMcpSelfCheck();
assert.equal(pending.length, 2);
pending[1].resolve(response({self_check:{passed:true, message:'SECOND_RESULT', tool_count:8, scope:'local_stdio_self_check', evidence_level:'offline_local_stdio', observation_scope_zh:'scope', network_calls:null, model_calls:0, api_keys_used:0, external_config_writes:null, retry_count:0, steps:Array.from({length:6}, (_, i) => ({name:'step'+i, message:'ok', passed:true}))}}));
await second;
pending[0].resolve(response({self_check:{passed:true, message:'STALE_RESULT', tool_count:8, scope:'local_stdio_self_check', evidence_level:'offline_local_stdio', observation_scope_zh:'scope', network_calls:null, model_calls:0, api_keys_used:0, external_config_writes:null, retry_count:0, steps:Array.from({length:6}, (_, i) => ({name:'step'+i, message:'ok', passed:true}))}}));
await first;
assert.equal(state.mcpSelfCheck.message, 'SECOND_RESULT');
''')


if __name__ == "__main__":
    unittest.main()
