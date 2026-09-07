from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from tests.node_runtime import NODE


STATIC_ROOT = Path(__file__).resolve().parents[1] / "src" / "literature_evidence_mcp" / "static"

HARNESS = r'''
const fs = require("fs");
const vm = require("vm");
const assert = require("assert/strict");
const root = process.argv[1];
const html = fs.readFileSync(root + "/index.html", "utf8");
const scripts = [...html.matchAll(/<script\s+src="([^"]+)"\s+defer><\/script>/g)].map(m => m[1]);
assert.deepEqual(scripts, ["/static/app.js", "/static/connections.js"]);

class Element {
  constructor(id = "", attributes = "") {
    this.id = id; this.value = id === "search-mode" ? "bm25" : "";
    this.checked = id === "build-blank"; this.disabled = /\bdisabled\b/.test(attributes);
    this.hidden = /\bhidden\b/.test(attributes); this.textContent = "";
    this.files = []; this.children = []; this.listeners = new Map();
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  addEventListener(name, action) {
    const actions = this.listeners.get(name) || []; actions.push(action); this.listeners.set(name, actions);
  }
  async fire(name) {
    if (name === "click" && this.disabled) return;
    await Promise.all((this.listeners.get(name) || []).map(action => action({preventDefault() {}})));
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}
const fakeElements = new Map();
for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
  fakeElements.set(match[1], new Element(match[1], match[0]));
}
const element = id => {
  assert(fakeElements.has(id), "missing actual HTML element: " + id);
  return fakeElements.get(id);
};
const documentEvents = new Map();
global.document = {
  querySelector: selector => element(selector.slice(1)), getElementById: element,
  createElement: tag => new Element(tag),
  addEventListener: (name, action) => documentEvents.set(name, action),
};
global.navigator = {clipboard: {writeText: async () => {}}};
const response = (body, ok = true) => ({ok, headers: {get: () => "application/json"}, json: async () => body});
const settings = (tunnelState = "stopped") => ({
  aliyun_configured: false, openai_tunnel_configured: false,
  tunnel_id: "", tunnel_backoff_accepted: false,
  tunnel: {state: tunnelState, passed: true},
});
const statusBody = (tunnelState = "stopped") => ({
  csrf_token: "synthetic-csrf", enhanced_available: false, enhanced: null, library_count: 0,
  connections: settings(tunnelState),
  upload_limits: {max_files: 20, max_file_bytes: 1024, max_total_bytes: 10240},
});
const recordedRequests = [];
let fetchRoute = null;
global.fetch = (path, options = {}) => {
  assert(path.startsWith("/api/"), "unexpected external request: " + path);
  recordedRequests.push({path, options});
  if (fetchRoute) {
    const result = fetchRoute(path, options);
    if (result !== undefined) return result;
  }
  if (path === "/api/status") return Promise.resolve(response(statusBody()));
  if (path === "/api/libraries") return Promise.resolve(response({libraries: []}));
  throw new Error("unplanned request: " + path);
};
const drain = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };
const loadScript = name => vm.runInThisContext(fs.readFileSync(root + "/" + name, "utf8"), {filename: name});
const domReady = async () => {
  if (documentEvents.has("DOMContentLoaded")) await documentEvents.get("DOMContentLoaded")();
  await drain();
};
const boot = async () => { loadScript("app.js"); loadScript("connections.js"); await domReady(); };
vm.runInThisContext(`;(async () => { ${process.argv[2]} })().catch(error => {
  process.stderr.write(String(error && error.stack || error)); process.exitCode = 1;
});`, {filename: "real-connections-scenario.js"});
'''


