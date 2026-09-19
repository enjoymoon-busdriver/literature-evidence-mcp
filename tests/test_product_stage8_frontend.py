from __future__ import annotations

import unittest

from tests.test_formal_ui_frontend import run_scenario


VALID_REPORT = r'''
const validReport = (overrides = {}) => ({
  passed: true, scope: 'local_stdio_self_check', evidence_level: 'offline_local_stdio',
  observation_scope_zh: 'fake HOME observation scope',
  message: 'fake HOME template tested; current user config/client not tested',
  tool_count: 8, network_calls: null, model_calls: 0, api_keys_used: 0,
  external_config_writes: null, retry_count: 0,
  steps: Array.from({length: 6}, (_, index) => ({name: `step-${index}`, message: 'ok', passed: true})),
  ...overrides,
});
'''


class StageEightFrontendTests(unittest.TestCase):
    def test_guide_copy_boundary_and_self_check_report_validation(self) -> None:
        run_scenario(self, VALID_REPORT + r'''
const guide = {
  state: 'copy_ready_not_configured', server_name: 'literature-evidence',
  tool_count: 8, api_key_required: false,
  cli: 'codex mcp add literature-evidence', toml: '[mcp_servers.literature-evidence]',
};
state.mcpGuide = guide;
let rendered = FolioConnections.renderApps();
assert(rendered.includes('data-action="copy-mcp-cli"'));
assert(rendered.includes('data-action="copy-mcp-toml"'));
assert(rendered.includes('data-action="mcp-self-check"'));
assert(!/data-action="copy-mcp-cli"[^>]*disabled/.test(rendered));
assert(!/data-action="mcp-self-check"[^>]*disabled/.test(rendered));

const valid = validReport();
assert(FolioApp.validMcpSelfCheck(valid), 'valid report rejected');
for (const key of Object.keys(valid)) {
  const missing = {...valid}; delete missing[key];
  assert(!FolioApp.validMcpSelfCheck(missing), `missing field accepted: ${key}`);
}
for (const bad of [
  {...valid, steps: valid.steps.slice(0, 5)},
  {...valid, steps: [{name: 'bad', message: 'bad', passed: true}]},
  {...valid, network_calls: 0}, {...valid, external_config_writes: 0},
  {...valid, model_calls: 1}, {...valid, api_keys_used: 1}, {...valid, retry_count: 1},
]) assert(!FolioApp.validMcpSelfCheck(bad), 'malformed report accepted');

state.csrfToken = 'synthetic-csrf';
let pending;
route = (path, options) => path === '/api/mcp-self-check'
  ? new Promise(resolve => { pending = {resolve, options}; }) : undefined;
const check = FolioConnections.runMcpSelfCheck();
assert.equal(pending.options.headers['X-Action-Intent'], 'mcp-self-check');
assert.equal(pending.options.headers['X-CSRF-Token'], 'synthetic-csrf');
pending.resolve(response({self_check: valid}));
await check;
assert.equal(state.mcpSelfCheck, valid);
rendered = FolioConnections.renderApps();
assert(rendered.includes('8 个只读工具'));
assert(rendered.includes('零模型、零 Key、零重试'));

route = () => Promise.resolve(response({self_check: {...valid, steps: []}}));
await FolioConnections.runMcpSelfCheck();
assert.equal(state.mcpSelfCheck.passed, false);
assert(state.mcpSelfCheck.message.includes('数据格式无效'));

state.mcpGuide = {...guide, state: 'unavailable', cli: 'stale cli', toml: 'stale toml'};
rendered = FolioConnections.renderApps();
assert(/data-action="copy-mcp-cli"[^>]*disabled/.test(rendered));
assert(/data-action="mcp-self-check"[^>]*disabled/.test(rendered));
assert(!rendered.includes('stale cli'));
assert(!rendered.includes('stale toml'));
''')


if __name__ == "__main__":
    unittest.main()
