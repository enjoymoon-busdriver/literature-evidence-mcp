from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


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


class StageNineFrontendTests(unittest.TestCase):
    def test_beginner_copy_and_permission_boundary_has_no_secret_input(self) -> None:
        html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        for text in (
            "ChatGPT Secure MCP Tunnel（离线模拟）",
            "演示配置模板",
            "Read + Manage",
            "Read + Use",
            "OpenAI Platform 组织",
            "ChatGPT 工作区",
            "不会收集或保存真实 API key",
            "本地健康检查不等于 ChatGPT 已发现工具",
            "零自动重试",
            "首错停止",
        ):
            self.assertIn(text, html)
        self.assertNotIn('id="tunnel-api-key"', html)
        self.assertNotIn('id="tunnel-command"', html)
        self.assertNotIn('id="tunnel-path"', html)
        self.assertNotIn("https://", html.lower())
        self.assertNotIn("http://", html.lower())
        for button in ("start", "health", "stop"):
            self.assertRegex(html, rf'id="tunnel-{button}-button"[^>]*disabled')

    def test_late_health_response_cannot_overwrite_stopped_state(self) -> None:
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
    this.classList = {add() {}, remove() {}, toggle() {}};
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

const tunnelRequests = [];
global.fetch = (path, options = {}) => {
  if (path.startsWith("/api/tunnel/simulated/")) {
    return new Promise((resolve) => {
      tunnelRequests.push({path, resolve, signal: options.signal});
    });
  }
  return new Promise(() => {});
};

function tunnelReport(action, state) {
  return {
    mode: "offline_simulation",
    action,
    state,
    passed: true,
    simulated: true,
    simulated_running: state === "模拟状态未知" ? null : state !== "模拟停止",
    real_connected: false,
    local_health_checked: action === "health",
    chatgpt_tool_discovery_checked: false,
    network_calls: 0,
    model_calls: 0,
    api_keys_collected: 0,
    external_config_writes: 0,
    retry_count: 0,
    message: state,
  };
}

function response(action, state) {
  return {
    ok: true,
    headers: {get: () => "application/json"},
    json: async () => ({tunnel: tunnelReport(action, state)}),
  };
}

const app = fs.readFileSync(process.argv[1], "utf8");
const test = `
;(async () => {
  const valid = tunnelReport("start", "模拟运行");
  if (!validTunnelReport(valid, "start")) throw new Error("valid report rejected");
  for (const key of Object.keys(valid)) {
    const missing = {...valid};
    delete missing[key];
    if (validTunnelReport(missing, "start")) throw new Error("missing field accepted: " + key);
  }
  for (const [key, value] of Object.entries({
    real_connected: true, network_calls: 1, retry_count: 1,
    simulated: false, simulated_running: false,
    local_health_checked: true, passed: "true", action: "stop",
  })) {
    if (validTunnelReport({...valid, [key]: value}, "start")) {
      throw new Error("invalid field accepted: " + key);
    }
  }
  const unknown = {...valid, state: "模拟状态未知", simulated_running: null, passed: false};
  if (!validTunnelReport(unknown, "start")) throw new Error("unknown failure rejected");
  if (validTunnelReport({...unknown, passed: true})) throw new Error("unknown passed accepted");
  renderTunnelReport(null);
  if (!fakeElements.get("tunnel-start-button").disabled ||
      !fakeElements.get("tunnel-health-button").disabled ||
      !fakeElements.get("tunnel-stop-button").disabled) {
    throw new Error("invalid report left controls enabled");
  }
  renderTunnelReport(tunnelReport("start", "模拟运行"));
  const health = runTunnelHealth();
  const stop = runTunnelStop();
  if (tunnelRequests.length !== 2) throw new Error("expected health and stop requests");
  if (!tunnelRequests[0].signal.aborted) throw new Error("old health request was not aborted");
  if (tunnelRequests[1].signal.aborted) throw new Error("stop request was aborted");
  tunnelRequests[1].resolve(response("stop", "模拟停止"));
  await stop;
  tunnelRequests[0].resolve(response("health", "模拟通过"));
  await health;
  const stateText = fakeElements.get("tunnel-state").textContent;
  if (!stateText.includes("模拟停止")) {
    throw new Error("late health response overwrote stopped state: " + stateText);
  }
  if (state.tunnelReport.state !== "模拟停止") {
    throw new Error("internal state was overwritten by late health response");
  }
  let resolveStatus;
  global.fetch = () => new Promise((resolve) => { resolveStatus = resolve; });
  state.tunnelAction = "stop";
  const refresh = loadStatus();
  state.tunnelAction = null;
  resolveStatus({
    ok: true,
    headers: {get: () => "application/json"},
    json: async () => ({
      csrf_token: "test", enhanced_available: false, library_count: 0,
      upload_limits: {max_files: 20, max_file_bytes: 1, max_total_bytes: 1},
      tunnel_wizard: {simulation: tunnelReport("status", "模拟运行")},
    }),
  });
  await refresh;
  if (state.tunnelReport.state !== "模拟停止") {
    throw new Error("status refresh begun during stop overwrote stopped state");
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
