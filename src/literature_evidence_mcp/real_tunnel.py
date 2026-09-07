"""Own one foreground Secure MCP Tunnel without persisting its runtime key."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


_TUNNEL_ID = re.compile(r"tunnel_[0-9a-f]{32}\Z")
_METRIC = re.compile(r"^([\w:]+)(?:\{([^}]*)\})?\s+([^\s]+)\s*$")
_STARTUP_SECONDS = 45
_MESSAGES = {
    "stopped": "真实 Tunnel 已停止。",
    "starting": "真实 Tunnel 正在启动，等待本次连接检查。",
    "connected": "本次 Tunnel 已成功轮询；尚未验收 ChatGPT 工具发现。",
    "client_missing": "尚未安装本应用专用 Tunnel 客户端。",
    "mcp_missing": "本应用的 MCP 启动入口尚未就绪。",
    "invalid_tunnel_id": "请输入 OpenAI Platform 创建的有效 Tunnel ID。",
    "key_missing": "尚未保存 OpenAI Tunnel Key，请先填写并保存。",
    "key_unavailable": "无法读取 OpenAI Tunnel Key；请检查钥匙串访问权限。",
    "invalid_key": "OpenAI Tunnel Key 格式无效，请重新填写。",
    "not_stopped": "当前 Tunnel 尚未确认停止，不能启动另一个连接。",
    "start_failed": "Tunnel 启动失败；未自动重启。",
    "auth_failed": "Tunnel 认证失败；已请求停止，未自动重启。",
    "transport_failed": "Tunnel 连接发生错误；已请求停止，未自动重启。",
    "health_failed": "无法确认本次 Tunnel 健康状态；已请求停止。",
    "process_exited": "本次 Tunnel 进程已退出；未自动重启。",
    "stop_failed": "无法确认本次 Tunnel 已停止；状态未知。",
}


def _executable(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and os.access(path, os.X_OK)
    except OSError:
        return False


def _log_error(line: str) -> str | None:
    """Read categories only; never expose or retain raw client output."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    if event.get("status_code") in (401, 403) or event.get("error_code") in (
        "invalid_api_key", "unauthorized", "permission_denied"
    ):
        return "auth_failed"
    if event.get("level") == "ERROR" or event.get("msg") in (
        "poll failed; backing off", "poll timed out; backing off"
    ):
        return "transport_failed"
    return None


