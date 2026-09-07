from __future__ import annotations

import importlib.util
import http.client
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import uvicorn

from literature_evidence_mcp.cli import main as cli_main
from literature_evidence_mcp.errors import ImportPolicyError
from literature_evidence_mcp.web import serve_local


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = ROOT / "scripts" / "macos_launcher.py"
COMMAND_PATH = ROOT / "启动文献证据管理页.command"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("stage4_macos_launcher", LAUNCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


def _supported_facts(**changes):
    values = {
        "system": "Darwin",
        "machine": "arm64",
        "python_version": (3, 12, 1),
        "has_serialize": True,
        "has_deserialize": True,
        "sqlite_image_roundtrip": True,
        "fts5_works": True,
    }
    values.update(changes)
    return launcher.RuntimeFacts(**values)


def _identity_for(paths, version=(3, 12, 1)):
    return launcher.PythonIdentity(
        version=version,
        prefix=os.fspath(paths.venv.resolve()),
        base_prefix=os.fspath((paths.venv.parent / "base-python").resolve()),
    )


def _is_venv_create(argv):
    return list(argv[1:4]) == ["-I", "-m", "venv"]


def _is_pip_install(argv):
    return list(argv[1:5]) == ["-I", "-m", "pip", "install"]


def _make_fake_venv(paths) -> None:
    paths.venv_python.parent.mkdir(parents=True)
    paths.venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    paths.venv_python.chmod(0o755)
    (paths.venv / "pyvenv.cfg").write_text(
        "include-system-site-packages = false\n",
        encoding="utf-8",
    )
    (paths.venv / "lib" / "python3.12" / "site-packages").mkdir(
        parents=True
    )


def _make_minimal_project(root: Path) -> None:
    (root / "scripts").mkdir(parents=True)
    (root / "src" / "literature_evidence_mcp").mkdir(parents=True)
    shutil.copy2(LAUNCHER_PATH, root / "scripts" / "macos_launcher.py")
    shutil.copy2(COMMAND_PATH, root / COMMAND_PATH.name)
    (root / "pyproject.toml").write_text(
        "[project]\nname='literature-evidence-mcp'\nversion='0.0.0'\n",
        encoding="utf-8",
    )
    (root / "src" / "literature_evidence_mcp" / "cli.py").write_text(
        "# launcher path fixture\n", encoding="utf-8"
    )


def _unused_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class StageFourPreflightTests(unittest.TestCase):
    def test_real_runtime_probe_exercises_sqlite_image_and_fts5(self) -> None:
        facts = launcher.inspect_runtime()
        if sys.platform == "darwin" and platform.machine() == "arm64":
            launcher.validate_runtime(facts)
        self.assertTrue(facts.has_serialize)
        self.assertTrue(facts.has_deserialize)
        self.assertTrue(facts.sqlite_image_roundtrip)
        self.assertTrue(facts.fts5_works)

    def test_unsupported_runtime_errors_are_stable_chinese_without_traceback(self) -> None:
        cases = (
            (_supported_facts(system="Linux"), "只支持 Apple Silicon macOS"),
            (_supported_facts(machine="x86_64"), "不是 arm64 架构"),
            (_supported_facts(python_version=(3, 10, 9)), "需要 Python 3.11"),
            (_supported_facts(has_serialize=False), "缺少 serialize/deserialize"),
            (_supported_facts(has_deserialize=False), "缺少 serialize/deserialize"),
            (_supported_facts(sqlite_image_roundtrip=False), "无法完成 serialize/deserialize"),
            (_supported_facts(fts5_works=False), "缺少可用的 FTS5"),
        )
        for facts, expected in cases:
            with self.subTest(expected=expected):
                errors = io.StringIO()
                with mock.patch.object(launcher, "inspect_runtime", return_value=facts):
                    with redirect_stderr(errors):
                        status = launcher.main(["--check-runtime"])
                self.assertEqual(status, 2)
                self.assertIn(expected, errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())

    def test_controlled_paths_use_project_venv_and_user_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "项目 含空格"
            home = root / "temporary home"
            paths = launcher.resolve_paths(project, home=home)
        self.assertEqual(paths.venv, project.resolve() / ".venv")
        self.assertEqual(paths.venv_python, project.resolve() / ".venv/bin/python")
        self.assertEqual(
            paths.application_root,
            home.resolve() / "Library/Application Support/literature-evidence-mcp",
        )
        self.assertEqual(paths.mcp_shim, paths.application_root / "mcp-server")

    def test_finder_command_locates_project_with_spaces_from_unrelated_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "文献 证据 项目"
            home = root / "isolated home"
            unrelated = root / "other cwd"
            project.mkdir()
            home.mkdir()
            unrelated.mkdir()
            _make_minimal_project(project)
            env = dict(os.environ)
            env["HOME"] = os.fspath(home)
            env["LITERATURE_EVIDENCE_PYTHON"] = sys.executable
            completed = subprocess.run(
                ["/bin/zsh", os.fspath(project / COMMAND_PATH.name), "--preflight-only"],
                cwd=unrelated,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(os.fspath(project.resolve()), completed.stdout)
            self.assertIn(os.fspath(home.resolve()), completed.stdout)
            self.assertFalse((project / ".venv").exists())
            self.assertFalse((project / "scripts" / "__pycache__").exists())

    def test_command_is_executable_and_valid_zsh(self) -> None:
        self.assertTrue(os.access(COMMAND_PATH, os.X_OK))
        completed = subprocess.run(
            ["/bin/zsh", "-n", os.fspath(COMMAND_PATH)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_command_without_project_bootstrap_has_actionable_chinese_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            isolated_command = Path(raw) / COMMAND_PATH.name
            shutil.copy2(COMMAND_PATH, isolated_command)
            completed = subprocess.run(
                ["/bin/zsh", os.fspath(isolated_command)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("不要只移动 .command", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_python_override_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            _make_minimal_project(project)
            override_directory = root / "not-a-python"
            override_directory.mkdir()
            env = dict(os.environ)
            env["LITERATURE_EVIDENCE_PYTHON"] = os.fspath(
                override_directory
            )
            completed = subprocess.run(
                ["/bin/zsh", os.fspath(project / COMMAND_PATH.name)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("不是可执行的 Python 路径", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_launcher_invokes_no_installer_or_shell_configuration_commands(self) -> None:
        command_text = COMMAND_PATH.read_text(encoding="utf-8")
        bootstrap_text = LAUNCHER_PATH.read_text(encoding="utf-8")
        executable_lines = command_text + "\n" + bootstrap_text
        self.assertNotRegex(
            executable_lines,
            re.compile(r"^\s*(sudo|brew|curl)\b", re.MULTILINE),
        )
        self.assertNotIn("/Users/", executable_lines)
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f'$HOME/.local/bin/python{version}', command_text)
        for shell_file in (
            ".zshrc",
            ".zprofile",
            ".bashrc",
            ".bash_profile",
            ".profile",
        ):
            self.assertNotIn(shell_file, executable_lines)


class StageFourEnvironmentTests(unittest.TestCase):
    def test_prepare_uses_noneditable_pip_once_and_writes_marker_only_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "source with spaces"
            project.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=Path(raw) / "home")
            calls = []

            def runner(command, *, cwd=None, env=None):
                argv = list(command)
                calls.append((argv, cwd, env))
                if _is_venv_create(argv):
                    _make_fake_venv(paths)
                return 0

            with redirect_stdout(io.StringIO()):
                installed = launcher.prepare_environment(
                    paths,
                    runner=runner,
                    identity_reader=lambda _python: _identity_for(
                        paths, (3, 11, 9)
                    ),
                )
            self.assertTrue(installed)
            pip_calls = [call for call in calls if _is_pip_install(call[0])]
            self.assertEqual(len(pip_calls), 1)
            pip_argv = pip_calls[0][0]
            self.assertNotIn("-e", pip_argv)
            self.assertNotIn("--retries", pip_argv)
            self.assertNotIn("--resume-retries", pip_argv)
            self.assertEqual(
                pip_argv[pip_argv.index("--prefix") + 1],
                os.fspath(paths.venv),
            )
            self.assertIn(os.fspath(project.resolve()), pip_argv)
            self.assertTrue(paths.marker.is_file())
            marker = json.loads(paths.marker.read_text(encoding="utf-8"))
            self.assertEqual(marker["python"], [3, 11, 9])
            pip_env = pip_calls[0][2]
            self.assertIsNotNone(pip_env)
            self.assertEqual(pip_env["PIP_NO_CACHE_DIR"], "1")
            self.assertEqual(pip_env["PIP_CONFIG_FILE"], os.devnull)
            self.assertNotIn("PIP_RETRIES", pip_env)
            self.assertNotIn("PIP_RESUME_RETRIES", pip_env)

            repeated_calls = []

            def repeat_runner(command, *, cwd=None, env=None):
                repeated_calls.append(list(command))
                return 0

            self.assertFalse(
                launcher.prepare_environment(
                    paths,
                    runner=repeat_runner,
                    identity_reader=lambda _python: _identity_for(
                        paths, (3, 11, 9)
                    ),
                )
            )
            self.assertFalse(
                any(_is_pip_install(argv) for argv in repeated_calls)
            )

            forbidden = {"sudo", "brew", "curl"}
            for argv, _cwd, _env in calls:
                executable = Path(argv[0]).name
                self.assertNotIn(executable, forbidden)

    def test_failed_install_stops_without_success_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "project"
            project.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=Path(raw) / "home")

            def runner(command, *, cwd=None, env=None):
                argv = list(command)
                if _is_venv_create(argv):
                    _make_fake_venv(paths)
                    return 0
                if _is_pip_install(argv):
                    return 1
                return 0

            with self.assertRaisesRegex(launcher.LauncherError, "安装失败"):
                with redirect_stdout(io.StringIO()):
                    launcher.prepare_environment(
                        paths,
                        runner=runner,
                        identity_reader=lambda _python: _identity_for(paths),
                    )
            self.assertFalse(paths.marker.exists())

    def test_existing_venv_must_pass_its_own_runtime_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "project"
            project.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=Path(raw) / "home")
            _make_fake_venv(paths)
            calls = []

            def runner(command, *, cwd=None, env=None):
                argv = list(command)
                calls.append(argv)
                return 2 if argv[-1] == "--check-runtime" else 0

            with self.assertRaisesRegex(
                launcher.LauncherError,
                r"\.venv 的 Python 不满足",
            ):
                launcher.prepare_environment(
                    paths,
                    runner=runner,
                    identity_reader=lambda _python: self.fail(
                        "version reader must not run after failed preflight"
                    ),
                )
            self.assertFalse(
                any(_is_pip_install(argv) for argv in calls)
            )
            self.assertFalse(paths.marker.exists())

    def test_residual_venv_cannot_redirect_pip_to_base_python(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "project"
            project.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=Path(raw) / "home")
            _make_fake_venv(paths)
            calls = []

            def runner(command, *, cwd=None, env=None):
                calls.append(list(command))
                return 0

            outside = Path(raw) / "base-python"
            bad_identity = launcher.PythonIdentity(
                version=(3, 12, 1),
                prefix=os.fspath(outside),
                base_prefix=os.fspath(outside),
            )
            with self.assertRaisesRegex(
                launcher.LauncherError,
                "不是完整隔离的 Python 虚拟环境",
            ):
                launcher.prepare_environment(
                    paths,
                    runner=runner,
                    identity_reader=lambda _python: bad_identity,
                )
            self.assertFalse(
                any(_is_pip_install(argv) for argv in calls)
            )
            self.assertFalse(paths.marker.exists())

    def test_internal_venv_symlink_cannot_redirect_pip_outside(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "project"
            project.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=Path(raw) / "home")
            _make_fake_venv(paths)
            outside = Path(raw) / "outside-site-packages"
            outside.mkdir()
            library = paths.venv / "lib" / "python3.12"
            site_packages = library / "site-packages"
            site_packages.rmdir()
            site_packages.symlink_to(
                outside,
                target_is_directory=True,
            )
            calls = []

            def runner(command, *, cwd=None, env=None):
                calls.append(list(command))
                return 0

            with self.assertRaisesRegex(
                launcher.LauncherError,
                r"\.venv/lib 含有符号链接",
            ):
                launcher.prepare_environment(
                    paths,
                    runner=runner,
                    identity_reader=lambda _python: _identity_for(paths),
                )
            self.assertEqual(calls, [])
            self.assertEqual(list(outside.iterdir()), [])

    def test_nested_links_and_external_pth_stop_before_managed_python(self) -> None:
        for case in (
            "package-link",
            "script-link",
            "pth-path",
            "pth-code",
            "pth-bom-code",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                project = root / "project"
                project.mkdir()
                _make_minimal_project(project)
                paths = launcher.resolve_paths(project, home=root / "home")
                _make_fake_venv(paths)
                site_packages = (
                    paths.venv / "lib" / "python3.12" / "site-packages"
                )
                outside = root / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel.txt"
                sentinel.write_text("unchanged\n", encoding="utf-8")

                if case == "package-link":
                    (site_packages / "literature_evidence_mcp").symlink_to(
                        outside,
                        target_is_directory=True,
                    )
                elif case == "script-link":
                    (paths.venv / "bin" / "literature-evidence").symlink_to(
                        sentinel
                    )
                elif case == "pth-path":
                    (site_packages / "escape.pth").write_text(
                        os.fspath(outside) + "\n",
                        encoding="utf-8",
                    )
                elif case == "pth-code":
                    (site_packages / "escape.pth").write_text(
                        "import os; os.remove(" + repr(os.fspath(sentinel)) + ")\n",
                        encoding="utf-8",
                    )
                else:
                    payload = (
                        "import os; os.remove("
                        + repr(os.fspath(sentinel))
                        + ")\n"
                    ).encode("utf-8")
                    (site_packages / "escape.pth").write_bytes(
                        b"\xef\xbb\xbf" + payload
                    )

                runner = mock.Mock()
                identity_reader = mock.Mock()
                with self.assertRaises(launcher.LauncherError):
                    launcher.prepare_environment(
                        paths,
                        runner=runner,
                        identity_reader=identity_reader,
                    )
                runner.assert_not_called()
                identity_reader.assert_not_called()
                self.assertEqual(
                    sentinel.read_text(encoding="utf-8"),
                    "unchanged\n",
                )

    def test_standard_setuptools_distutils_pth_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=root / "home")
            _make_fake_venv(paths)
            site_packages = (
                paths.venv / "lib" / "python3.12" / "site-packages"
            )
            (site_packages / "distutils-precedence.pth").write_text(
                launcher.SETUPTOOLS_DISTUTILS_PTH + "\n",
                encoding="utf-8",
            )
            (site_packages / "package-data" / "site-packages").mkdir(
                parents=True
            )

            launcher._validate_managed_tree_before_python(paths)

    def test_install_tree_walk_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=root / "home")
            _make_fake_venv(paths)

            def failing_walk(_root, *, followlinks, onerror):
                self.assertFalse(followlinks)
                self.assertIsNotNone(onerror)
                onerror(PermissionError("blocked subtree"))
                return ()

            with mock.patch.object(launcher.os, "walk", side_effect=failing_walk):
                with self.assertRaisesRegex(
                    launcher.LauncherError,
                    r"无法安全检查项目内 \.venv/lib",
                ):
                    launcher._validate_managed_tree_before_python(paths)

    def test_pip_and_python_children_ignore_ambient_install_and_import_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "project"
            project.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=Path(raw) / "home")
            outside = Path(raw) / "outside"
            calls = []

            def runner(command, *, cwd=None, env=None):
                argv = list(command)
                calls.append((argv, cwd, env))
                if _is_venv_create(argv):
                    _make_fake_venv(paths)
                return 0

            poisoned = {
                "PIP_TARGET": os.fspath(outside / "target"),
                "PIP_PREFIX": os.fspath(outside / "prefix"),
                "PIP_ROOT": os.fspath(outside / "root"),
                "PIP_USER": "1",
                "PIP_CONFIG_FILE": os.fspath(outside / "pip.conf"),
                "PIP_INDEX_URL": "https://invalid.example/simple",
                "PIP_RETRIES": "99",
                "PIP_RESUME_RETRIES": "99",
                "PYTHONHOME": os.fspath(outside / "python-home"),
                "PYTHONPATH": os.fspath(outside / "python-path"),
                "PYTHONUSERBASE": os.fspath(outside / "user-base"),
                "VIRTUAL_ENV": os.fspath(outside / "other-venv"),
                "__PYVENV_LAUNCHER__": os.fspath(outside / "python"),
            }
            with mock.patch.dict(os.environ, poisoned, clear=False):
                with redirect_stdout(io.StringIO()):
                    launcher.prepare_environment(
                        paths,
                        runner=runner,
                        identity_reader=lambda _python: _identity_for(paths),
                    )

            pip_call = next(call for call in calls if _is_pip_install(call[0]))
            pip_argv, _cwd, pip_env = pip_call
            self.assertEqual(
                pip_argv[1:5],
                ["-I", "-m", "pip", "install"],
            )
            self.assertNotIn("--resume-retries", pip_argv)
            self.assertNotIn("--retries", pip_argv)
            self.assertEqual(
                pip_argv[pip_argv.index("--prefix") + 1],
                os.fspath(paths.venv),
            )
            self.assertEqual(pip_env["PIP_CONFIG_FILE"], os.devnull)
            self.assertNotIn("PIP_RETRIES", pip_env)
            self.assertNotIn("PIP_RESUME_RETRIES", pip_env)
            for name in poisoned:
                if name == "PIP_CONFIG_FILE":
                    continue
                self.assertNotIn(name, pip_env)
            for argv, _cwd, child_env in calls:
                if argv[0] not in {sys.executable, os.fspath(paths.venv_python)}:
                    continue
                self.assertIsNotNone(child_env)
                for name in (
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "PYTHONUSERBASE",
                    "VIRTUAL_ENV",
                    "__PYVENV_LAUNCHER__",
                ):
                    self.assertNotIn(name, child_env)
                self.assertEqual(
                    child_env["PYTHONDONTWRITEBYTECODE"],
                    "1",
                )

    def test_venv_configuration_requires_explicit_system_site_false(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            false_paths = launcher.resolve_paths(
                root / "false-project",
                home=root / "home",
            )
            false_paths.venv.mkdir(parents=True)
            (false_paths.venv / "pyvenv.cfg").write_text(
                "  Include-System-Site-Packages = FaLsE  \n",
                encoding="utf-8",
            )
            launcher._validate_venv_configuration(false_paths)

            true_paths = launcher.resolve_paths(
                root / "true-project",
                home=root / "home",
            )
            true_paths.venv.mkdir(parents=True)
            (true_paths.venv / "pyvenv.cfg").write_text(
                "include-system-site-packages = true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                launcher.LauncherError,
                "必须明确禁用系统 site-packages",
            ):
                launcher._validate_venv_configuration(true_paths)

            missing_paths = launcher.resolve_paths(
                root / "missing-project",
                home=root / "home",
            )
            missing_paths.venv.mkdir(parents=True)
            with self.assertRaisesRegex(
                launcher.LauncherError,
                "缺少可读取的 pyvenv.cfg",
            ):
                launcher._validate_venv_configuration(missing_paths)

    def test_marker_write_does_not_follow_preexisting_temporary_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / launcher.MARKER_NAME
            old_fixed_temporary = marker.with_name(marker.name + ".tmp")
            outside = root / "outside.txt"
            outside.write_text("do not change\n", encoding="utf-8")
            old_fixed_temporary.symlink_to(outside)

            launcher._write_marker(marker, {"format": 1})

            self.assertEqual(outside.read_text(encoding="utf-8"), "do not change\n")
            self.assertTrue(old_fixed_temporary.is_symlink())
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8")),
                {"format": 1},
            )

    def test_application_root_creation_is_bounded_to_isolated_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            paths = launcher.resolve_paths(project, home=home)
            launcher.ensure_application_root(paths.application_root)
            self.assertTrue(paths.application_root.is_dir())
            self.assertEqual(
                sorted(path.name for path in home.iterdir()),
                ["Library"],
            )
            for forbidden in (
                ".zshrc",
                ".zprofile",
                ".bashrc",
                ".bash_profile",
                ".profile",
                ".codex",
                ".claude",
            ):
                self.assertFalse((home / forbidden).exists())

    def test_application_root_rejects_ancestor_symlink_before_outside_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            project = root / "project"
            outside = root / "outside"
            home.mkdir()
            project.mkdir()
            outside.mkdir()
            (home / "Library").symlink_to(outside, target_is_directory=True)
            paths = launcher.resolve_paths(project, home=home)

            with self.assertRaisesRegex(launcher.LauncherError, "符号链接"):
                launcher.ensure_application_root(paths.application_root)

            self.assertEqual(list(outside.iterdir()), [])

    def test_mcp_shim_is_atomic_0700_updates_for_spaces_and_rejects_symlink(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home with spaces"
            first_project = root / "first project with spaces"
            second_project = root / "moved project with spaces"
            first_project.mkdir()
            second_project.mkdir()
            first = launcher.resolve_paths(first_project, home=home)
            second = launcher.resolve_paths(second_project, home=home)
            _make_fake_venv(first)
            _make_fake_venv(second)
            launcher.ensure_application_root(first.application_root)

            launcher.install_mcp_shim(first)
            first_inode = first.mcp_shim.stat().st_ino
            self.assertEqual(
                first.mcp_shim.read_text(encoding="utf-8"),
                launcher._mcp_shim_text(first),
            )
            self.assertEqual(stat.S_IMODE(first.mcp_shim.stat().st_mode), 0o700)
            self.assertIn("literature_evidence_mcp.mcp_server", first.mcp_shim.read_text())
            self.assertNotIn("guarded_mcp", first.mcp_shim.read_text())

            launcher.install_mcp_shim(second)
            self.assertNotEqual(first_inode, second.mcp_shim.stat().st_ino)
            self.assertEqual(
                second.mcp_shim.read_text(encoding="utf-8"),
                launcher._mcp_shim_text(second),
            )
            self.assertEqual(stat.S_IMODE(second.mcp_shim.stat().st_mode), 0o700)
            self.assertEqual(
                list(second.application_root.glob(".mcp-server.*.tmp")),
                [],
            )

            outside = root / "outside-sentinel"
            outside.write_text("unchanged\n", encoding="utf-8")
            second.mcp_shim.unlink()
            second.mcp_shim.symlink_to(outside)
            with self.assertRaisesRegex(launcher.LauncherError, "不是普通文件"):
                launcher.install_mcp_shim(second)
            self.assertTrue(second.mcp_shim.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")

    def test_mcp_shim_replace_failure_preserves_old_entry_and_cleans_temporary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            paths = launcher.resolve_paths(project, home=root / "home")
            _make_fake_venv(paths)
            launcher.ensure_application_root(paths.application_root)
            paths.mcp_shim.write_text("old-safe-shim\n", encoding="utf-8")
            paths.mcp_shim.chmod(0o700)

            with mock.patch.object(
                launcher.os,
                "replace",
                side_effect=OSError("synthetic replace failure"),
            ):
                with self.assertRaisesRegex(
                    launcher.LauncherError,
                    "无法安全安装",
                ):
                    launcher.install_mcp_shim(paths)

            self.assertEqual(
                paths.mcp_shim.read_text(encoding="utf-8"),
                "old-safe-shim\n",
            )
            self.assertEqual(
                list(paths.application_root.glob(".mcp-server.*.tmp")),
                [],
            )


class StageFourBrowserAndLifecycleTests(unittest.TestCase):
    def test_browser_opens_only_after_successful_bind_and_listen(self) -> None:
        port = _unused_port()
        events = []

        class FakeServer:
            def __init__(self, config: uvicorn.Config) -> None:
                self.config = config

            def run(self, sockets=None) -> None:
                events.append(("server", True))

        def opener(url: str) -> bool:
            competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with self.assertRaises(OSError):
                    competitor.bind(("127.0.0.1", port))
            finally:
                competitor.close()
            queued = socket.create_connection(("127.0.0.1", port), timeout=1)
            queued.close()
            events.append(("browser", url))
            return True

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("uvicorn.Server", FakeServer):
                with redirect_stdout(io.StringIO()):
                    serve_local(
                        Path(raw) / "library",
                        port=port,
                        open_browser=True,
                        browser_opener=opener,
                    )
        self.assertEqual(events[0], ("browser", f"http://127.0.0.1:{port}/"))
        self.assertEqual(events[1], ("server", True))

    def test_occupied_port_never_calls_browser(self) -> None:
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        opener = mock.Mock(return_value=True)
        try:
            with tempfile.TemporaryDirectory() as raw:
                with self.assertRaises(ImportPolicyError):
                    serve_local(
                        Path(raw) / "library",
                        port=port,
                        open_browser=True,
                        browser_opener=opener,
                    )
        finally:
            occupied.close()
        opener.assert_not_called()

    def test_browser_failure_keeps_service_entry_and_prints_manual_action(self) -> None:
        port = _unused_port()
        ran = []

        class FakeServer:
            def __init__(self, config: uvicorn.Config) -> None:
                pass

            def run(self, sockets=None) -> None:
                ran.append(True)

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("uvicorn.Server", FakeServer):
                with redirect_stdout(output):
                    serve_local(
                        Path(raw) / "library",
                        port=port,
                        open_browser=True,
                        browser_opener=mock.Mock(side_effect=OSError("no browser")),
                    )
        self.assertEqual(ran, [True])
        self.assertIn("请手动打开", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_cli_open_browser_flag_reaches_existing_serve_entry(self) -> None:
        with mock.patch("literature_evidence_mcp.web.serve_local") as mocked:
            self.assertEqual(
                cli_main(
                    [
                        "serve",
                        "--application-root",
                        "/tmp/fixed-application",
                        "--port",
                        "18765",
                        "--open-browser",
                    ]
                ),
                0,
            )
        mocked.assert_called_once_with(
            Path("/tmp/fixed-application"), port=18765, open_browser=True
        )

    @unittest.skipUnless(
        sys.platform == "darwin" and platform.machine() == "arm64",
        "stage-four Finder launcher targets Apple Silicon macOS",
    )
    def test_command_sigint_stops_foreground_server_and_port_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "lifecycle project with spaces"
            home = root / "isolated home"
            project.mkdir()
            home.mkdir()
            _make_minimal_project(project)
            paths = launcher.resolve_paths(project, home=home)
            completed = subprocess.run(
                [sys.executable, "-I", "-m", "venv", os.fspath(paths.venv)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            site_query = subprocess.run(
                [
                    os.fspath(paths.venv_python),
                    "-I",
                    "-c",
                    "import site; print(site.getsitepackages()[0])",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(site_query.returncode, 0, site_query.stderr)
            site_packages = Path(site_query.stdout.strip())
            shutil.copytree(
                ROOT / "src" / "literature_evidence_mcp",
                site_packages / "literature_evidence_mcp",
            )
            dependency_paths = [
                path
                for path in sys.path
                if path and "site-packages" in path and Path(path).is_dir()
            ]
            self.assertTrue(dependency_paths)
            for dependency_path in dependency_paths:
                for entry in Path(dependency_path).iterdir():
                    if entry.name == "literature_evidence_mcp" or entry.suffix == ".pth":
                        continue
                    destination = site_packages / entry.name
                    if entry.is_dir():
                        shutil.copytree(
                            entry,
                            destination,
                            dirs_exist_ok=True,
                        )
                    elif entry.is_file():
                        shutil.copy2(entry, destination)
            fingerprint = launcher.source_fingerprint(project)
            paths.marker.write_text(
                json.dumps(
                    launcher._marker_payload(
                        fingerprint,
                        (
                            sys.version_info.major,
                            sys.version_info.minor,
                            sys.version_info.micro,
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            port = _unused_port()
            env = dict(os.environ)
            env["HOME"] = os.fspath(home)
            env["LITERATURE_EVIDENCE_PYTHON"] = sys.executable
            env["PYTHONPATH"] = os.fspath(root / "must-not-be-used")
            env["PYTHONHOME"] = os.fspath(root / "must-not-be-used-as-home")
            process = subprocess.Popen(
                [
                    "/bin/zsh",
                    os.fspath(project / COMMAND_PATH.name),
                    "--no-browser",
                    "--port",
                    str(port),
                ],
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                ready = False
                while process.poll() is None and time.monotonic() < deadline:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=0.3
                    )
                    try:
                        connection.request("GET", "/api/status")
                        response = connection.getresponse()
                        body = response.read()
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.05)
                    else:
                        try:
                            payload = json.loads(body)
                        except (UnicodeError, json.JSONDecodeError):
                            payload = {}
                        if (
                            response.status == 200
                            and payload.get("service") == "ready"
                            and payload.get("binding") == "127.0.0.1"
                        ):
                            ready = True
                            break
                        time.sleep(0.05)
                    finally:
                        connection.close()
                if not ready:
                    process.kill()
                    failed_stdout, failed_stderr = process.communicate(timeout=5)
                    self.fail(
                        "launcher did not bind the loopback port\n"
                        + failed_stdout
                        + failed_stderr
                    )
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            self.assertIn(process.returncode, (0, 130))
            self.assertNotIn("Traceback", stdout + stderr)

            rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                rebound.bind(("127.0.0.1", port))
            finally:
                rebound.close()
            self.assertTrue(paths.application_root.is_dir())
            self.assertFalse((home / ".codex").exists())
            self.assertFalse((home / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
