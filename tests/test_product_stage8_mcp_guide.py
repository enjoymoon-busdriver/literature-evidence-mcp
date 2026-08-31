from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from literature_evidence_mcp import mcp_selfcheck
from literature_evidence_mcp.web import create_app


PORT = 19128
ORIGIN = f"http://127.0.0.1:{PORT}"
NODE = Path(
    "/Users/hongchengyu/.cache/codex-runtimes/codex-primary-runtime/"
    "dependencies/node/bin/node"
)
STATIC_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "literature_evidence_mcp"
    / "static"
)


class StageEightSelfCheckTests(unittest.TestCase):
    def test_copy_only_config_is_stable_path_free_and_key_free(self) -> None:
        guide = mcp_selfcheck.local_mcp_guide()

        self.assertEqual(guide["state"], "copy_only_not_configured")
        self.assertEqual(guide["tool_count"], 8)
        self.assertIs(guide["api_key_required"], False)
        self.assertEqual(
            guide["cli"],
            "codex mcp add literature-evidence -- literature-evidence-mcp",
        )
        self.assertEqual(
            guide["toml"],
            '[mcp_servers.literature-evidence]\n'
            'command = "literature-evidence-mcp"\n',
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
            self.assertEqual(report["network_calls"], 0)
            self.assertEqual(report["model_calls"], 0)
            self.assertEqual(report["api_keys_used"], 0)
            self.assertEqual(report["external_config_writes"], 0)
            self.assertEqual(report["retry_count"], 0)
            self.assertEqual(len(report["steps"]), 6)
            self.assertTrue(all(step["passed"] for step in report["steps"]))
            self.assertIn("真实 STDIO", report["steps"][1]["message"])
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


class StageEightWebBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lemcp-stage8-web-")
        self.addCleanup(temporary.cleanup)
        self.application_root = Path(temporary.name) / "unused-user-root"
        self.client = TestClient(
            create_app(self.application_root, port=PORT),
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
        self.assertEqual(guide, mcp_selfcheck.local_mcp_guide())
        self.assertFalse(self.application_root.exists())

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
        self.assertEqual(report["network_calls"], 0)
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["api_keys_used"], 0)
        self.assertEqual(report["external_config_writes"], 0)
        self.assertFalse(self.application_root.exists())

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

    def test_beginner_guide_copy_boundary_and_no_remote_assets(self) -> None:
        html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

        for text in (
            "连接本地 AI / MCP",
            "复制但未配置",
            "不需要 OpenAI API key",
            "不会执行命令",
            "ChatGPT web 不会读取本机 Codex 配置",
            "远程插件或 Tunnel 留到 Stage 9",
            "不代表客户端已配置",
        ):
            self.assertIn(text, html)
        self.assertNotIn("https://", html.lower())
        self.assertNotIn("http://", html.lower())
        self.assertIn("navigator.clipboard.writeText", script)
        self.assertIn("已复制，但尚未配置", script)
        self.assertIn("new AbortController()", script)
        self.assertIn("state.mcpSelfCheckController.abort()", script)
        self.assertIn("requestId !== state.mcpSelfCheckRequestId", script)

    def test_newer_self_check_request_wins_even_if_old_response_arrives_last(
        self,
    ) -> None:
        self.assertTrue(NODE.is_file(), NODE)
        harness = r'''
const fs = require("fs");
const vm = require("vm");

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.value = id === "search-mode" ? "bm25" : "";
    this.checked = id === "build-blank";
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.files = [];
    this.children = [];
    this.classList = {
      add() {},
      remove() {},
      toggle() {},
    };
  }
  addEventListener() {}
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
}

const fakeElements = new Map();
global.document = {
  querySelector(selector) {
    const id = selector.startsWith("#") ? selector.slice(1) : selector;
    if (!fakeElements.has(id)) fakeElements.set(id, new FakeElement(id));
    return fakeElements.get(id);
  },
  createElement(tag) { return new FakeElement(tag); },
};
global.navigator = {clipboard: {writeText: async () => {}}};

const selfChecks = [];
global.fetch = (path, options = {}) => {
  if (path === "/api/mcp-self-check") {
    return new Promise((resolve) => {
      selfChecks.push({resolve, signal: options.signal});
    });
  }
  return new Promise(() => {});
};

function response(message, passed) {
  return {
    ok: true,
    headers: {get: () => "application/json"},
    json: async () => ({
      self_check: {
        passed,
        message,
        tool_count: 8,
        network_calls: 0,
        model_calls: 0,
        api_keys_used: 0,
        external_config_writes: 0,
        retry_count: 0,
        steps: [],
      },
    }),
  };
}

const app = fs.readFileSync(process.argv[1], "utf8");
const test = `
;(async () => {
  const first = runMcpSelfCheck();
  const second = runMcpSelfCheck();
  if (selfChecks.length !== 2) throw new Error("expected two requests");
  if (!selfChecks[0].signal.aborted) throw new Error("old request was not aborted");
  if (selfChecks[1].signal.aborted) throw new Error("new request was aborted");
  selfChecks[1].resolve(response("SECOND_RESULT", true));
  await second;
  selfChecks[0].resolve(response("STALE_RESULT", false));
  await first;
  const status = fakeElements.get("mcp-self-check-status").textContent;
  if (status !== "SECOND_RESULT") {
    throw new Error("stale response overwrote newer result: " + status);
  }
})().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exitCode = 1;
});
`;
vm.runInThisContext(app + test, {filename: process.argv[1]});
'''
        completed = subprocess.run(
            [str(NODE), "-e", harness, str(STATIC_ROOT / "app.js")],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
