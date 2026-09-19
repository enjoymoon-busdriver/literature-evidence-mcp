from __future__ import annotations

import unittest

from tests.test_formal_ui_frontend import run_scenario


TUNNEL_REPORT = r'''
const tunnelReport = (action, stateValue, overrides = {}) => ({
  mode: 'offline_simulation', action, state: stateValue,
  passed: stateValue !== '模拟状态未知', simulated: true,
  simulated_running: stateValue === '模拟状态未知' ? null : stateValue !== '模拟停止',
  real_connected: false, local_health_checked: action === 'health',
  chatgpt_tool_discovery_checked: false, network_calls: 0, model_calls: 0,
  api_keys_collected: 0, external_config_writes: 0, retry_count: 0,
  message: stateValue, ...overrides,
});
'''


class StageNineFrontendTests(unittest.TestCase):
    def test_offline_simulation_boundary_and_strict_report_validation(self) -> None:
        run_scenario(self, TUNNEL_REPORT + r'''
const valid = tunnelReport('start', '模拟运行');
assert(FolioApp.validSimulatedTunnelReport(valid, 'start'), 'valid report rejected');
for (const key of Object.keys(valid)) {
  const missing = {...valid}; delete missing[key];
  assert(!FolioApp.validSimulatedTunnelReport(missing, 'start'), `missing field accepted: ${key}`);
}
for (const [key, value] of Object.entries({
  real_connected: true, network_calls: 1, model_calls: 1, api_keys_collected: 1,
  external_config_writes: 1, retry_count: 1, simulated: false,
  simulated_running: false, local_health_checked: true,
  chatgpt_tool_discovery_checked: true, passed: 'true', action: 'stop',
})) {
  assert(!FolioApp.validSimulatedTunnelReport({...valid, [key]: value}, 'start'), `invalid field accepted: ${key}`);
}
const unknown = tunnelReport('start', '模拟状态未知');
assert(FolioApp.validSimulatedTunnelReport(unknown, 'start'));
assert(!FolioApp.validSimulatedTunnelReport({...unknown, passed: true}, 'start'));

state.mcpGuide = {state: 'unavailable'};
state.simulatedTunnel = valid;
const output = FolioConnections.renderApps();
const offline = output.slice(output.indexOf('ChatGPT Secure MCP Tunnel（离线模拟）'));
assert(offline.includes('不收集真实 Key'));
assert(offline.includes('网络调用与外部配置写入均为 0'));
assert(!offline.includes('type="password"'));
assert(!offline.includes('openai-secret'));
assert(!offline.includes('real-tunnel-id'));
assert(!offline.includes('http://') && !offline.includes('https://'));
''')

    def test_stop_wins_over_late_health_and_status_responses(self) -> None:
        run_scenario(self, TUNNEL_REPORT + r'''
state.csrfToken = 'synthetic-csrf';
state.simulatedTunnel = tunnelReport('start', '模拟运行');
const pending = [];
route = (path, options) => new Promise(resolve => pending.push({path, options, resolve}));
const health = FolioConnections.runSimulatedTunnel('health');
const stop = FolioConnections.runSimulatedTunnel('stop');
assert.deepEqual(pending.map(item => item.path), [
  '/api/tunnel/simulated/health', '/api/tunnel/simulated/stop',
]);
assert.equal(pending[1].options.headers['X-Action-Intent'], 'tunnel-simulated-stop');
pending[1].resolve(response({tunnel: tunnelReport('stop', '模拟停止')}));
await stop;
pending[0].resolve(response({tunnel: tunnelReport('health', '模拟通过')}));
await health;
assert.equal(state.simulatedTunnel.state, '模拟停止');

pending.length = 0;
const secondStop = FolioConnections.runSimulatedTunnel('stop');
const refresh = FolioApp.loadStatus();
pending[0].resolve(response({tunnel: tunnelReport('stop', '模拟停止')}));
await secondStop;
pending[1].resolve(response({
  service: 'ready', csrf_token: 'new-csrf',
  tunnel_wizard: {simulation: tunnelReport('status', '模拟运行')},
}));
await refresh;
assert.equal(state.simulatedTunnel.state, '模拟停止', 'late status overwrote completed stop');
''')


if __name__ == "__main__":
    unittest.main()
