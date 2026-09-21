const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { app, BrowserWindow, dialog, session, shell } = require('electron');

const squirrelStartup = require('electron-squirrel-startup');
const {
  ALLOWED_EXTERNAL_HTTPS_ORIGINS,
  READY_LINE_LIMIT,
  backendArguments,
  createMcpShimContent,
  isAllowedExternalUrl,
  isSameBackendNavigation,
  parseLaunchOptions,
  parseReadyLine,
  resolveBackendExecutable,
  resolveStableShimPath,
} = require('./runtime.cjs');

const BACKEND_START_TIMEOUT_MS = 30_000;
const BACKEND_STOP_TIMEOUT_MS = 5_000;

let backendChild = null;
let backendStopping = false;
let mainWindow = null;
let exitStarted = false;

async function writeStableMcpShim(backendExecutable) {
  const destination = resolveStableShimPath(process.env.LOCALAPPDATA);
  const destinationDirectory = path.win32.dirname(destination);
  const temporary = path.win32.join(
    destinationDirectory,
    `${path.win32.basename(destination)}.tmp-${process.pid}-${crypto.randomUUID()}`,
  );
  await fs.mkdir(destinationDirectory, { recursive: true });
  try {
    await fs.writeFile(temporary, createMcpShimContent(backendExecutable), {
      encoding: 'utf8',
      flag: 'wx',
    });
    await fs.rename(temporary, destination);
  } catch (error) {
    await fs.rm(temporary, { force: true }).catch(() => {});
    throw error;
  }
}

function waitForBackendReady(child) {
  return new Promise((resolve, reject) => {
    let stdout = Buffer.alloc(0);
    let settled = false;
    const timer = setTimeout(() => {
      finish(new Error('等待本机后端启动超时。'));
    }, BACKEND_START_TIMEOUT_MS);

    function cleanup() {
      clearTimeout(timer);
      child.stdout.off('data', onData);
      child.off('error', onError);
      child.off('exit', onExit);
    }

    function finish(error, value) {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve(value);
    }

    function onData(chunk) {
      stdout = Buffer.concat([stdout, chunk]);
      if (stdout.length > READY_LINE_LIMIT) {
        finish(new Error('后端启动消息超过允许长度。'));
        return;
      }
      const newline = stdout.indexOf(0x0a);
      if (newline < 0) return;
      const line = stdout.subarray(0, newline).toString('utf8').replace(/\r$/, '');
      const remainder = stdout.subarray(newline + 1).toString('utf8');
      if (remainder.trim() !== '') {
        finish(new Error('后端启动时返回了多余的标准输出。'));
        return;
      }
      try {
        finish(null, parseReadyLine(line));
      } catch (error) {
        finish(error);
      }
    }

    function onError(error) {
      finish(new Error('无法启动本机后端进程。'));
    }

    function onExit(code, signal) {
      const detail = signal ? `信号 ${signal}` : `退出码 ${code}`;
      finish(new Error(`本机后端在页面就绪前退出（${detail}）。`));
    }

    child.stdout.on('data', onData);
    child.once('error', onError);
    child.once('exit', onExit);
  });
}

