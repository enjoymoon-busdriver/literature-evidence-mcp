from __future__ import annotations

import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from literature_evidence_mcp import desktop, mcp_selfcheck
from literature_evidence_mcp.connections import ConnectionError, WindowsLocalOnlyConnections
from literature_evidence_mcp.web import create_app
from tests.test_formal_ui_frontend import run_scenario


class _ParentPipe:
    def __init__(self, server_started: threading.Event, line: str = "") -> None:
        self.server_started = server_started
        self.line = line
        self.reads = 0

    def readline(self) -> str:
        self.server_started.wait(timeout=2)
        self.reads += 1
        return self.line


class DesktopRuntimeTests(unittest.TestCase):
    def test_explicit_shutdown_line_owns_the_same_graceful_signal(self) -> None:
        shutdown = threading.Event()
        desktop._watch_stdin(io.StringIO("ignored\nshutdown\n"), shutdown)
        self.assertTrue(shutdown.is_set())

    def test_mcp_subcommand_forwards_arguments_without_rewriting(self) -> None:
        with mock.patch(
            "literature_evidence_mcp.mcp_server.main", return_value=17
        ) as mcp_main:
            result = desktop.main(
                ["mcp", "--application-root", r"C:\Synthetic Root", "--future-flag"]
            )
        self.assertEqual(result, 17)
        mcp_main.assert_called_once_with(
            ["--application-root", r"C:\Synthetic Root", "--future-flag"]
        )

    def test_desktop_reports_one_ready_line_then_stops_on_parent_eof(self) -> None:
        started = threading.Event()
        captured: dict[str, object] = {}

        class FakeServer:
            def __init__(self, config) -> None:
                self.config = config
                self.started = False
                self.should_exit = False
                self.force_exit = False

            def run(self, sockets=None) -> None:
                listener = sockets[0]
                host, port = listener.getsockname()
                captured["socket"] = (host, port)
                probe = socket.create_connection((host, port), timeout=1)
                probe.close()
                captured["listening"] = True
                self.started = True
                started.set()
                deadline = time.monotonic() + 3
                while not self.should_exit and time.monotonic() < deadline:
                    time.sleep(0.005)

        output = io.StringIO()
        parent = _ParentPipe(started)
        with tempfile.TemporaryDirectory(prefix="foliohook-desktop-") as raw:
            root = Path(raw) / "application"

            def fake_create_app(application_root, *, port, connections):
                captured["root"] = application_root
                captured["port"] = port
                captured["connections"] = connections
                return object()

            with (
                mock.patch("uvicorn.Server", FakeServer),
                mock.patch(
                    "literature_evidence_mcp.web.create_app",
                    side_effect=fake_create_app,
                ),
                mock.patch(
                    "literature_evidence_mcp.connections.desktop_connections",
                    return_value=object(),
                ),
            ):
                result = desktop.run_desktop(root, stdin=parent, stdout=output)

        self.assertEqual(result, 0)
        self.assertEqual(captured["root"], root)
        self.assertEqual(captured["socket"], ("127.0.0.1", captured["port"]))
        self.assertTrue(captured["listening"])
        self.assertEqual(parent.reads, 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            json.loads(lines[0]),
            {"url": f"http://127.0.0.1:{captured['port']}"},
        )
        self.assertFalse(lines[0].endswith("/"))

    def test_frozen_windows_guide_uses_only_stable_cmd_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="foliohook-guide-") as raw:
            root = Path(raw) / "中文 Local App Data !" / "literature-evidence-mcp"
            root.mkdir(parents=True)
            executable = Path(raw) / "app-0.1.0" / "foliohook-backend.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"synthetic")
            launcher = root / "mcp-server.cmd"
            launcher.write_text("@echo off\r\n", encoding="utf-8")
            expected_launcher = str(launcher.absolute())
            with mock.patch.object(
                mcp_selfcheck, "_frozen_windows_executable", return_value=executable
            ):
                guide = mcp_selfcheck.local_mcp_guide(root)

        self.assertEqual(guide["state"], "copy_ready_not_configured")
        self.assertEqual(guide["platform"], "windows_x64_test")
        self.assertIn("cmd.exe", guide["toml"])
        self.assertIn("/v:off", guide["toml"])
        self.assertIn("mcp-server.cmd", guide["toml"])
        args_line = next(
            line for line in guide["toml"].splitlines() if line.startswith("args = ")
        )
        self.assertEqual(
            json.loads(args_line.removeprefix("args = ")),
            ["/d", "/v:off", "/c", "call", expected_launcher],
        )
        self.assertNotIn("/s", guide["toml"])
        self.assertIn(" /c call ", guide["cli"])
        self.assertNotIn("foliohook-backend.exe", guide["toml"])
        self.assertNotIn("python", guide["toml"].lower())
        self.assertNotIn("zsh", guide["toml"].lower())
        self.assertNotIn(" -m ", guide["cli"])

    def test_windows_guide_rejects_percent_expansion_in_cmd_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="foliohook-guide-") as raw:
            root = Path(raw) / "%unsafe%" / "literature-evidence-mcp"
            root.mkdir(parents=True)
            executable = Path(raw) / "foliohook-backend.exe"
            executable.write_bytes(b"synthetic")
            (root / "mcp-server.cmd").write_text("@echo off\r\n", encoding="utf-8")
            with mock.patch.object(
                mcp_selfcheck, "_frozen_windows_executable", return_value=executable
            ):
                guide = mcp_selfcheck.local_mcp_guide(root)
        self.assertEqual(guide["state"], "unavailable")
        self.assertNotIn("cli", guide)
        self.assertNotIn("toml", guide)

    def test_frozen_self_check_launches_executable_directly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="foliohook-stdio-") as raw:
            temporary = Path(raw)
            executable = temporary / "foliohook-backend.exe"
            application_root = temporary / "application"
            fake_home = temporary / "fake home"
            params = mcp_selfcheck._frozen_stdio_parameters(
                executable, application_root, temporary, fake_home
            )
            with mock.patch.object(
                mcp_selfcheck, "_frozen_windows_executable", return_value=executable
            ):
                observation = mcp_selfcheck._base_report()["observation_scope_zh"]

        self.assertEqual(params.command, str(executable))
        self.assertEqual(
            params.args,
            ["mcp", "--application-root", str(application_root)],
        )
        self.assertNotIn("HOME", params.env)
        self.assertNotIn("PYTHONPATH", params.env)
        self.assertNotIn("PYTHONHOME", params.env)
        self.assertEqual(
            set(params.env) - {"SystemRoot"}, {"LOCALAPPDATA", "TEMP", "TMP"}
        )
        self.assertIn("不核验稳定 cmd 入口", observation)

    def test_windows_candidate_is_visibly_local_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="foliohook-local-only-") as raw:
            root = Path(raw) / "application"
            connections = WindowsLocalOnlyConnections(root)
            status = connections.status()
            self.assertTrue(status["platform_support"]["local_bm25"])
            self.assertFalse(status["platform_support"]["enhanced"])
            self.assertFalse(status["platform_support"]["tunnel"])
            self.assertEqual(status["credential_storage"], "unavailable")
            self.assertFalse(status["aliyun_configured"])
            self.assertFalse(status["openai_tunnel_configured"])
            self.assertFalse((root / "connections.json").exists())
            with self.assertRaisesRegex(ConnectionError, "Windows x64 测试版"):
                connections.save_key("aliyun", "synthetic-not-a-real-key")
            self.assertFalse((root / "connections.json").exists())

            app = create_app(root, port=19141, connections=connections)
            with TestClient(app, base_url="http://127.0.0.1:19141") as client:
                response = client.get("/api/status")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertFalse(payload["enhanced_available"])
            self.assertFalse(payload["connections"]["platform_support"]["tunnel"])

        run_scenario(self, r'''
const status = {
  aliyun_configured: false, openai_tunnel_configured: false,
  tunnel_id: '', tunnel_backoff_accepted: false,
  model_settings: {enabled:false, provider:'unavailable', region:'unavailable',
    model_ids:{}, recommended_model_ids:{}},
  tunnel: {state:'unavailable', client_installed:false},
  platform_support: {local_bm25:true, enhanced:false, tunnel:false,
    credential_storage:false,
    message:'Windows x64 测试版仅支持本机资料库与 BM25；AI 增强搜索、凭据保存和 Secure MCP Tunnel 暂不可用。'},
};
FolioConnections.acceptStatus({connections: status});
state.page = 'settings'; FolioApp.render();
assert(nodes.get('content').innerHTML.includes('Windows x64 测试版'));
assert(nodes.get('content').innerHTML.includes('不会保存明文 Key'));
assert(!nodes.get('content').innerHTML.includes('id="aliyun-secret"'));
state.page = 'apps'; FolioApp.render();
assert(nodes.get('content').innerHTML.includes('Secure MCP Tunnel · 当前不可用'));
assert(!nodes.get('content').innerHTML.includes('id="openai-secret"'));
state.mcpGuide = {state:'copy_ready_not_configured', platform:'windows_x64_test', cli:'x', toml:'y'};
state.mcpSelfCheck = {passed:true, tool_count:8}; FolioApp.render();
assert(nodes.get('content').innerHTML.includes('本次未核验稳定 cmd 被真实客户端调用'));
assert.equal(requests.length, 0);
''')


if __name__ == "__main__":
    unittest.main()
