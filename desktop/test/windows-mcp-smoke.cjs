#!/usr/bin/env node

const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const {
  createMcpShimContent,
  resolveStableShimPath,
} = require('../src/runtime.cjs');

const OUTPUT_LIMIT = 16 * 1024;

class DriverFailure extends Error {
  constructor(report) {
    super(report.error || 'MCP smoke driver failed');
    this.report = report;
  }
}

function parseArguments(argv) {
  const result = { backend: null, python: null };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === '--backend' || item === '--python') {
      const key = item.slice(2);
      if (result[key] !== null || !argv[index + 1]) {
        throw new Error(`invalid ${item} argument`);
      }
      result[key] = argv[index + 1];
      index += 1;
    } else {
      throw new Error(`unknown argument: ${item}`);
    }
  }
  if (!result.backend || !result.python) {
    throw new Error('usage: windows-mcp-smoke.cjs --backend <exe> --python <python.exe>');
  }
  return result;
}

function appendBounded(current, chunk) {
  const combined = current + chunk.toString('utf8');
  if (Buffer.byteLength(combined, 'utf8') > OUTPUT_LIMIT) {
    throw new Error('driver output exceeded the smoke-test limit');
  }
  return combined;
}

function runDriver(python, driver, shim, cwd, environment) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      python,
      [driver, '--shim', shim, '--cwd', cwd],
      {
        cwd,
        env: environment,
        shell: false,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    );
    let stdout = '';
    let stderr = '';
    let finished = false;
    const timer = setTimeout(() => {
      if (!finished) child.kill();
    }, 90_000);
    child.stdout.on('data', (chunk) => {
      try {
        stdout = appendBounded(stdout, chunk);
      } catch (error) {
        child.kill();
        reject(error);
      }
    });
    child.stderr.on('data', (chunk) => {
      try {
        stderr = appendBounded(stderr, chunk);
      } catch (error) {
        child.kill();
        reject(error);
      }
    });
    child.once('error', () => reject(new Error('could not start the MCP smoke driver')));
    child.once('exit', (code, signal) => {
      finished = true;
      clearTimeout(timer);
      if (signal) {
        reject(new Error('MCP smoke driver timed out or was terminated'));
      } else if (code !== 0) {
        try {
          const lines = stderr.trim().split(/\r?\n/).filter(Boolean);
          const report = JSON.parse(lines.at(-1));
          reject(new DriverFailure(report));
        } catch (error) {
          if (error instanceof DriverFailure) reject(error);
          else reject(new Error(stderr.trim() || `MCP smoke driver exited ${code}`));
        }
      } else {
        resolve(stdout.trim());
      }
    });
  });
}

async function main() {
  if (process.platform !== 'win32') {
    throw new Error('windows-mcp-smoke.cjs must run on Windows');
  }
  const options = parseArguments(process.argv.slice(2));
  const sourceExecutable = path.resolve(options.backend);
  const python = path.resolve(options.python);
  const sourceDirectory = path.dirname(sourceExecutable);
  const executableName = path.basename(sourceExecutable);
  const [backendStat, pythonStat] = await Promise.all([
    fs.stat(sourceExecutable).catch(() => null),
    fs.stat(python).catch(() => null),
  ]);
  if (!backendStat?.isFile() || !pythonStat?.isFile()) {
    throw new Error('backend or Python driver executable is missing');
  }

  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'foliohook-cmd-smoke-'));
  try {
    const unusualRoot = path.join(temporary, '本地 数据!');
    const copiedBackendDirectory = path.join(unusualRoot, '应用 后端!');
    const isolatedLocalAppData = path.join(unusualRoot, 'Local App Data!');
    const isolatedTemp = path.join(unusualRoot, 'Temp!');
    await fs.mkdir(unusualRoot, { recursive: true });
    await fs.cp(sourceDirectory, copiedBackendDirectory, { recursive: true });
    await fs.mkdir(isolatedTemp, { recursive: true });

    const copiedExecutable = path.join(copiedBackendDirectory, executableName);
    const shim = resolveStableShimPath(isolatedLocalAppData);
    await fs.mkdir(path.dirname(shim), { recursive: true });
    await fs.writeFile(shim, createMcpShimContent(copiedExecutable), 'utf8');

    const environment = {
      LOCALAPPDATA: isolatedLocalAppData,
      TEMP: isolatedTemp,
      TMP: isolatedTemp,
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
    };
    for (const name of ['ComSpec', 'PATH', 'SystemRoot', 'WINDIR']) {
      if (process.env[name]) environment[name] = process.env[name];
    }
    const driver = path.join(__dirname, 'windows-mcp-driver.py');
    const output = await runDriver(python, driver, shim, unusualRoot, environment);
    const report = JSON.parse(output);
    if (report.passed !== true || report.tool_count !== 8 || report.library_count !== 0) {
      throw new Error('MCP smoke driver returned an invalid success report');
    }
    const applicationRootEntries = (await fs.readdir(path.dirname(shim))).sort();
    if (
      applicationRootEntries.length !== 1 ||
      applicationRootEntries[0] !== path.basename(shim)
    ) {
      throw new Error('read-only MCP smoke unexpectedly changed the application root');
    }
    process.stdout.write(`${JSON.stringify(report)}\n`);
  } finally {
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

main().catch((error) => {
  const report = error instanceof DriverFailure
    ? error.report
    : { passed: false, error: error.message };
  process.stderr.write(`${JSON.stringify(report)}\n`);
  process.exitCode = 1;
});