class RealConnectionsFrontendTests(unittest.TestCase):
    def run_scenario(self, scenario: str) -> None:
        completed = subprocess.run(
            [str(NODE), "-e", HARNESS, str(STATIC_ROOT), scenario],
            text=True, capture_output=True, timeout=15, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_boot_and_delayed_second_script_reveal_connections(self):
        self.run_scenario(r'''
loadScript("app.js");
await drain();
loadScript("connections.js");
await domReady();
assert.equal(element("real-connections").hidden, false, "late connections script left real setup hidden");
assert.equal(element("search-mode").value, "bm25");
assert.equal(element("save-aliyun-secret").listeners.get("click").length, 1);
assert(recordedRequests.every(request => !(request.options.method === "POST")), "startup performed a write or model call");
''')

    def test_saving_keys_clears_fields_and_only_saves_then_reads_status(self):
        self.run_scenario(r'''
await boot();
for (const [kind, field, button] of [["aliyun", "aliyun-secret", "save-aliyun-secret"],
                                   ["openai_tunnel", "openai-secret", "save-openai-secret"]]) {
  recordedRequests.length = 0;
  let finishSave;
  fetchRoute = path => path === "/api/connections/credentials"
    ? new Promise(resolve => {finishSave = resolve;}) : undefined;
  element(field).value = "synthetic-secret-only";
  const save = element(button).fire("click");
  assert.equal(element(field).value, "", "secret remained visible while saving");
  assert.equal(element(button).disabled, true);
  assert.equal(recordedRequests.length, 1);
  const request = recordedRequests[0];
  assert.equal(request.options.headers["X-Action-Intent"], "save-credential");
  assert.equal(request.options.headers["X-CSRF-Token"], "synthetic-csrf");
  assert.deepEqual(JSON.parse(request.options.body), {kind, key: "synthetic-secret-only"});
  finishSave(response({connections: settings()})); await save;
  assert.equal(element(field).value, "");
  assert.equal(element(button).disabled, false);
  assert.deepEqual(recordedRequests.map(request => request.path), ["/api/connections/credentials", "/api/status"]);
  assert(element("connection-notice").textContent.includes("没有联网测试"));
  assert([...fakeElements.values()].every(item => !item.textContent.includes("synthetic-secret-only")));
}
''')

    def test_vector_preview_build_failure_displays_actual_calls_and_usage(self):
        self.run_scenario(r'''
await boot(); recordedRequests.length = 0;
state.libraryId = "lib_synthetic"; element("snapshot-select").value = "snapshot-synthetic";
fetchRoute = path => {
  if (path.endsWith("/vectors/preview")) return Promise.resolve(response({vectors: {
    new_inputs: 12, new_input_chars: 120, planned_calls: 2, reused_inputs: 3,
  }}));
  if (path.endsWith("/vectors/build")) return Promise.resolve(response({
    error: "第二批失败", provider_audit: {call_count: 2, calls: [
      {model_id: "synthetic-model", usage: {total_tokens: 37}}, {model_id: "synthetic-model"},
    ]},
  }, false));
};
assert.equal(element("build-real-vectors").disabled, true);
await element("preview-real-vectors").fire("click");
assert(element("real-vector-plan").textContent.includes("最多 2 次"));
assert(element("real-vector-plan").textContent.includes("120 字符"));
assert.equal(element("build-real-vectors").disabled, false);
await element("build-real-vectors").fire("click");
const rendered = element("real-vector-plan").textContent;
assert(rendered.includes("已发送 2 次")); assert(rendered.includes("可能计费"));
assert(rendered.includes("37")); assert(rendered.includes("没有自动重试"));
assert.equal(element("build-real-vectors").disabled, true);
assert.equal(element("preview-real-vectors").disabled, false);
assert.equal(recordedRequests.length, 2);
for (const request of recordedRequests) assert.deepEqual(JSON.parse(request.options.body), {snapshot_id: "snapshot-synthetic"});
''')

    def test_late_start_and_status_responses_cannot_overwrite_stopped(self):
        self.run_scenario(r'''
await boot();
const pending = [];
fetchRoute = path => new Promise(resolve => pending.push({path, resolve}));
const start = element("real-tunnel-start").fire("click");
const stop = element("real-tunnel-stop").fire("click");
assert.deepEqual(pending.map(request => request.path), ["/api/tunnel/production-start", "/api/tunnel/production/stop"]);
pending[1].resolve(response({tunnel: {state: "stopped", passed: true}})); await stop;
pending[0].resolve(response({tunnel: {state: "connected", passed: true}})); await start;
assert(element("real-tunnel-status").textContent.includes("已停止"), "late start overwrote stop");
pending.length = 0;
const refresh = loadStatus();
const secondStop = element("real-tunnel-stop").fire("click");
pending[1].resolve(response({tunnel: {state: "stopped", passed: true}})); await secondStop;
pending[0].resolve(response(statusBody("connected"))); await refresh;
assert(element("real-tunnel-status").textContent.includes("已停止"), "late status refresh overwrote stop");
''')


if __name__ == "__main__":
    unittest.main()