class RealTunnel:
    def __init__(self, application_root: Path, key_loader: Callable[[], str]) -> None:
        self._root = Path(application_root).expanduser().resolve()
        self._binary = self._root / "bin" / "tunnel-client"
        self._shim = self._root / "mcp-server"
        self._key_loader = key_loader
        self._lock = threading.RLock()
        self._process: Any = None
        self._pgid: int | None = None
        self._run: Path | None = None
        self._started_at = 0.0
        self._state = "stopped"
        self._error: str | None = None
        self._health_checked = False

    def _report(self, *, passed: bool | None = None) -> dict[str, Any]:
        code = self._error or self._state
        return {
            "state": self._state,
            "passed": self._error is None if passed is None else passed,
            "simulated": False,
            "client_installed": _executable(self._binary),
            "running": None if self._state == "unknown" else self._process is not None,
            "real_connected": self._state == "connected",
            "local_health_checked": self._health_checked,
            "chatgpt_tool_discovery_checked": False,
            "transport_retry_policy": "official_client_backoff",
            "app_restart_count": 0,
            "network_calls": None,
            "error_code": self._error,
            "message": _MESSAGES[code],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._state != "unknown":
                try:
                    exited = self._process.poll() is not None
                except Exception:
                    return self._fail("health_failed")
                if exited:
                    return self._fail("process_exited")
            return self._report()

    def start(self, tunnel_id: str) -> dict[str, Any]:
        with self._lock:
            if self._process is not None or self._state == "unknown":
                report = self._report(passed=False)
                report.update(error_code="not_stopped", message=_MESSAGES["not_stopped"])
                return report
            for valid, code in (
                (isinstance(tunnel_id, str) and _TUNNEL_ID.fullmatch(tunnel_id), "invalid_tunnel_id"),
                (_executable(self._binary), "client_missing"),
                (_executable(self._shim), "mcp_missing"),
            ):
                if not valid:
                    self._error = code
                    return self._report()
            try:
                key = self._key_loader()
            except Exception as error:
                self._error = "key_missing" if getattr(error, "code", None) == "missing" else "key_unavailable"
                return self._report()
            if (
                not isinstance(key, str) or not key.startswith("sk-") or len(key) > 1024
                or any(char.isspace() or ord(char) < 32 or ord(char) > 126 for char in key)
            ):
                self._error = "invalid_key"
                return self._report()
            read_fd = write_fd = None
            try:
                self._run = Path(tempfile.mkdtemp(prefix="tunnel-run-", dir=self._root))
                fake_home = self._run / "home"
                fake_home.mkdir(mode=0o700)
                read_fd, write_fd = os.pipe()
                os.write(write_fd, key.encode("ascii"))
                os.close(write_fd)
                write_fd = None
                key = ""
                command = [
                    str(self._binary), "run",
                    "--control-plane.tunnel-id", tunnel_id,
                    "--control-plane.api-key", f"file:/dev/fd/{read_fd}",
                    "--mcp.command", shlex.quote(str(self._shim)),
                    "--health.listen-addr", "127.0.0.1:0",
                    "--health.url-file", str(self._run / "health.url"),
                    "--pid.file", str(self._run / "tunnel.pid"),
                    "--log.format", "json", "--log.level", "info",
                ]
                self._started_at = time.time()
                self._process = subprocess.Popen(
                    command, cwd=self._root, shell=False, start_new_session=True,
                    env={
                        "HOME": str(fake_home), "XDG_CONFIG_HOME": str(fake_home / ".config"),
                        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                    },
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    pass_fds=(read_fd,),
                )
                self._pgid = self._process.pid
                if os.getpgid(self._process.pid) != self._pgid:
                    raise RuntimeError("process group not isolated")
                self._state, self._error, self._health_checked = "starting", None, False
                threading.Thread(target=self._consume, args=(self._process,), daemon=True).start()
            except Exception:
                return self._fail("start_failed")
            finally:
                key = ""
                for descriptor in (read_fd, write_fd):
                    if descriptor is not None:
                        os.close(descriptor)
            return self._report()

    def _consume(self, process: Any) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            while line := stream.readline(65537):
                code = "transport_failed" if len(line) > 65536 else _log_error(line)
                if code:
                    with self._lock:
                        if self._process is process and self._state != "unknown":
                            self._fail(code)
                    return
        except Exception:
            with self._lock:
                if self._process is process and self._state != "unknown":
                    self._fail("transport_failed")
        finally:
            stream.close()

    def _read_health(self) -> tuple[bool, bool]:
        assert self._run is not None
        path = self._run / "health.url"
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256 or info.st_mtime < self._started_at - 1:
            raise ValueError("invalid health file")
        url = urlsplit(path.read_text(encoding="utf-8").strip())
        if (
            url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username is not None or url.password is not None
            or url.path not in ("", "/") or url.query or url.fragment
        ):
            raise ValueError("invalid health URL")
        payloads: dict[str, str] = {}
        ready = False
        for endpoint in ("/healthz", "/readyz", "/metrics"):
            # HTTPConnection neither follows redirects nor inherits proxy settings.
            connection = http.client.HTTPConnection("127.0.0.1", url.port, timeout=2)
            try:
                connection.request("GET", endpoint)
                response = connection.getresponse()
                payload = response.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    raise ValueError("oversized health response")
                if endpoint == "/readyz" and response.status == 503:
                    continue
                if response.status != 200:
                    raise ValueError("health request failed")
                ready = ready or endpoint == "/readyz"
                payloads[endpoint] = payload.decode("utf-8")
            finally:
                connection.close()
        latest = 0.0
        failed = False
        for line in payloads["/metrics"].splitlines():
            match = _METRIC.fullmatch(line)
            if match is None:
                continue
            name, labels, raw = match.groups()
            value = float(raw)
            if not math.isfinite(value):
                continue
            if name == "commands_poll_last_successful_timestamp_seconds":
                latest = max(latest, value)
            if name == "commands_poll_errors_total" and value > 0:
                failed = True
            if name == "http_client_requests_total" and labels and value > 0:
                failed = failed or bool(re.search(r'http_response_status_code="[45]\d\d"', labels))
        return ready and latest >= self._started_at - 1, failed

    def health(self) -> dict[str, Any]:
        with self._lock:
            if self._process is None or self._state == "unknown":
                return self._report(passed=False)
            try:
                if self._process.poll() is not None:
                    return self._fail("process_exited")
                try:
                    connected, failed = self._read_health()
                except FileNotFoundError:
                    if self._state == "starting" and time.time() - self._started_at < _STARTUP_SECONDS:
                        return self._report()
                    raise
                self._health_checked = True
                if failed:
                    return self._fail("transport_failed")
                if connected:
                    self._state = "connected"
                elif self._state == "connected" or time.time() - self._started_at >= _STARTUP_SECONDS:
                    return self._fail("health_failed")
            except Exception:
                return self._fail("health_failed")
            return self._report()

    def _stop_owned(self) -> bool:
        process, pgid = self._process, self._pgid
        if process is not None:
            try:
                if pgid != process.pid:
                    return False
                if process.poll() is None:
                    if os.getpgid(process.pid) != pgid:
                        return False
                    os.killpg(pgid, signal.SIGINT)
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        if os.getpgid(process.pid) != pgid:
                            return False
                        os.killpg(pgid, signal.SIGTERM)
                        process.wait(timeout=3)
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    pass
                else:
                    return False
            except Exception:
                return False
        self._process, self._pgid = None, None
        if self._run is not None:
            try:
                shutil.rmtree(self._run)
            except OSError:
                return False
            self._run = None
        self._state, self._health_checked = "stopped", False
        return True

    def _fail(self, code: str) -> dict[str, Any]:
        if self._stop_owned():
            self._state, self._error = "error", code
        else:
            self._state, self._error = "unknown", "stop_failed"
        self._health_checked = False
        return self._report(passed=False)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._stop_owned():
                self._error = None
            else:
                self._state, self._error = "unknown", "stop_failed"
            return self._report()

    def close(self) -> None:
        self.stop()
