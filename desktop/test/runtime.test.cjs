const test = require('node:test');
const assert = require('node:assert/strict');

const {
  backendArguments,
  createMcpShimContent,
  isAllowedExternalUrl,
  isSameBackendNavigation,
  parseLaunchOptions,
  parseReadyLine,
  resolveBackendExecutable,
  resolveStableShimPath,
} = require('../src/runtime.cjs');

test('resolves packaged backend and stable MCP paths', () => {
  assert.equal(
    resolveBackendExecutable('C:\\Users\\Ada\\AppData\\Local\\FolioHook\\resources', 'win32'),
    'C:\\Users\\Ada\\AppData\\Local\\FolioHook\\resources\\backend\\foliohook-backend.exe',
  );
  assert.equal(
    resolveStableShimPath('C:\\Users\\Ada\\AppData\\Local'),
    'C:\\Users\\Ada\\AppData\\Local\\literature-evidence-mcp\\mcp-server.cmd',
  );
});

test('creates a quoted MCP shim without forwarding caller arguments', () => {
  const content = createMcpShimContent(
    'C:\\用户资料\\A% B!&(x)^\\app-0.1.0\\resources\\backend\\foliohook-backend.exe',
  );
  assert.match(content, /setlocal DisableDelayedExpansion/);
  assert.match(content, /chcp 65001 >nul 2>nul/);
  assert.match(content, /"C:\\用户资料\\A%% B!&\(x\)\^\\app-0\.1\.0/);
  assert.match(content, /" mcp\r\n/);
  assert.doesNotMatch(content, /%\*|%1/);
});

test('accepts only the exact loopback ready payload', () => {
  assert.equal(
    parseReadyLine('{"url":"http://127.0.0.1:49152"}'),
    'http://127.0.0.1:49152',
  );
  for (const invalid of [
    '{"url":"http://localhost:49152"}',
    '{"url":"http://127.0.0.1:49152/"}',
    '{"url":"https://127.0.0.1:49152"}',
    '{"url":"http://127.0.0.1:70000"}',
    '{"url":"http://127.0.0.1:49152","other":true}',
  ]) {
    assert.throws(() => parseReadyLine(invalid));
  }
});

test('allows only the backend origin inside the Electron window', () => {
  const backend = 'http://127.0.0.1:51234';
  assert.equal(isSameBackendNavigation(`${backend}/api/status`, backend), true);
  assert.equal(isSameBackendNavigation('http://127.0.0.1:51235/', backend), false);
  assert.equal(isSameBackendNavigation('file:///C:/Windows/System32/calc.exe', backend), false);
  assert.equal(isSameBackendNavigation('https://example.com/', backend), false);
});

test('external opening is HTTPS and explicit-origin only', () => {
  const allowed = ['https://docs.example.test'];
  assert.equal(isAllowedExternalUrl('https://docs.example.test/help?q=1', allowed), true);
  assert.equal(isAllowedExternalUrl('http://docs.example.test/help', allowed), false);
  assert.equal(isAllowedExternalUrl('https://evil.example/help', allowed), false);
  assert.equal(isAllowedExternalUrl('file:///C:/temp/file.txt', allowed), false);
  assert.equal(isAllowedExternalUrl('https://user@docs.example.test/help', allowed), false);
});

test('smoke-test application root is opt-in and absolute', () => {
  assert.deepEqual(parseLaunchOptions(['app.exe'], 'win32'), {
    smokeTest: false,
    applicationRoot: null,
  });
  const options = parseLaunchOptions(
    ['app.exe', '--smoke-test', '--application-root', 'D:\\ci temp\\app'],
    'win32',
  );
  assert.deepEqual(options, {
    smokeTest: true,
    applicationRoot: 'D:\\ci temp\\app',
  });
  assert.deepEqual(backendArguments(options), [
    'desktop',
    '--application-root',
    'D:\\ci temp\\app',
  ]);
  assert.throws(() => parseLaunchOptions(['--application-root', 'D:\\data'], 'win32'));
  assert.throws(() => parseLaunchOptions(['--smoke-test', '--application-root', 'relative'], 'win32'));
});
