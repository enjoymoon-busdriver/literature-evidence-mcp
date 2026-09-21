const path = require('node:path');

const BACKEND_EXECUTABLE = 'foliohook-backend.exe';
const READY_LINE_LIMIT = 4096;
const STABLE_SHIM_DIRECTORY = 'literature-evidence-mcp';
const STABLE_SHIM_NAME = 'mcp-server.cmd';

const ALLOWED_EXTERNAL_HTTPS_ORIGINS = Object.freeze([
  'https://bailian.console.aliyun.com',
  'https://brew.sh',
  'https://developers.openai.com',
  'https://github.com',
  'https://help.aliyun.com',
  'https://learn.chatgpt.com',
  'https://platform.openai.com',
]);

function resolveBackendExecutable(resourcesPath, platform = process.platform) {
  if (platform !== 'win32') {
    throw new Error('FolioHook 桌面测试壳当前仅支持 Windows。');
  }
  if (!path.win32.isAbsolute(resourcesPath)) {
    throw new Error('Electron resources 路径不是 Windows 绝对路径。');
  }
  return path.win32.join(resourcesPath, 'backend', BACKEND_EXECUTABLE);
}

function resolveStableShimPath(localAppData) {
  if (!localAppData || !path.win32.isAbsolute(localAppData)) {
    throw new Error('LOCALAPPDATA 不可用，无法准备稳定 MCP 入口。');
  }
  return path.win32.join(localAppData, STABLE_SHIM_DIRECTORY, STABLE_SHIM_NAME);
}

function escapeBatchQuotedPath(targetPath) {
  if (!path.win32.isAbsolute(targetPath)) {
    throw new Error('MCP 后端路径必须是 Windows 绝对路径。');
  }
  if (/[\0\r\n"]/.test(targetPath)) {
    throw new Error('MCP 后端路径包含批处理不支持的字符。');
  }
  return targetPath.replaceAll('%', '%%');
}

function createMcpShimContent(backendExecutable) {
  const escapedExecutable = escapeBatchQuotedPath(backendExecutable);
  return [
    '@echo off',
    'setlocal DisableDelayedExpansion',
    'chcp 65001 >nul 2>nul',
    `"${escapedExecutable}" mcp`,
    'exit /b %ERRORLEVEL%',
    '',
  ].join('\r\n');
}

function parseReadyLine(line) {
  if (typeof line !== 'string' || Buffer.byteLength(line, 'utf8') > READY_LINE_LIMIT) {
    throw new Error('后端启动消息过长或格式无效。');
  }
  let payload;
  try {
    payload = JSON.parse(line);
  } catch {
    throw new Error('后端没有返回有效的启动 JSON。');
  }
  if (
    payload === null ||
    Array.isArray(payload) ||
    typeof payload !== 'object' ||
    Object.keys(payload).length !== 1 ||
    typeof payload.url !== 'string'
  ) {
    throw new Error('后端启动 JSON 必须只包含 url。');
  }
  const match = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})$/.exec(payload.url);
  if (!match) {
    throw new Error('后端没有返回受信任的 127.0.0.1 地址。');
  }
  const port = Number.parseInt(match[1], 10);
  if (port > 65535) {
    throw new Error('后端返回了无效端口。');
  }
  return payload.url;
}

function isSameBackendNavigation(targetUrl, backendUrl) {
  try {
    const target = new URL(targetUrl);
    const backend = new URL(backendUrl);
    return (
      target.protocol === 'http:' &&
      target.hostname === '127.0.0.1' &&
      target.origin === backend.origin &&
      target.username === '' &&
      target.password === ''
    );
  } catch {
    return false;
  }
}

function isAllowedExternalUrl(
  targetUrl,
  allowedOrigins = ALLOWED_EXTERNAL_HTTPS_ORIGINS,
) {
  try {
    const target = new URL(targetUrl);
    return (
      target.protocol === 'https:' &&
      target.username === '' &&
      target.password === '' &&
      allowedOrigins.includes(target.origin)
    );
  } catch {
    return false;
  }
}

function backendArguments(launchOptions) {
  const args = ['desktop'];
  if (launchOptions.smokeTest) {
    args.push('--application-root', launchOptions.applicationRoot);
  }
  return args;
}

function parseLaunchOptions(argv, platform = process.platform) {
  const smokeIndexes = [];
  const applicationRootIndexes = [];
  argv.forEach((item, index) => {
    if (item === '--smoke-test') smokeIndexes.push(index);
    if (item === '--application-root') applicationRootIndexes.push(index);
  });

  if (smokeIndexes.length === 0) {
    if (applicationRootIndexes.length > 0) {
      throw new Error('--application-root 仅可用于 --smoke-test。');
    }
    return { smokeTest: false, applicationRoot: null };
  }
  if (smokeIndexes.length !== 1 || applicationRootIndexes.length !== 1) {
    throw new Error('烟雾测试参数重复或缺失。');
  }
  const rootIndex = applicationRootIndexes[0];
  const applicationRoot = argv[rootIndex + 1];
  if (!applicationRoot || applicationRoot.startsWith('--')) {
    throw new Error('--application-root 缺少路径。');
  }
  const pathApi = platform === 'win32' ? path.win32 : path.posix;
  if (!pathApi.isAbsolute(applicationRoot)) {
    throw new Error('--application-root 必须是绝对路径。');
  }
  return { smokeTest: true, applicationRoot };
}

module.exports = {
  ALLOWED_EXTERNAL_HTTPS_ORIGINS,
  BACKEND_EXECUTABLE,
  READY_LINE_LIMIT,
  backendArguments,
  createMcpShimContent,
  escapeBatchQuotedPath,
  isAllowedExternalUrl,
  isSameBackendNavigation,
  parseLaunchOptions,
  parseReadyLine,
  resolveBackendExecutable,
  resolveStableShimPath,
};
