#!/usr/bin/env python3
"""Standard-library bootstrap for the Finder ``.command`` entry.

This file intentionally stays outside the installed package so it can check an
unprepared Python before any project dependency is installed.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Optional, Sequence, Tuple


PYTHON_DOWNLOAD_URL = "https://www.python.org/downloads/macos/"
MINIMUM_PYTHON = (3, 11)
DEFAULT_PORT = 8765
MARKER_NAME = ".literature-evidence-install.json"
MCP_SHIM_NAME = "mcp-server"
PYTHON_LINK_NAME = re.compile(r"python(?:3(?:\.\d+)?t?)?")
SETUPTOOLS_DISTUTILS_PTH = (
    "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; "
    "enabled = os.environ.get(var, 'local') == 'local'; "
    "enabled and __import__('_distutils_hack').add_shim();"
)


class LauncherError(RuntimeError):
    """A user-actionable launcher failure."""


class RuntimeFacts(NamedTuple):
    system: str
    machine: str
    python_version: Tuple[int, int, int]
    has_serialize: bool
    has_deserialize: bool
    sqlite_image_roundtrip: bool
    fts5_works: bool


class LauncherPaths(NamedTuple):
    project_root: Path
    venv: Path
    venv_python: Path
    application_root: Path
    marker: Path
    mcp_shim: Path


class PythonIdentity(NamedTuple):
    version: Tuple[int, int, int]
    prefix: str
    base_prefix: str


def inspect_runtime() -> RuntimeFacts:
    """Probe the actual interpreter without writing to disk."""
    has_serialize = False
    has_deserialize = False
    sqlite_image_roundtrip = False
    fts5_works = False
    database = None
    clone = None
    try:
        database = sqlite3.connect(":memory:")
        has_serialize = hasattr(database, "serialize")
        has_deserialize = hasattr(database, "deserialize")

        if has_serialize and has_deserialize:
            database.execute("CREATE TABLE launcher_roundtrip(value TEXT NOT NULL)")
            database.execute(
                "INSERT INTO launcher_roundtrip(value) VALUES (?)", ("ok",)
            )
            image = database.serialize()
            clone = sqlite3.connect(":memory:")
            clone.deserialize(image)
            sqlite_image_roundtrip = (
                clone.execute("SELECT value FROM launcher_roundtrip").fetchone()
                == ("ok",)
            )

        database.execute(
            "CREATE VIRTUAL TABLE launcher_fts USING fts5(content)"
        )
        database.execute(
            "INSERT INTO launcher_fts(content) VALUES (?)", ("local evidence",)
        )
        fts5_works = (
            database.execute(
                "SELECT content FROM launcher_fts WHERE launcher_fts MATCH ?",
                ("evidence",),
            ).fetchone()
            == ("local evidence",)
        )
    except (AttributeError, sqlite3.Error, TypeError, ValueError):
        # validate_runtime turns these capability flags into stable Chinese errors.
        pass
    finally:
        if clone is not None:
            clone.close()
        if database is not None:
            database.close()

    version = sys.version_info
    return RuntimeFacts(
        system=platform.system(),
        machine=platform.machine(),
        python_version=(version.major, version.minor, version.micro),
        has_serialize=has_serialize,
        has_deserialize=has_deserialize,
        sqlite_image_roundtrip=sqlite_image_roundtrip,
        fts5_works=fts5_works,
    )


def validate_runtime(facts: RuntimeFacts) -> None:
    if facts.system != "Darwin":
        raise LauncherError("此入口只支持 Apple Silicon macOS；当前系统不是 macOS。")
    if facts.machine != "arm64":
        raise LauncherError(
            "此入口只支持 Apple Silicon（arm64）；当前 Mac 不是 arm64 架构。"
        )
    if facts.python_version[:2] < MINIMUM_PYTHON:
        current = ".".join(str(part) for part in facts.python_version[:2])
        raise LauncherError(
            f"需要 Python 3.11 或更新版本；当前是 Python {current}。"
            f"请从 {PYTHON_DOWNLOAD_URL} 获取 macOS 安装包，安装后重新双击。"
        )
    if not facts.has_serialize or not facts.has_deserialize:
        raise LauncherError(
            "当前 Python 的 sqlite3 缺少 serialize/deserialize 能力，无法安全读取冻结快照。"
            f"请改用 {PYTHON_DOWNLOAD_URL} 提供的 Python 3.11 或更新版本。"
        )
    if not facts.sqlite_image_roundtrip:
        raise LauncherError(
            "当前 Python 的 sqlite3 无法完成 serialize/deserialize 内存往返。"
            f"请改用 {PYTHON_DOWNLOAD_URL} 提供的 Python 3.11 或更新版本。"
        )
    if not facts.fts5_works:
        raise LauncherError(
            "当前 Python 的 SQLite 缺少可用的 FTS5 全文检索能力。"
            f"请改用 {PYTHON_DOWNLOAD_URL} 提供的 Python 3.11 或更新版本。"
        )


def resolve_paths(project_root: Path, *, home: Optional[Path] = None) -> LauncherPaths:
    root = project_root.expanduser().resolve()
    user_home = (home if home is not None else Path.home()).expanduser().resolve()
    venv = root / ".venv"
    application_root = (
        user_home
        / "Library"
        / "Application Support"
        / "literature-evidence-mcp"
    )
    return LauncherPaths(
        project_root=root,
        venv=venv,
        venv_python=venv / "bin" / "python",
        application_root=application_root,
        marker=venv / MARKER_NAME,
        mcp_shim=application_root / MCP_SHIM_NAME,
    )


def validate_project(paths: LauncherPaths) -> None:
    required = (
        paths.project_root / "pyproject.toml",
        paths.project_root / "src" / "literature_evidence_mcp" / "cli.py",
        paths.project_root / "scripts" / "macos_launcher.py",
    )
    if not paths.project_root.is_dir() or any(not item.is_file() for item in required):
        raise LauncherError(
            "启动入口旁边缺少完整项目文件。请保留整个项目目录，不要只移动 .command 文件。"
        )


def source_fingerprint(project_root: Path) -> str:
    files = [project_root / "pyproject.toml"]
    package_root = project_root / "src" / "literature_evidence_mcp"
    files.extend(
        path
        for path in package_root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".html", ".css", ".js"}
    )
    digest = hashlib.sha256()
    try:
        for path in sorted(
            files,
            key=lambda item: item.relative_to(project_root).as_posix(),
        ):
            relative = path.relative_to(project_root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(path.read_bytes())
    except OSError as exc:
        raise LauncherError("无法读取本地项目文件，未开始安装。") from exc
    return digest.hexdigest()


def _run_command(
    command: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    try:
        completed = subprocess.run(
            list(command),
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise LauncherError("无法启动本地环境准备命令。") from exc
    return completed.returncode


def _isolated_python_env() -> dict:
    """Keep child Python processes independent of caller import/install settings."""
    blocked = {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "__PYVENV_LAUNCHER__",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked and not key.startswith("PIP_")
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _isolated_pip_env() -> dict:
    environment = _isolated_python_env()
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_NO_CACHE_DIR"] = "1"
    return environment


def _marker_payload(
    fingerprint: str,
    python_version: Tuple[int, int, int],
) -> dict:
    return {
        "format": 1,
        "python": list(python_version),
        "source_sha256": fingerprint,
    }


def _read_marker(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _installed_environment_is_current(
    paths: LauncherPaths,
    fingerprint: str,
    python_version: Tuple[int, int, int],
    *,
    runner: Callable[..., int] = _run_command,
) -> bool:
    if not paths.venv_python.is_file():
        return False
    if _read_marker(paths.marker) != _marker_payload(
        fingerprint,
        python_version,
    ):
        return False
    command = [
        os.fspath(paths.venv_python),
        "-I",
        "-c",
        (
            "import literature_evidence_mcp, mcp, pypdf, "
            "python_multipart, starlette, uvicorn"
        ),
    ]
    return (
        runner(
            command,
            cwd=paths.project_root,
            env=_isolated_python_env(),
        )
        == 0
    )


def _read_python_identity(python: Path) -> PythonIdentity:
    try:
        completed = subprocess.run(
            [
                os.fspath(python),
                "-I",
                "-c",
                (
                    "import json,sys; print(json.dumps({"
                    "'version': list(sys.version_info[:3]), "
                    "'prefix': sys.prefix, 'base_prefix': sys.base_prefix}))"
                ),
            ],
            env=_isolated_python_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise LauncherError("无法检查受控虚拟环境的 Python。") from exc
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LauncherError("无法识别受控虚拟环境的 Python 版本。") from exc
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or not isinstance(payload.get("version"), list)
        or len(payload["version"]) != 3
        or any(type(part) is not int or part < 0 for part in payload["version"])
        or not isinstance(payload.get("prefix"), str)
        or not payload["prefix"]
        or not isinstance(payload.get("base_prefix"), str)
        or not payload["base_prefix"]
    ):
        raise LauncherError("无法识别受控虚拟环境的 Python 版本。")
    return PythonIdentity(
        version=(payload["version"][0], payload["version"][1], payload["version"][2]),
        prefix=payload["prefix"],
        base_prefix=payload["base_prefix"],
    )


def _managed_runtime_version(
    paths: LauncherPaths,
    *,
    runner: Callable[..., int],
    identity_reader: Callable[[Path], PythonIdentity],
) -> Tuple[int, int, int]:
    status = runner(
        [
            os.fspath(paths.venv_python),
            "-I",
            os.fspath(paths.project_root / "scripts" / "macos_launcher.py"),
            "--check-runtime",
        ],
        cwd=paths.project_root,
        env=_isolated_python_env(),
    )
    if status != 0:
        raise LauncherError(
            "项目内 .venv 的 Python 不满足 Python 3.11+、"
            "SQLite serialize/deserialize 和 FTS5 要求。"
            "请移走该 .venv 目录后重新双击。"
        )
    identity = identity_reader(paths.venv_python)
    expected_prefix = paths.venv.resolve()
    actual_prefix = Path(identity.prefix).resolve()
    actual_base_prefix = Path(identity.base_prefix).resolve()
    if actual_prefix != expected_prefix or actual_prefix == actual_base_prefix:
        raise LauncherError(
            "项目内的 .venv 不是完整隔离的 Python 虚拟环境。"
            "请移走该 .venv 目录后重新双击；未执行 pip。"
        )
    return identity.version


def _validate_venv_configuration(paths: LauncherPaths) -> None:
    configuration = paths.venv / "pyvenv.cfg"
    try:
        lines = configuration.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LauncherError(
            "项目内的 .venv 缺少可读取的 pyvenv.cfg。"
            "请移走该 .venv 目录后重新双击；未执行 pip。"
        ) from exc
    values = []
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "include-system-site-packages":
            values.append(value.strip().lower())
    if values != ["false"]:
        raise LauncherError(
            "项目内的 .venv 必须明确禁用系统 site-packages。"
            "请移走该 .venv 目录后重新双击；未执行 pip。"
        )


def _validate_pth_file(path: Path, expected_root: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LauncherError(
            "项目内 .venv 的 .pth 导入配置无法安全读取；未执行虚拟环境 Python。"
        ) from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("import ", "import\t")):
            if (
                path.name == "distutils-precedence.pth"
                and stripped == SETUPTOOLS_DISTUTILS_PTH
            ):
                continue
            raise LauncherError(
                "项目内 .venv 含有会执行代码的 .pth 导入配置。"
                "请移走该 .venv 目录后重新双击；未执行虚拟环境 Python。"
            )
        configured = Path(stripped)
        if not configured.is_absolute():
            configured = path.parent / configured
        try:
            resolved = configured.resolve()
        except (OSError, RuntimeError) as exc:
            raise LauncherError(
                "项目内 .venv 的 .pth 导入路径无法安全解析；"
                "未执行虚拟环境 Python。"
            ) from exc
        if resolved != expected_root and expected_root not in resolved.parents:
            raise LauncherError(
                "项目内 .venv 的 .pth 导入路径指向项目外部。"
                "请移走该 .venv 目录后重新双击；未执行虚拟环境 Python。"
            )


def _validate_managed_tree_before_python(paths: LauncherPaths) -> None:
    """Reject existing install-tree redirects before running managed Python."""
    expected_root = paths.venv.resolve()
    bin_directory = paths.venv / "bin"
    library_directory = paths.venv / "lib"
    for directory in (bin_directory, library_directory):
        if directory.is_symlink() or not directory.is_dir():
            raise LauncherError(
                "项目内的 .venv 安装目录不完整或使用了符号链接。"
                "请移走该 .venv 目录后重新双击；未执行虚拟环境 Python。"
            )

    try:
        bin_entries = tuple(bin_directory.iterdir())
    except OSError as exc:
        raise LauncherError(
            "无法安全检查项目内 .venv/bin；未执行虚拟环境 Python。"
        ) from exc
    for entry in bin_entries:
        if entry.is_symlink() and PYTHON_LINK_NAME.fullmatch(entry.name) is None:
            raise LauncherError(
                "项目内 .venv/bin 含有非标准的符号链接。"
                "请移走该 .venv 目录后重新双击；未执行虚拟环境 Python。"
            )

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        for current, directories, files in os.walk(
            library_directory,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            current_path = Path(current)
            for name in tuple(directories) + tuple(files):
                candidate = current_path / name
                if candidate.is_symlink():
                    raise LauncherError(
                        "项目内 .venv/lib 含有符号链接。"
                        "请移走该 .venv 目录后重新双击；"
                        "未执行虚拟环境 Python。"
                    )
    except OSError as exc:
        raise LauncherError(
            "无法安全检查项目内 .venv/lib；未执行虚拟环境 Python。"
        ) from exc
    try:
        site_directories = [
            candidate
            for candidate in library_directory.glob("python*/site-packages")
            if candidate.is_dir()
        ]
    except OSError as exc:
        raise LauncherError(
            "无法安全定位项目内 .venv 的 site-packages；"
            "未执行虚拟环境 Python。"
        ) from exc
    if len(site_directories) != 1:
        raise LauncherError(
            "项目内的 .venv 缺少唯一的 site-packages 目录。"
            "请移走该 .venv 目录后重新双击；未执行虚拟环境 Python。"
        )
    for pth_file in site_directories[0].glob("*.pth"):
        _validate_pth_file(pth_file, expected_root)


def _validate_install_paths(
    paths: LauncherPaths,
    python_version: Tuple[int, int, int],
) -> None:
    python_library = paths.venv / "lib" / (
        f"python{python_version[0]}.{python_version[1]}"
    )
    candidates = (
        paths.venv / "bin",
        paths.venv / "lib",
        python_library,
        python_library / "site-packages",
    )
    expected_root = paths.venv.resolve()
    for candidate in candidates:
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise LauncherError(
                "项目内的 .venv 安装目录无法安全解析；未执行 pip。"
            ) from exc
        if resolved != expected_root and expected_root not in resolved.parents:
            raise LauncherError(
                "项目内的 .venv 安装目录指向项目外部；未执行 pip。"
                "请移走该 .venv 目录后重新双击。"
            )


def _write_marker(path: Path, payload: dict) -> None:
    descriptor = None
    temporary = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=os.fspath(path.parent),
        )
        temporary = Path(raw_temporary)
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = None
        with stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(os.fspath(temporary), os.fspath(path))
        temporary = None
    except OSError as exc:
        raise LauncherError("环境已经安装，但无法记录安装状态。") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def prepare_environment(
    paths: LauncherPaths,
    *,
    runner: Callable[..., int] = _run_command,
    identity_reader: Callable[[Path], PythonIdentity] = _read_python_identity,
) -> bool:
    """Create/update the controlled non-editable environment.

    Returns True when this invocation ran pip, otherwise False.
    """
    validate_project(paths)
    fingerprint = source_fingerprint(paths.project_root)

    if paths.venv.is_symlink():
        raise LauncherError("项目中的 .venv 不能是符号链接；未写入该位置。")
    if paths.venv.exists() and not paths.venv.is_dir():
        raise LauncherError("项目中的 .venv 不是目录；请移走它后重试。")
    if not paths.venv.exists():
        print("首次运行：正在项目内创建受控虚拟环境 .venv（不需要 sudo）。")
        status = runner(
            [
                sys.executable,
                "-I",
                "-m",
                "venv",
                os.fspath(paths.venv),
            ],
            cwd=paths.project_root,
            env=_isolated_python_env(),
        )
        if status != 0 or not paths.venv_python.is_file():
            raise LauncherError(
                "无法创建项目内的 .venv。请确认项目目录可写，并确认该 Python 带有 venv。"
            )
    elif not paths.venv_python.is_file():
        raise LauncherError(
            "项目内的 .venv 不完整。请移走这个 .venv 目录后重新双击。"
        )

    _validate_venv_configuration(paths)
    _validate_managed_tree_before_python(paths)
    managed_version = _managed_runtime_version(
        paths,
        runner=runner,
        identity_reader=identity_reader,
    )
    _validate_install_paths(paths, managed_version)
    if _installed_environment_is_current(
        paths,
        fingerprint,
        managed_version,
        runner=runner,
    ):
        return False

    print(
        "正在安装当前本地项目及其免费开源依赖；"
        "首次运行可能需要从默认 PyPI 软件包索引下载。"
    )
    print("不会发送文献、调用云模型或付费 API。")
    install_env = _isolated_pip_env()
    status = runner(
        [
            os.fspath(paths.venv_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--prefix",
            os.fspath(paths.venv),
            os.fspath(paths.project_root),
        ],
        cwd=paths.project_root,
        env=install_env,
    )
    if status != 0:
        raise LauncherError(
            "本地项目或依赖安装失败，已停止且未启动管理页。"
            "请检查网络是否能访问默认 PyPI 软件包索引，然后重新双击。"
        )

    _validate_managed_tree_before_python(paths)
    verify_command = [
        os.fspath(paths.venv_python),
        "-I",
        "-c",
        (
            "import literature_evidence_mcp, mcp, pypdf, "
            "python_multipart, starlette, uvicorn"
        ),
    ]
    if (
        runner(
            verify_command,
            cwd=paths.project_root,
            env=_isolated_python_env(),
        )
        != 0
    ):
        raise LauncherError("安装完成后无法导入本地项目，已停止且未启动管理页。")

    _write_marker(
        paths.marker,
        _marker_payload(fingerprint, managed_version),
    )
    return True


def _open_application_root(path: Path, *, create: bool) -> int:
    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise LauncherError("无法确定应用根目录；未写入该位置。") from exc
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = None
    try:
        descriptor = os.open(absolute.anchor, flags)
        for part in absolute.parts[1:]:
            try:
                status = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise LauncherError("应用根尚未安全准备；未写入该位置。")
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                status = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise LauncherError(
                    "应用根路径不能经过符号链接或非目录；未写入该位置。"
                )
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        result = descriptor
        descriptor = None
        return result
    except LauncherError:
        raise
    except OSError as exc:
        raise LauncherError("无法安全创建应用根目录，请检查用户目录写入权限。") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def ensure_application_root(path: Path) -> None:
    descriptor = _open_application_root(path, create=True)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise LauncherError("无法同步应用根目录；未继续启动。") from exc
    finally:
        os.close(descriptor)


def _mcp_shim_text(paths: LauncherPaths) -> str:
    command = (
        f"exec {shlex.quote(os.fspath(paths.venv_python))} -I -B -m "
        "literature_evidence_mcp.mcp_server --application-root "
        f"{shlex.quote(os.fspath(paths.application_root))}"
    )
    return f"#!/bin/zsh -f\n{command}\n"


def install_mcp_shim(paths: LauncherPaths) -> None:
    """Atomically install the fixed local MCP entry inside the application root."""
    root_descriptor = _open_application_root(paths.application_root, create=False)
    temporary_name = None
    temporary_descriptor = None
    try:
        try:
            current = os.stat(
                MCP_SHIM_NAME,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None
        if current is not None and (
            stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
        ):
            raise LauncherError(
                "本地 MCP 启动入口已存在但不是普通文件；未覆盖该位置。"
            )

        payload = _mcp_shim_text(paths).encode("utf-8")
        for _attempt in range(8):
            candidate = f".{MCP_SHIM_NAME}.{secrets.token_hex(8)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                temporary_descriptor = os.open(
                    candidate,
                    flags,
                    0o700,
                    dir_fd=root_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_descriptor is None or temporary_name is None:
            raise LauncherError("无法创建本地 MCP 启动入口临时文件。")

        os.fchmod(temporary_descriptor, 0o700)
        view = memoryview(payload)
        while view:
            written = os.write(temporary_descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None

        try:
            current = os.stat(
                MCP_SHIM_NAME,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None
        if current is not None and (
            stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
        ):
            raise LauncherError(
                "本地 MCP 启动入口已改变为非普通文件；未覆盖该位置。"
            )

        os.replace(
            temporary_name,
            MCP_SHIM_NAME,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        temporary_name = None
        os.fsync(root_descriptor)
    except LauncherError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LauncherError("无法安全安装本地 MCP 启动入口。") from exc
    finally:
        if temporary_descriptor is not None:
            try:
                os.close(temporary_descriptor)
            except OSError:
                pass
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=root_descriptor)
            except OSError:
                pass
        os.close(root_descriptor)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("端口必须是 1024-65535 的整数。") from exc
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 1024-65535 的整数。")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查并启动 Apple Silicon macOS 本机文献证据管理页。"
    )
    parser.add_argument("project_root", nargs="?", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, help=argparse.SUPPRESS)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="只检查运行环境和路径，不创建文件、不安装、不启动。",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="准备受控环境和资料库后退出，不启动管理页。",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动服务但不自动打开浏览器（仅用于自动验收）。",
    )
    parser.add_argument("--check-runtime", action="store_true", help=argparse.SUPPRESS)
    return parser


def _print_paths(paths: LauncherPaths) -> None:
    print(f"项目目录：{paths.project_root}")
    print(f"受控虚拟环境：{paths.venv}")
    print(f"多资料库应用根：{paths.application_root}")


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    validate_runtime(inspect_runtime())
    if args.check_runtime:
        print("运行环境符合要求。")
        return 0
    if args.project_root is None:
        raise LauncherError("缺少项目目录，无法确定要安装和启动的本地项目。")

    paths = resolve_paths(args.project_root)
    validate_project(paths)
    if args.preflight_only:
        print("预检通过；未创建文件、未安装、未启动管理页。")
        _print_paths(paths)
        return 0

    prepare_environment(paths)
    ensure_application_root(paths.application_root)
    install_mcp_shim(paths)
    if args.prepare_only:
        print("环境和多资料库应用根已准备；按要求没有启动管理页。")
        _print_paths(paths)
        return 0

    print(f"多资料库应用根：{paths.application_root}")
    command = [
        os.fspath(paths.venv_python),
        "-I",
        "-m",
        "literature_evidence_mcp",
        "serve",
        "--application-root",
        os.fspath(paths.application_root),
        "--port",
        str(args.port),
    ]
    if not args.no_browser:
        command.append("--open-browser")
    try:
        os.execve(
            os.fspath(paths.venv_python),
            command,
            _isolated_python_env(),
        )
    except OSError as exc:
        raise LauncherError("环境已准备，但无法启动本机管理页。") from exc
    return 0  # pragma: no cover - successful execve never returns


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(argv)
    except LauncherError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("已停止。\n")
        return 130
    except Exception:
        sys.stderr.write("错误：启动准备遇到未预期的本地错误，已安全停止。\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
