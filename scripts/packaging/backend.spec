# Build on the target Windows x64 host, from a fresh non-editable environment.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parents[1]
datas = collect_data_files("literature_evidence_mcp")
datas += copy_metadata("literature-evidence-mcp", recursive=True)
datas += copy_metadata("pyinstaller")  # Bootloader license and distribution exception.
datas += [(str(root / "LICENSE"), "licenses/FolioHook")]
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if sys.platform == "win32":
    if not python_license.is_file():
        raise SystemExit("Windows Python distribution license is missing")
    datas += [(str(python_license), "licenses/Python")]

a = Analysis(
    [str(root / "scripts/packaging/backend_entry.py")],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("mcp"),
    hookspath=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="foliohook-backend", console=True, debug=False,
    strip=False, upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="backend")
