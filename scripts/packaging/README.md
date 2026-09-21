# Windows candidate build

Build on Windows x64, not with a copied Mac virtual environment. The candidate
uses Python 3.12.10, Node 24.15.0, the pinned build requirements, and the desktop
lockfile. The application runtime does not need Python or Node installed by the
recipient.

From the repository root in PowerShell:

```powershell
python -m venv .build-venv
.build-venv/Scripts/python -m pip --isolated install --index-url https://pypi.org/simple -r scripts/packaging/requirements-build.txt
.build-venv/Scripts/python -m pip --isolated install --no-build-isolation --no-deps .
.build-venv/Scripts/python -m unittest -v tests.test_windows_storage tests.test_desktop_runtime tests.test_stage1 tests.test_stage2_retrieval
.build-venv/Scripts/python -m PyInstaller --noconfirm --distpath local-artifacts/windows-package/dist --workpath local-artifacts/windows-package/build scripts/packaging/backend.spec
.build-venv/Scripts/python scripts/packaging/smoke_backend.py --backend local-artifacts/windows-package/dist/backend/foliohook-backend.exe
node desktop/test/windows-mcp-smoke.cjs --backend local-artifacts/windows-package/dist/backend/foliohook-backend.exe --python .build-venv/Scripts/python.exe
cd desktop
npm ci --no-audit --no-fund
npm test
npm run make -- --platform=win32 --arch=x64
```

Stop if any command fails. The setup program is under
`desktop/out/make/squirrel.windows/x64/`. `desktop/out` and the backend build
directory are ignored. Only package files from this build: never copy a real
application root, credentials, imported literature, or another project's runtime.

The Windows workflow is restricted to the development branch and requires
`[windows-build]` in the head commit message. Its `workflow_dispatch` entry
becomes usable only after a separately authorized merge to the default branch.
No merge or release is performed by the workflow. It uses a standard public
Windows Server runner, no dependency cache, and retains the setup artifact for
one day. Storage spending must be bounded by the account's existing zero-dollar
stop-usage budget before invoking artifact-producing runs.

PyInstaller copies installed runtime distribution metadata, including wheel
license files. Electron retains its bundled license notices; the application
keeps its Apache-2.0 license. Inspect the actual frozen output and installed
dependency versions when changing dependencies. No Tunnel executable or model
weights are included in this local-only candidate.

Before broader distribution, use the same artifact on a clean Windows 11 x64
standard-user account: install, import, search, connect a real local MCP client,
close/reopen, upgrade to another candidate, and uninstall while preserving data.
Cloud build/frozen smoke checks do not establish these interactive results.