async function startBackend(launchOptions) {
  const backendExecutable = resolveBackendExecutable(process.resourcesPath);
  const stat = await fs.stat(backendExecutable).catch(() => null);
  if (!stat || !stat.isFile()) {
    throw new Error('安装内容不完整：没有找到 FolioHook 本机后端。请重新安装测试包。');
  }
  if (!launchOptions.smokeTest) {
    try {
      await writeStableMcpShim(backendExecutable);
    } catch {
      throw new Error(
        '无法更新稳定 MCP 入口。请先关闭正在使用 FolioHook MCP 的客户端，再重新启动应用。',
      );
    }
  }

  let child;
  try {
    child = spawn(backendExecutable, backendArguments(launchOptions), {
      shell: false,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch {
    throw new Error('无法启动本机后端进程。');
  }
  backendChild = child;
  child.stderr.resume();
  const url = await waitForBackendReady(child);
  if (child.exitCode !== null || child.signalCode !== null) {
    throw new Error('本机后端在页面加载前停止。');
  }
  return { child, url };
}

async function stopBackendChild(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  backendStopping = true;
  const exited = once(child, 'exit').then(() => true);
  if (child.stdin && !child.stdin.destroyed) child.stdin.end();
  const graceful = await Promise.race([
    exited,
    new Promise((resolve) => setTimeout(() => resolve(false), BACKEND_STOP_TIMEOUT_MS)),
  ]);
  if (!graceful && child.exitCode === null && child.signalCode === null) {
    child.kill();
    await Promise.race([
      exited,
      new Promise((resolve) => setTimeout(resolve, 2_000)),
    ]);
  }
}

function openApprovedExternal(url) {
  if (!isAllowedExternalUrl(url, ALLOWED_EXTERNAL_HTTPS_ORIGINS)) return;
  void shell.openExternal(url).catch(() => {
    dialog.showErrorBox('无法打开链接', '系统没有成功打开这个外部 HTTPS 链接。');
  });
}

function installNavigationPolicy(window, backendUrl) {
  const handleNavigation = (event, url) => {
    if (isSameBackendNavigation(url, backendUrl)) return;
    event.preventDefault();
    openApprovedExternal(url);
  };
  window.webContents.on('will-navigate', handleNavigation);
  window.webContents.on('will-redirect', handleNavigation);
  window.webContents.setWindowOpenHandler(({ url }) => {
    openApprovedExternal(url);
    return { action: 'deny' };
  });
  window.webContents.on('will-attach-webview', (event) => event.preventDefault());
}

function createMainWindow(backendUrl, launchOptions) {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 680,
    show: false,
    backgroundColor: '#f8fafc',
    icon: path.join(process.resourcesPath, 'icon.png'),
    webPreferences: {
      allowRunningInsecureContent: false,
      contextIsolation: true,
      experimentalFeatures: false,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
    },
  });
  installNavigationPolicy(window, backendUrl);
  window.once('ready-to-show', () => {
    if (!launchOptions.smokeTest) window.show();
  });
  window.webContents.once('did-finish-load', () => {
    if (launchOptions.smokeTest) void requestExit(0);
  });
  window.webContents.on('render-process-gone', () => {
    if (!exitStarted) {
      dialog.showErrorBox('FolioHook 页面已停止', '桌面页面进程意外停止，应用将关闭。');
      void requestExit(1);
    }
  });
  void window.loadURL(backendUrl).catch(() => {
    if (!exitStarted) {
      dialog.showErrorBox('无法加载 FolioHook', '本机页面加载失败，应用将关闭。');
      void requestExit(1);
    }
  });
  return window;
}

async function requestExit(code) {
  if (exitStarted) return;
  exitStarted = true;
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.hide();
  await stopBackendChild(backendChild);
  app.exit(code);
}

async function run() {
  let launchOptions;
  try {
    launchOptions = parseLaunchOptions(process.argv.slice(1));
  } catch (error) {
    dialog.showErrorBox('FolioHook 启动参数错误', error.message);
    app.exit(2);
    return;
  }

  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => {
    callback(false);
  });
  session.defaultSession.setPermissionCheckHandler(() => false);

  try {
    const backend = await startBackend(launchOptions);
    backend.child.once('exit', (code, signal) => {
      if (backendStopping || exitStarted) return;
      const detail = signal ? `信号 ${signal}` : `退出码 ${code}`;
      dialog.showErrorBox(
        'FolioHook 后端已停止',
        `本机后端意外停止（${detail}）。应用将关闭。`,
      );
      void requestExit(1);
    });
    mainWindow = createMainWindow(backend.url, launchOptions);
  } catch (error) {
    dialog.showErrorBox('FolioHook 无法启动', error.message);
    await stopBackendChild(backendChild);
    app.exit(1);
  }
}

if (squirrelStartup) {
  app.quit();
} else {
  app.setAppUserModelId('com.squirrel.FolioHook.FolioHook');
  const singleInstance = app.requestSingleInstanceLock();
  if (!singleInstance) {
    app.quit();
  } else {
    app.on('second-instance', () => {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    });
    app.on('before-quit', (event) => {
      if (!exitStarted && backendChild) {
        event.preventDefault();
        void requestExit(0);
      }
    });
    app.on('window-all-closed', () => {
      void requestExit(0);
    });
    app.whenReady().then(run).catch(() => {
      dialog.showErrorBox('FolioHook 无法启动', '桌面运行环境未能就绪。');
      void requestExit(1);
    });
  }
}
