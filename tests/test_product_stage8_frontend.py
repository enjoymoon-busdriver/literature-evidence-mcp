from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


from tests.node_runtime import NODE
STATIC_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "literature_evidence_mcp"
    / "static"
)


class StageEightFrontendTests(unittest.TestCase):
    def test_frontend_validates_guides_and_self_check_reports(self) -> None:
        html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="copy-mcp-cli-button" class="secondary" type="button" disabled', html)
        self.assertIn('id="copy-mcp-toml-button" class="secondary" type="button" disabled', html)
        self.assertIn('id="mcp-self-check-button" type="button" disabled', html)

        harness = r'''
const fs = require("fs");
const vm = require("vm");

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.value = id === "search-mode" ? "bm25" : "";
    this.checked = id === "build-blank";
    this.disabled = [
      "copy-mcp-cli-button",
      "copy-mcp-toml-button",
      "mcp-self-check-button",
    ].includes(id);
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

let pending;
global.fetch = (path, options = {}) => {
  if (path === "/api/mcp-self-check") {
    return new Promise((resolve) => {
      pending = {resolve, signal: options.signal};
    });
  }
  return new Promise(() => {});
};

function response(report) {
  return {
    ok: true,
    headers: {get: () => "application/json"},
    json: async () => ({self_check: report}),
  };
}

function validReport(overrides = {}) {
  return {
    passed: true,
    scope: "local_stdio_self_check",
    evidence_level: "offline_local_stdio",
    observation_scope_zh: "fake HOME observation scope",
    message: "fake HOME template tested; current user config/client not tested",
    tool_count: 8,
    network_calls: null,
    model_calls: 0,
    api_keys_used: 0,
    external_config_writes: null,
    retry_count: 0,
    steps: Array.from({length: 6}, (_, index) => ({
      name: `step-${index}`,
      message: "ok",
      passed: true,
    })),
    ...overrides,
  };
}

const app = fs.readFileSync(process.argv[1], "utf8");
const test = `
;(async () => {
  renderMcpGuide({
    state: "copy_ready_not_configured",
    server_name: "literature-evidence",
    tool_count: 8,
    api_key_required: false,
    cli: "codex mcp add literature-evidence",
    toml: "[mcp_servers.literature-evidence]",
  });
  if (fakeElements.get("copy-mcp-cli-button").disabled) throw new Error("valid guide stayed disabled");

  const valid = runMcpSelfCheck();
  pending.resolve(response(validReport()));
  await valid;
  const rendered = fakeElements.get("mcp-self-check-report").children
    .map((child) => child.textContent).join("|");
  if (!rendered.includes("网络调用：未观测")) throw new Error("null network count was not labeled");
  if (!rendered.includes("外部配置写入：未观测")) throw new Error("null write count was not labeled");

  const malformed = runMcpSelfCheck();
  pending.resolve(response({...validReport(), steps: [{name: "bad", message: "bad", passed: true}]}));
  await malformed;
  const status = fakeElements.get("mcp-self-check-status").textContent;
  if (!status.includes("数据格式无效")) throw new Error("malformed report was not rejected");
  if (!fakeElements.get("mcp-self-check-report").hidden) throw new Error("malformed report was rendered");
  if (fakeElements.get("mcp-self-check-report").children.length !== 0) throw new Error("stale report remained");

  fakeElements.get("mcp-cli").value = "stale cli";
  fakeElements.get("mcp-toml").value = "stale toml";
  renderMcpGuide({state: "unavailable", reason: "not ready"});
  if (fakeElements.get("mcp-cli").value || fakeElements.get("mcp-toml").value) throw new Error("unavailable guide kept stale text");
  if (!fakeElements.get("copy-mcp-cli-button").disabled || !fakeElements.get("mcp-self-check-button").disabled) throw new Error("unavailable guide enabled controls");
})().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exitCode = 1;
});
`;
vm.runInThisContext(app + test, {filename: process.argv[1]});
'''
        self.assertTrue(NODE.is_file(), NODE)
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
