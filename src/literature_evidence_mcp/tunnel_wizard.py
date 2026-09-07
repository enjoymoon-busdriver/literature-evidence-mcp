from __future__ import annotations

from typing import Any


SIMULATED_START_INTENT = "tunnel-simulated-start"
SIMULATED_HEALTH_INTENT = "tunnel-simulated-health"
SIMULATED_STOP_INTENT = "tunnel-simulated-stop"
PRODUCTION_START_INTENT = "tunnel-production-start"

_STOPPED = "模拟停止"
_RUNNING = "模拟运行"
_HEALTHY = "模拟通过"
_UNKNOWN = "模拟状态未知"


def production_boundary_report() -> dict[str, Any]:
    return {
        "state": "real-approval-required",
        "available": False,
        "adapter_configured": False,
        "real_connected": False,
        "runtime_api_key_collected": False,
        "network_calls": 0,
        "retry_count": 0,
        "message": (
            "真实 Secure MCP Tunnel 未获本阶段授权；必须另行批准后才可配置或启动。"
        ),
    }


def tunnel_wizard_guide() -> dict[str, Any]:
    return {
        "stage": 9,
        "mode": "offline_simulation_only",
        "demo_profile": {
            "kind": "演示配置模板",
            "label": "Stage 9 离线 Tunnel 演示（不是真实 Tunnel 配置）",
            "tunnel_id_template": "tunnel_<未来获批后由 OpenAI Platform 创建>",
            "tunnel_id_configured": False,
            "local_mcp": "本项目固定八个只读 stdio MCP 工具",
            "transport": "进程内离线替身",
        },
        "permissions_zh": [
            "OpenAI Platform 组织必须与所连接的 ChatGPT 工作区关联。",
            "在 Platform 创建 Tunnel 需要 Read + Manage；选择或运行已有 Tunnel 需要 Read + Use。",
            "ChatGPT developer mode 中的创建者需要相应 Tunnel 的 Read + Use。",
        ],
        "runtime_api_key_collected": False,
        "health_scope_zh": (
            "本地 healthz/readyz 只表示本地进程诊断；不等于 ChatGPT 已发现或可调用工具。"
        ),
        "stop_checkpoint_zh": (
            "未来单独批准前，先报告预计问题数和总调用数、发送的查询/摘录范围、"
            "模型与成本可见性、零自动重试与首错停止规则，以及需要用户完成的电脑操作。"
        ),
        "production": production_boundary_report(),
    }


class OfflineTunnelAdapter:
    """In-memory fake used only by the Stage 9 offline wizard."""

    simulated = True

    def __init__(self) -> None:
        self._running = False

    def start(self) -> None:
        self._running = True

    def health(self) -> bool:
        return self._running

    def stop(self) -> None:
        self._running = False


class TunnelSimulation:
    def __init__(self, adapter: Any | None = None) -> None:
        selected = OfflineTunnelAdapter() if adapter is None else adapter
        if selected.simulated is not True or any(
            not callable(getattr(selected, action, None))
            for action in ("start", "health", "stop")
        ):
            raise ValueError("Tunnel 离线替身配置无效。")
        self._adapter = selected
        self._state = _STOPPED
        self._running: bool | None = False
        self._local_health_checked = False

    def _report(
        self,
        action: str,
        *,
        passed: bool,
        message: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        report = {
            "mode": "offline_simulation",
            "action": action,
            "state": self._state,
            "passed": passed,
            "simulated": True,
            "simulated_running": self._running,
            "real_connected": False,
            "local_health_checked": self._local_health_checked,
            "chatgpt_tool_discovery_checked": False,
            "network_calls": 0,
            "model_calls": 0,
            "api_keys_collected": 0,
            "external_config_writes": 0,
            "retry_count": 0,
            "message": message,
        }
        if error_code is not None:
            report["error_code"] = error_code
        return report

    def snapshot(self) -> dict[str, Any]:
        return self._report(
            "status",
            passed=self._state != _UNKNOWN,
            message=f"离线替身当前为{self._state}；未建立真实连接。",
        )

    def _cleanup(self) -> bool:
        try:
            self._adapter.stop()
        except Exception:
            self._state = _UNKNOWN
            self._running = None
            self._local_health_checked = False
            return False
        self._state = _STOPPED
        self._running = False
        self._local_health_checked = False
        return True

    def start(self) -> tuple[int, dict[str, Any]]:
        if self._running is not False:
            return 409, self._report(
                "start",
                passed=False,
                message="离线替身不是模拟停止状态；本次未启动、未重试。",
                error_code="simulation-not-stopped",
            )
        try:
            self._adapter.start()
        except Exception:
            cleaned = self._cleanup()
            return 503, self._report(
                "start",
                passed=False,
                message=(
                    "模拟启动首个步骤失败；已停止且未重试。"
                    if cleaned
                    else "模拟启动首个步骤失败，清理停止也失败；模拟状态未知且未重试。"
                ),
                error_code="simulated-start-failed",
            )
        self._state = _RUNNING
        self._running = True
        self._local_health_checked = False
        return 200, self._report(
            "start",
            passed=True,
            message="离线替身已模拟运行；这不是 Secure MCP Tunnel 真实连接。",
        )

    def health(self) -> tuple[int, dict[str, Any]]:
        if self._running is not True:
            return 409, self._report(
                "health",
                passed=False,
                message="离线替身未处于模拟运行；未执行健康检查。",
                error_code="simulation-not-running",
            )
        try:
            healthy = self._adapter.health()
            if healthy is not True:
                raise RuntimeError("offline adapter did not report healthy")
        except Exception:
            cleaned = self._cleanup()
            return 503, self._report(
                "health",
                passed=False,
                message=(
                    "模拟健康检查首错停止；离线替身已停止且未重试。"
                    if cleaned
                    else "模拟健康检查失败，清理停止也失败；模拟状态未知且未重试。"
                ),
                error_code="simulated-health-failed",
            )
        self._state = _HEALTHY
        self._running = True
        self._local_health_checked = True
        return 200, self._report(
            "health",
            passed=True,
            message=(
                "本地离线健康检查模拟通过；未检查 ChatGPT 工具发现，也未建立真实连接。"
            ),
        )

    def stop(self) -> tuple[int, dict[str, Any]]:
        if self._running is False:
            return 200, self._report(
                "stop",
                passed=True,
                message="离线替身已是模拟停止；未执行网络操作。",
            )
        try:
            self._adapter.stop()
        except Exception:
            self._state = _UNKNOWN
            self._running = None
            self._local_health_checked = False
            return 503, self._report(
                "stop",
                passed=False,
                message="模拟停止失败；模拟状态未知且未重试。",
                error_code="simulated-stop-failed",
            )
        self._state = _STOPPED
        self._running = False
        self._local_health_checked = False
        return 200, self._report(
            "stop",
            passed=True,
            message="离线替身已模拟停止；未建立或保留真实连接。",
        )


__all__ = [
    "OfflineTunnelAdapter",
    "PRODUCTION_START_INTENT",
    "SIMULATED_HEALTH_INTENT",
    "SIMULATED_START_INTENT",
    "SIMULATED_STOP_INTENT",
    "TunnelSimulation",
    "production_boundary_report",
    "tunnel_wizard_guide",
]
