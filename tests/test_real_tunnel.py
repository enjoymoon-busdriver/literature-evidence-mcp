from __future__ import annotations

import io
import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from literature_evidence_mcp.real_tunnel import RealTunnel, _log_error


TUNNEL_ID = "tunnel_" + "a" * 32
KEY = "sk-SYNTHETIC-ONLY-NEVER-A-REAL-KEY"
MODULE = "literature_evidence_mcp.real_tunnel."


class FakeProcess:
    pid = 424242

    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self.returncode = None
        self.group_alive = True

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.returncode = 0
        self.group_alive = False
        return 0


class RealTunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lemcp-tunnel-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "应用 root"
        (self.root / "bin").mkdir(parents=True)
        for path in (self.root / "bin" / "tunnel-client", self.root / "mcp-server"):
            path.write_text("synthetic placeholder; never executed\n", encoding="utf-8")
            path.chmod(0o700)
        self.key_loader = mock.Mock(return_value=KEY)
        self.tunnel = RealTunnel(self.root, self.key_loader)
        self.processes = []
        self.starts = []
        self.signals = []
        self.real_popen = subprocess.Popen
        self.popen = mock.patch(MODULE + "subprocess.Popen", side_effect=self.spawn).start()
        self.getpgid = mock.patch(MODULE + "os.getpgid", return_value=FakeProcess.pid).start()
        self.killpg = mock.patch(MODULE + "os.killpg", side_effect=self.kill).start()
        self.thread = mock.patch(MODULE + "threading.Thread").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch(MODULE + "_HOMEBREW_CLIENTS", ()).start()
        self.addCleanup(self.tunnel.close)

    def spawn(self, command, **options):
        process = FakeProcess()
        self.processes.append(process)
        descriptor, = options["pass_fds"]
        # The synthetic key must be readable solely through the inherited pipe.
        self.assertEqual(os.read(descriptor, 4096).decode(), KEY)
        self.assertEqual(os.read(descriptor, 1), b"")
        self.starts.append((command, options))
        return process

    def kill(self, pgid, value):
        self.assertEqual(pgid, FakeProcess.pid)
        self.signals.append(value)
        if value == 0 and not self.processes[-1].group_alive:
            raise ProcessLookupError()

    def start(self):
        report = self.tunnel.start(TUNNEL_ID)
        self.assertTrue(report["passed"], report)
        return report

    def health_url(self, url="http://127.0.0.1:19874"):
        self.tunnel._run.joinpath("health.url").write_text(url, encoding="utf-8")

    def test_status_and_missing_prerequisites_never_load_credentials(self) -> None:
        self.assertEqual(self.tunnel.status()["state"], "stopped")
        self.assertFalse(self.tunnel.health()["passed"])
        self.assertEqual(self.tunnel.start("invalid")["error_code"], "invalid_tunnel_id")
        (self.root / "bin" / "tunnel-client").unlink()
        self.assertEqual(self.tunnel.start(TUNNEL_ID)["error_code"], "client_missing")
        self.key_loader.assert_not_called()
        self.popen.assert_not_called()

    def test_private_argv_environment_and_owned_cleanup(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "another-secret", "HTTPS_PROXY": "http://old-project"}):
            report = self.start()
        command, options = self.starts[0]
        self.assertEqual(command[:2], [str(self.root / "bin" / "tunnel-client"), "run"])
        self.assertEqual(command[command.index("--health.listen-addr") + 1], "127.0.0.1:0")
        mcp_command = command[command.index("--mcp.command") + 1]
        self.assertEqual(shlex.split(mcp_command), [
            "/usr/bin/env", f"HOME={Path.home()}", str(self.root / "mcp-server"),
        ])
        self.assertTrue(command[command.index("--control-plane.api-key") + 1].startswith("file:/dev/fd/"))
        self.assertTrue(options["start_new_session"])
        self.assertFalse(options["shell"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("HTTPS_PROXY", options["env"])
        self.assertTrue(Path(options["env"]["HOME"]).is_relative_to(self.root))
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes())
        self.assertNotIn(KEY, repr(command) + repr(options) + json.dumps(report))
        run = self.tunnel._run
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)
        self.assertEqual(report["transport_retry_policy"], "official_client_backoff")
        self.assertEqual(report["app_restart_count"], 0)
        self.assertFalse(report["real_connected"])
        self.assertFalse(report["chatgpt_tool_discovery_checked"])
        stopped = self.tunnel.stop()
        self.assertEqual(stopped["state"], "stopped")
        self.assertFalse(stopped["running"])
        self.assertEqual(self.signals, [signal.SIGINT, 0])
        self.assertFalse(run.exists())

    def test_homebrew_symlink_is_found_and_used_without_copying(self) -> None:
        (self.root / "bin" / "tunnel-client").unlink()
        wrapper = self.root / "homebrew cellar" / "bin" / "tunnel-client"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("synthetic wrapper; never executed\n", encoding="utf-8")
        wrapper.chmod(0o700)
        link = self.root / "homebrew-link"
        link.symlink_to(wrapper)
        with mock.patch(MODULE + "_HOMEBREW_CLIENTS", (link,)):
            self.assertTrue(self.tunnel.status()["client_installed"])
            self.key_loader.assert_not_called()
            self.popen.assert_not_called()
            self.start()
        command, options = self.starts[0]
        self.assertEqual(command[0], str(wrapper))
        self.assertFalse((self.root / "bin" / "tunnel-client").exists())
        self.assertEqual(options["env"]["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertTrue(Path(options["env"]["HOME"]).is_relative_to(self.root))
        self.assertNotIn(KEY, repr(command) + repr(options))

    def test_bundled_client_has_priority_over_homebrew(self) -> None:
        with mock.patch(MODULE + "_HOMEBREW_CLIENTS", (self.root / "mcp-server",)):
            self.start()
        self.assertEqual(self.starts[0][0][0], str(self.root / "bin" / "tunnel-client"))

    def test_missing_broken_nonexecutable_and_directory_clients_are_rejected(self) -> None:
        (self.root / "bin" / "tunnel-client").unlink()
        broken = self.root / "broken"
        broken.symlink_to(self.root / "missing")
        plain = self.root / "plain"
        plain.write_text("not executable\n", encoding="utf-8")
        plain.chmod(0o600)
        with mock.patch(MODULE + "_HOMEBREW_CLIENTS", (broken, plain, self.root)):
            self.assertFalse(self.tunnel.status()["client_installed"])
            self.assertEqual(self.tunnel.start(TUNNEL_ID)["error_code"], "client_missing")
        self.key_loader.assert_not_called()
        self.popen.assert_not_called()

    def test_client_installed_after_app_start_is_detected_without_path_search(self) -> None:
        (self.root / "bin" / "tunnel-client").unlink()
        binary = self.root / "homebrew-client"
        path_only = self.root / "tunnel-client"
        path_only.write_text("synthetic PATH client; never executed\n", encoding="utf-8")
        path_only.chmod(0o700)
        with mock.patch.dict(os.environ, {"PATH": str(self.root)}):
            self.assertFalse(self.tunnel.status()["client_installed"])
        with mock.patch(MODULE + "_HOMEBREW_CLIENTS", (binary,)):
            binary.write_text("synthetic placeholder\n", encoding="utf-8")
            binary.chmod(0o700)
            self.assertTrue(self.tunnel.status()["client_installed"])
        self.key_loader.assert_not_called()
        self.popen.assert_not_called()

    def test_mcp_child_restores_home_without_changing_tunnel_environment(self) -> None:
        user_home = self.root / "user home ' $USER"
        (self.root / "mcp-server").write_text(
            '#!/bin/sh\n/usr/bin/printf \'%s\\n\' "$HOME" "$XDG_CONFIG_HOME"\n',
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"HOME": str(user_home)}):
            self.start()
        command, options = self.starts[0]
        private_home = str(self.tunnel._run / "home")
        self.assertEqual(options["env"]["HOME"], private_home)
        self.assertEqual(options["env"]["XDG_CONFIG_HOME"], private_home + "/.config")
        mcp_command = command[command.index("--mcp.command") + 1]
        with mock.patch(MODULE + "subprocess.Popen", new=self.real_popen):
            child = subprocess.run(
                ["/bin/sh", "-c", mcp_command], env=options["env"],
                capture_output=True, text=True, check=True, timeout=5,
            )
        self.assertEqual(child.stdout.splitlines(), [str(user_home), private_home + "/.config"])
        self.assertEqual(options["env"]["HOME"], private_home)

    def test_key_failures_are_redacted_and_do_not_start(self) -> None:
        class Missing(Exception):
            code = "missing"
        for failure, expected in ((Missing(KEY), "key_missing"), (RuntimeError(KEY), "key_unavailable")):
            with self.subTest(expected=expected):
                self.key_loader.side_effect = failure
                result = self.tunnel.start(TUNNEL_ID)
                self.assertEqual(result["error_code"], expected)
                self.assertNotIn(KEY, json.dumps(result))
        self.key_loader.side_effect = None
        self.key_loader.return_value = KEY + "\n"
        self.assertEqual(self.tunnel.start(TUNNEL_ID)["error_code"], "invalid_key")
        self.popen.assert_not_called()

    def test_double_start_does_not_load_a_second_key_or_create_process(self) -> None:
        self.start()
        self.assertEqual(self.tunnel.start(TUNNEL_ID)["error_code"], "not_stopped")
        self.assertEqual(len(self.processes), 1)
        self.key_loader.assert_called_once_with()

    def test_missing_health_file_is_pending_only_during_startup(self) -> None:
        self.start()
        self.assertEqual(self.tunnel.health()["state"], "starting")
        with mock.patch(MODULE + "time.time", return_value=self.tunnel._started_at + 46):
            result = self.tunnel.health()
        self.assertEqual(result["state"], "not_ready")
        self.assertFalse(result["passed"])
        self.assertTrue(result["running"])
        self.assertEqual(self.signals, [])

    def test_readiness_and_current_successful_poll_are_both_required(self) -> None:
        self.start()
        self.health_url()
        metrics = f"commands_poll_last_successful_timestamp_seconds {time.time()}\n"
        responses = [
            mock.Mock(status=200, read=mock.Mock(return_value=b"ok")),
            mock.Mock(status=200, read=mock.Mock(return_value=b"ready")),
            mock.Mock(status=200, read=mock.Mock(return_value=metrics.encode())),
        ]
        with mock.patch(MODULE + "http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.side_effect = responses
            result = self.tunnel.health()
        self.assertTrue(result["real_connected"])
        self.assertFalse(result["chatgpt_tool_discovery_checked"])
        self.assertEqual([call.args for call in connection.call_args_list], [("127.0.0.1", 19874)] * 3)
        with mock.patch.object(self.tunnel, "_read_health", return_value=False):
            result = self.tunnel.health()
        self.assertEqual(result["state"], "not_ready")
        self.assertFalse(result["real_connected"])
        self.assertTrue(result["running"])
        self.assertEqual(self.signals, [])

    def test_nonloopback_health_url_is_rejected_without_network(self) -> None:
        self.start()
        self.health_url("https://example.com/?key=" + KEY)
        with mock.patch(MODULE + "http.client.HTTPConnection") as connection:
            result = self.tunnel.health()
        connection.assert_not_called()
        self.assertEqual(result["error_code"], "health_failed")
        self.assertNotIn(KEY, json.dumps(result))

    def test_transport_failure_stops_and_late_old_logs_cannot_stop_new_run(self) -> None:
        self.start()
        old_process = self.processes[-1]
        old_process.stdout = io.StringIO(json.dumps({"level": "ERROR", "msg": "health server error", "error": KEY}) + "\n")
        self.tunnel._consume(old_process)
        self.assertEqual(self.tunnel.status()["error_code"], "transport_failed")
        self.assertEqual(len(self.processes), 1)
        self.assertNotIn(KEY, json.dumps(self.tunnel.status()))
        self.start()
        old_process.stdout = io.StringIO(json.dumps({"status_code": 401, "error": KEY}) + "\n")
        self.tunnel._consume(old_process)
        self.assertEqual(self.tunnel.status()["state"], "starting")
        self.assertEqual(self.tunnel.status()["app_restart_count"], 0)

    def test_backoff_is_nonfatal_until_recovered_and_auth_takes_priority(self) -> None:
        self.start()
        process = self.processes[-1]
        for event in (
            {"level": "WARN", "msg": "poll timed out; backing off"},
            {"level": "WARN", "msg": "poll failed; backing off", "status_code": 503},
            {"level": "ERROR", "msg": "failed to forward notification to control plane; continuing to terminal response"},
        ):
            process.stdout = io.StringIO(json.dumps({**event, "error": KEY}) + "\n")
            self.tunnel._consume(process)
            with mock.patch.object(self.tunnel, "_read_health", return_value=True):
                report = self.tunnel.health()
            self.assertEqual(report["state"], "not_ready")
            self.assertTrue(report["running"])
            self.assertEqual(self.signals, [])
            self.assertNotIn(KEY, json.dumps(report))
        process.stdout = io.StringIO(json.dumps({"msg": "poller recovered; polling operational"}) + "\n")
        self.tunnel._consume(process)
        with mock.patch.object(self.tunnel, "_read_health", return_value=True):
            self.assertTrue(self.tunnel.health()["real_connected"])
        process.stdout = io.StringIO(json.dumps({"msg": "poll failed; backing off", "status_code": 403}) + "\n")
        self.tunnel._consume(process)
        self.assertEqual(self.tunnel.status()["error_code"], "auth_failed")
        self.assertFalse(self.tunnel.status()["running"])
        self.assertEqual(len(self.processes), 1)

    def test_historical_error_counters_do_not_override_current_readiness(self) -> None:
        self.start()
        self.health_url()
        for ready, timestamp, expected in ((200, time.time(), True), (503, time.time(), False), (200, 1, False)):
            metrics = (
                f"commands_poll_last_successful_timestamp_seconds {timestamp}\n"
                'commands_poll_errors_total{error_kind="timeout"} 7\n'
                'http_client_requests_total{http_response_status_code="503"} 3\n'
                'http_client_requests_total{http_response_status_code="401"} 1\n'
            )
            with mock.patch(MODULE + "http.client.HTTPConnection") as connection:
                connection.return_value.getresponse.side_effect = [
                    mock.Mock(status=200, read=mock.Mock(return_value=b"ok")),
                    mock.Mock(status=ready, read=mock.Mock(return_value=b"ready")),
                    mock.Mock(status=200, read=mock.Mock(return_value=metrics.encode())),
                ]
                report = self.tunnel.health()
            self.assertEqual(report["real_connected"], expected)
            self.assertTrue(report["running"])
            self.assertEqual(self.signals, [])

    def test_transient_health_timeout_and_process_exit_are_distinct(self) -> None:
        self.start()
        with mock.patch.object(self.tunnel, "_read_health", side_effect=TimeoutError()):
            report = self.tunnel.health()
        self.assertEqual(report["state"], "not_ready")
        self.assertTrue(report["running"])
        self.assertEqual(self.signals, [])
        process = self.processes[-1]
        process.returncode, process.group_alive = 1, False
        report = self.tunnel.status()
        self.assertEqual(report["error_code"], "process_exited")
        self.assertFalse(report["running"])
        self.assertEqual(len(self.processes), 1)

    def test_stop_failure_keeps_unknown_and_blocks_restart(self) -> None:
        self.start()
        self.killpg.side_effect = PermissionError(KEY)
        result = self.tunnel.stop()
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["running"])
        self.assertFalse(result["real_connected"])
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(self.tunnel.start(TUNNEL_ID)["error_code"], "not_stopped")
        self.assertEqual(len(self.processes), 1)
        self.killpg.side_effect = self.kill

    def test_stop_escalates_only_owned_group_and_verifies_descendants(self) -> None:
        self.start()
        process = self.processes[-1]
        process.wait = mock.Mock(side_effect=[subprocess.TimeoutExpired("fake", 4), 0])
        result = self.tunnel.stop()
        self.assertEqual(self.signals, [signal.SIGINT, signal.SIGTERM, 0])
        self.assertEqual(result["state"], "unknown")
        process.group_alive, process.returncode = False, 0
        self.assertEqual(self.tunnel.stop()["state"], "stopped")

    def test_start_failure_and_ownership_mismatch_are_not_reported_as_running(self) -> None:
        self.popen.side_effect = RuntimeError(KEY)
        failed = self.tunnel.start(TUNNEL_ID)
        self.assertEqual(failed["error_code"], "start_failed")
        self.assertFalse(failed["running"])
        self.assertEqual(list(self.root.glob("tunnel-run-*")), [])
        self.popen.side_effect = self.spawn
        self.getpgid.return_value = 888
        mismatch = self.tunnel.start(TUNNEL_ID)
        self.assertEqual(mismatch["state"], "unknown")
        self.assertEqual(self.signals, [])
        self.getpgid.return_value = FakeProcess.pid

    def test_auth_logs_use_fixed_error_categories(self) -> None:
        self.assertEqual(_log_error(json.dumps({"status_code": 401, "error": KEY})), "auth_failed")
        self.assertEqual(_log_error(json.dumps({"level": "ERROR", "msg": KEY})), "not_ready")
        self.assertIsNone(_log_error(KEY))


if __name__ == "__main__":
    unittest.main()
