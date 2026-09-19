from __future__ import annotations

import unittest

from tests.test_formal_ui_frontend import run_scenario


CONNECTIONS = r'''
const modelIds = {
  query_rewrite: 'qwen3.8-flash', vector_recall: 'qwen3.7-text-embedding',
  candidate_rerank: 'qwen3-rerank',
};
const connectionStatus = (tunnelState = 'stopped', ids = modelIds) => ({
  aliyun_configured: false, openai_tunnel_configured: false,
  tunnel_id: '', tunnel_backoff_accepted: false,
  model_settings: {
    enabled: false, provider: 'aliyun-beijing', region: 'cn-beijing',
    model_ids: {...ids}, recommended_model_ids: {...modelIds},
  },
  tunnel: {state: tunnelState, passed: true},
});
'''


class RealConnectionsFrontendTests(unittest.TestCase):
    def test_tunnel_installation_status_is_visible_without_requests(self) -> None:
        run_scenario(self, CONNECTIONS + r'''
state.page = 'apps';
const status = connectionStatus();
status.tunnel.client_installed = false;
FolioConnections.acceptStatus({connections: status}); FolioApp.render();
assert(nodes.get('content').innerHTML.includes('未找到；请按教程通过 Homebrew 安装'));
status.tunnel.client_installed = true;
FolioConnections.acceptStatus({connections: status}); FolioApp.render();
assert(nodes.get('content').innerHTML.includes('已找到（应用自带或 Homebrew 安装）'));
assert.equal(requests.length, 0, 'installation display must not start or check a connection');
''')

    def test_startup_is_read_only_and_secret_fields_clear_immediately(self) -> None:
        run_scenario(self, CONNECTIONS + r'''
assert.equal(requests.length, 0, 'script startup issued a request');
assert.equal(FolioConnections.enhancedAvailable(), false);
state.csrfToken = 'synthetic-csrf';
FolioConnections.acceptStatus({connections: connectionStatus()});

for (const [page, action, fieldId, kind] of [
  ['settings', 'save-aliyun-key', 'aliyun-secret', 'aliyun'],
  ['apps', 'save-openai-key', 'openai-secret', 'openai_tunnel'],
]) {
  state.page = page; FolioApp.render(); requests.length = 0;
  const field = nodes.get(fieldId);
  field.value = 'synthetic-secret-only';
  let finish;
  route = (path, options) => path === '/api/connections/credentials'
    ? new Promise(resolve => { finish = {resolve, options}; }) : undefined;
  const saving = FolioConnections.handleAction(action);
  assert.equal(field.value, '', 'secret remained in field while saving');
  assert.equal(requests.length, 1);
  assert.equal(finish.options.headers['X-Action-Intent'], 'save-credential');
  assert.equal(finish.options.headers['X-CSRF-Token'], 'synthetic-csrf');
  assert.deepEqual(JSON.parse(finish.options.body), {kind, key: 'synthetic-secret-only'});
  finish.resolve(response({connections: connectionStatus()}));
  await saving;
  assert.equal(requests.length, 1, 'save triggered an implicit check or extra request');
  assert(!nodes.get('content').innerHTML.includes('synthetic-secret-only'));
}
''')

    def test_vector_preview_failure_and_plan_identity(self) -> None:
        run_scenario(self, CONNECTIONS + r'''
state.csrfToken = 'synthetic-csrf';
state.libraries = [{library_id: 'lib_synthetic', name: 'Synthetic'}];
state.libraryId = 'lib_synthetic'; state.libraryEpoch = 1;
state.snapshotId = 'snapshot-synthetic'; state.snapshotEpoch = 1;
state.snapshots = [{snapshot_id: 'snapshot-synthetic', members: [{document_id: 'doc-1'}]}];
FolioConnections.acceptStatus({connections: connectionStatus()});
FolioConnections.showVectorDialog(state.snapshotId);
const plan = nodes.get('vector-plan');
const previewButton = new Element('button'); previewButton.dataset.action = 'preview-vectors';
const buildButton = new Element('button'); buildButton.dataset.action = 'build-vectors'; buildButton.disabled = true;
document.body.append(previewButton, buildButton);

const pending = [];
route = (path, options) => new Promise(resolve => pending.push({path, options, resolve}));
const failedPreview = FolioConnections.vectors('preview');
assert(previewButton.disabled && buildButton.disabled, 'in-flight vector controls stayed enabled');
pending[0].resolve(response({error: '预览失败'}, false));
await failedPreview;
assert(buildButton.disabled, 'failed preview enabled vector build');

const preview = FolioConnections.vectors('preview');
pending[1].resolve(response({vectors: {
  library_id: 'lib_synthetic', snapshot_id: 'snapshot-synthetic',
  new_inputs: 12, new_input_chars: 120, planned_calls: 2, reused_inputs: 3,
}}));
await preview;
assert(plan.textContent.includes('预计最多 2 次调用'));
assert(plan.textContent.includes('120 字符'));
assert.equal(buildButton.disabled, false);

const build = FolioConnections.vectors('build');
assert(previewButton.disabled && buildButton.disabled, 'build left vector controls enabled');
pending[2].resolve(response({
  error: '第二批失败', provider_audit: {call_count: 2, calls: [
    {model_id: 'synthetic-model', usage: {total_tokens: 37}},
    {model_id: 'synthetic-model'},
  ]},
}, false));
await build;
assert(plan.textContent.includes('已发送 2 次'));
assert(plan.textContent.includes('可能计费'));
assert(plan.textContent.includes('37'), 'provider usage was dropped from failure audit');
assert(plan.textContent.includes('没有自动重试'));
assert(buildButton.disabled);
for (const item of pending.slice(0, 3)) {
  assert.deepEqual(JSON.parse(item.options.body), {snapshot_id: 'snapshot-synthetic'});
}

const oldPlan = plan.textContent;
const oldPreview = FolioConnections.vectors('preview');
FolioConnections.acceptStatus({connections: connectionStatus('stopped', {
  ...modelIds, vector_recall: 'changed-vector-model',
})});
pending[3].resolve(response({vectors: {
  library_id: 'lib_synthetic', snapshot_id: 'snapshot-synthetic',
  new_inputs: 99, new_input_chars: 999, planned_calls: 99, reused_inputs: 0,
}}));
await oldPreview;
assert.equal(plan.textContent, oldPlan, 'stale preview crossed model configuration');
assert(buildButton.disabled, 'old model plan allowed build after configuration changed');
''')

    def test_newer_model_check_wins_and_uses_explicit_action(self) -> None:
        run_scenario(self, CONNECTIONS + r'''
state.csrfToken = 'synthetic-csrf';
FolioConnections.acceptStatus({connections: connectionStatus()});
const pending = [];
route = (path, options) => new Promise(resolve => pending.push({path, options, resolve}));
const oldCheck = FolioConnections.checkModels();
const newCheck = FolioConnections.checkModels();
assert.equal(pending.length, 2);
for (const item of pending) {
  assert.equal(item.path, '/api/connections/models/check');
  assert.equal(item.options.headers['X-Action-Intent'], 'check-model-connections');
}
const results = label => Object.keys(modelIds).map(role => ({
  role, model_id: modelIds[role], passed: true, classification: 'available', message: label,
}));
pending[1].resolve(response({connection_check: {passed: true, results: results('new')}}));
await newCheck;
pending[0].resolve(response({connection_check: {passed: true, results: results('old')}}));
await oldCheck;
assert(FolioConnections.local.connectionCheck.results.every(item => item.message === 'new'));
''')

    def test_real_tunnel_stop_wins_over_late_start_and_status(self) -> None:
        run_scenario(self, CONNECTIONS + r'''
state.csrfToken = 'synthetic-csrf';
FolioConnections.acceptStatus({connections: connectionStatus('stopped')});
const pending = [];
route = (path, options) => new Promise(resolve => pending.push({path, options, resolve}));
const start = FolioConnections.runRealTunnel('start');
const stop = FolioConnections.runRealTunnel('stop');
assert.deepEqual(pending.map(item => item.path), [
  '/api/tunnel/production-start', '/api/tunnel/production/stop',
]);
assert.equal(pending[0].options.headers['X-Action-Intent'], 'tunnel-production-start');
assert.equal(pending[1].options.headers['X-Action-Intent'], 'tunnel-production-stop');
pending[1].resolve(response({tunnel: {state: 'stopped', passed: true}}));
await stop;
pending[0].resolve(response({tunnel: {state: 'connected', passed: true}}));
await start;
assert.equal(FolioConnections.local.connections.tunnel.state, 'stopped');

pending.length = 0;
const secondStop = FolioConnections.runRealTunnel('stop');
const refresh = FolioApp.loadStatus();
pending[0].resolve(response({tunnel: {state: 'stopped', passed: true}}));
await secondStop;
pending[1].resolve(response({
  service: 'ready', csrf_token: 'new-csrf', connections: connectionStatus('connected'),
}));
await refresh;
assert.equal(FolioConnections.local.connections.tunnel.state, 'stopped', 'late status overwrote completed stop');
''')

    def test_tunnel_automatically_checks_without_repainting_and_keeps_manual_check(self) -> None:
        run_scenario(self, CONNECTIONS + r'''
const timers = new Map(); let serial = 0;
global.setTimeout = (fn, delay) => { timers.set(++serial, {fn, delay}); return serial; };
global.clearTimeout = id => timers.delete(id);
const tick = async () => { const [id, timer] = timers.entries().next().value; timers.delete(id); timer.fn(); await drain(); };
state.page = 'apps';
FolioConnections.acceptStatus({connections: connectionStatus()});
let report = {state: 'starting', running: true, passed: true};
route = () => Promise.resolve(response({tunnel: report}));
await FolioConnections.runRealTunnel('start');
assert.equal(requests.length, 1);
assert.equal(timers.size, 1);
assert(nodes.get('content').innerHTML.includes('启动中'));
report = {state: 'not_ready', running: true, passed: false};
await tick();
assert.equal(requests[1].path, '/api/tunnel/production/health');
assert.equal(requests[1].options.headers['X-Action-Intent'], 'tunnel-production-health');
assert(nodes.get('real-tunnel-status').textContent.includes('未就绪'));
assert.equal(timers.size, 1);
const field = nodes.get('openai-secret'); field.value = 'unsaved-synthetic'; field.focus();
const contentBefore = nodes.get('content').innerHTML;
report = {state: 'connected', running: true, passed: true, chatgpt_tool_discovery_checked: false};
await tick();
assert(nodes.get('real-tunnel-status').textContent.includes('已连接（本地 Tunnel）'));
assert.equal(nodes.get('content').innerHTML, contentBefore, 'automatic check repainted page');
assert.equal(nodes.get('openai-secret'), field, 'input node was replaced');
assert.equal(field.value, 'unsaved-synthetic');
assert.equal(document.activeElement, field);
assert.equal(timers.size, 0, 'polling continued after ready');
assert(/data-action="real-tunnel-health" >/.test(nodes.get('content').innerHTML), 'manual check disabled between checks');
await FolioConnections.handleAction('real-tunnel-health');
assert.equal(timers.size, 0, 'manual check started ongoing monitoring');
// An unready result after the startup check window stays honest without more polling.
FolioConnections.local.tunnelCheckUntil = Date.now() - 1;
report = {state: 'not_ready', running: true, passed: false};
await FolioConnections.handleAction('real-tunnel-health');
assert.equal(timers.size, 0);
assert(nodes.get('content').innerHTML.includes('未就绪'));
report = {state: 'error', running: false, passed: false, error_code: 'auth_failed'};
await FolioConnections.handleAction('real-tunnel-health');
assert(nodes.get('content').innerHTML.includes('连接失败'));
assert.equal(timers.size, 0);
assert.equal(requests.filter(r => r.path === '/api/tunnel/production-start').length, 1, 'app restarted tunnel');
''')

    def test_auto_health_stop_rejects_late_response_and_clears_timer(self) -> None:
        run_scenario(self, CONNECTIONS + r'''
const timers = new Map(); let serial = 0;
global.setTimeout = (fn, delay) => { timers.set(++serial, fn); return serial; };
global.clearTimeout = id => timers.delete(id);
state.page = 'apps';
FolioConnections.acceptStatus({connections: connectionStatus()});
const pending = [];
route = (path, options) => new Promise(resolve => pending.push({path, resolve}));
const start = FolioConnections.runRealTunnel('start');
assert(nodes.get('content').innerHTML.includes('启动中…'));
pending[0].resolve(response({tunnel: {state: 'starting', running: true}})); await start;
const [id, fn] = timers.entries().next().value; timers.delete(id); fn(); await drain();
assert(nodes.get('real-tunnel-status').textContent.includes('检查中…'));
assert.equal(pending[1].path, '/api/tunnel/production/health');
state.page = 'settings'; FolioApp.render();
const stop = FolioConnections.runRealTunnel('stop');
const settingsBefore = nodes.get('content').innerHTML;
pending[2].resolve(response({tunnel: {state: 'stopped', running: false}})); await stop;
pending[1].resolve(response({tunnel: {state: 'connected', running: true}})); await drain();
assert.equal(FolioConnections.local.connections.tunnel.state, 'stopped');
assert.equal(timers.size, 0);
assert.equal(state.page, 'settings');
assert.equal(nodes.get('content').innerHTML, settingsBefore);
// A queued timer is also removed when stop occurs between checks.
const again = FolioConnections.runRealTunnel('start');
pending[3].resolve(response({tunnel: {state: 'starting', running: true}})); await again;
assert.equal(timers.size, 1);
const stopAgain = FolioConnections.runRealTunnel('stop');
assert.equal(timers.size, 0);
pending[4].resolve(response({tunnel: {state: 'stopped', running: false}})); await stopAgain;
// A failed start never starts polling or retries the start request.
const failed = FolioConnections.runRealTunnel('start');
pending[5].resolve(response({error: 'synthetic failure', tunnel: {state: 'error', running: false}}, false)); await failed;
assert.equal(timers.size, 0);
''')


if __name__ == "__main__":
    unittest.main()
