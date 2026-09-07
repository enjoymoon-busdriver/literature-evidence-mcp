"""Local connection settings; secrets are held only by the native credential store."""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path
from typing import Any

from .errors import EnhancedSearchError, LiteratureEvidenceError
from .registry import LibraryRegistry


class ConnectionError(LiteratureEvidenceError):
    pass


class Connections:
    def __init__(self, application_root: Path, *, credentials: Any = None, tunnel: Any = None):
        from .credentials import MacOSKeychain
        from .real_tunnel import RealTunnel

        self.registry = LibraryRegistry(application_root)
        self.root = self.registry.application_root
        self.credentials = credentials if credentials is not None else MacOSKeychain(self.root)
        self.tunnel = tunnel if tunnel is not None else RealTunnel(
            self.root, lambda: self.credentials.get("openai_tunnel")
        )
        self._lock = threading.Lock()
        self.enhanced = _ConfiguredSearch(self)

    def settings(self) -> dict[str, Any]:
        empty = {"version": 1, "aliyun_configured": False,
                 "openai_tunnel_configured": False, "tunnel_id": "",
                 "tunnel_backoff_accepted": False}
        try:
            with self.registry._application_root(create=False) as directory:
                if directory is None:
                    return empty
                try:
                    fd = os.open("connections.json", os.O_RDONLY | os.O_NOFOLLOW,
                                 dir_fd=directory)
                except FileNotFoundError:
                    return empty
                with os.fdopen(fd, "rb") as source:
                    status = os.fstat(source.fileno())
                    if not stat.S_ISREG(status.st_mode) or status.st_size > 4096:
                        raise ValueError
                    value = json.load(source)
            if (not isinstance(value, dict) or set(value) != set(empty)
                    or type(value["version"]) is not int or value["version"] != 1
                    or any(type(value[k]) is not bool for k in
                           ("aliyun_configured", "openai_tunnel_configured", "tunnel_backoff_accepted"))
                    or not isinstance(value["tunnel_id"], str)
                    or (value["tunnel_id"] and not re.fullmatch(r"tunnel_[0-9a-f]{32}", value["tunnel_id"]))):
                raise ValueError
            return value
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise ConnectionError("本机连接设置无法读取；未尝试联网。") from None

    def _write(self, value: dict[str, Any]) -> None:
        with self.registry._application_root(create=True) as directory:
            temporary = f".connections-{secrets.token_hex(8)}.tmp"
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=directory)
                with os.fdopen(fd, "w", encoding="utf-8") as target:
                    json.dump(value, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, "connections.json", src_dir_fd=directory,
                           dst_dir_fd=directory)
                os.fsync(directory)
            except OSError:
                raise ConnectionError("本机连接设置保存失败。") from None
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    def save_key(self, kind: str, key: str) -> dict[str, Any]:
        if not isinstance(kind, str) or kind not in {"aliyun", "openai_tunnel"}:
            raise ConnectionError("不支持此凭据类型。")
        with self._lock:
            value = self.settings()
            self.credentials.set(kind, key)
            value[f"{kind}_configured"] = True
            self._write(value)
        return self.status()

    def save_tunnel(self, tunnel_id: str, accept_backoff: bool) -> dict[str, Any]:
        if (not isinstance(tunnel_id, str)
                or not re.fullmatch(r"tunnel_[0-9a-f]{32}", tunnel_id)
                or type(accept_backoff) is not bool):
            raise ConnectionError("Tunnel ID 或重连选项无效。")
        with self._lock:
            value = self.settings()
            value.update(tunnel_id=tunnel_id, tunnel_backoff_accepted=accept_backoff)
            self._write(value)
        return self.status()

    def status(self) -> dict[str, Any]:
        return {**self.settings(), "credential_storage": "macos_keychain",
                "models": self.enhanced.public_summary(), "tunnel": self.tunnel.status()}

    def preview_vectors(self, library_id: str, snapshot_id: str) -> dict[str, Any]:
        from .aliyun import aliyun_profile
        from .vectors import preview_vectors
        root = self.registry.library_path(library_id)
        return {"library_id": library_id, "snapshot_id": snapshot_id,
                **preview_vectors(root, snapshot_id, aliyun_profile())}

    def build_vectors(self, library_id: str, snapshot_id: str) -> dict[str, Any]:
        from .aliyun import AliyunEmbedder, aliyun_profile
        from .vectors import build_vectors
        if not self.settings()["aliyun_configured"]:
            raise ConnectionError("请先在本机填写阿里云 API Key。")
        root = self.registry.library_path(library_id)
        result = build_vectors(root, snapshot_id, aliyun_profile(),
                               AliyunEmbedder(lambda: self.credentials.get("aliyun")))
        # Internal object locations are unnecessary for the management page.
        return {"library_id": library_id, "snapshot_id": snapshot_id,
                **{k: v for k, v in result.items() if not k.endswith("path")}}

    def start_tunnel(self) -> dict[str, Any]:
        settings = self.settings()
        if not settings["openai_tunnel_configured"] or not settings["tunnel_id"]:
            raise ConnectionError("请先填写 OpenAI 运行 Key 和本项目 Tunnel ID。")
        if not settings["tunnel_backoff_accepted"]:
            raise ConnectionError("真实 Tunnel 使用官方客户端断线重连；需先确认此行为。")
        return self.tunnel.start(settings["tunnel_id"])


class _ConfiguredSearch:
    """Resolve settings only for an explicitly requested enhanced search."""
    def __init__(self, connections: Connections):
        self.connections = connections

    def public_summary(self) -> dict[str, Any]:
        from .aliyun import AliyunTransport, enhanced_config
        from .enhanced import EnhancedSearchService
        return EnhancedSearchService(enhanced_config(), AliyunTransport(
            lambda: self.connections.credentials.get("aliyun")
        )).public_summary()

    def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from .aliyun import AliyunTransport, enhanced_config
        from .enhanced import EnhancedSearchService
        if not self.connections.settings()["aliyun_configured"]:
            raise EnhancedSearchError("增强搜索尚未配置；请先在本机填写阿里云 API Key。")
        transport = AliyunTransport(lambda: self.connections.credentials.get("aliyun"))
        service = EnhancedSearchService(enhanced_config(), transport)
        try:
            result = service.search(*args, **kwargs)
        except EnhancedSearchError as exc:
            audit = dict(exc.audit)
            audit["call_count"] = transport.last_audit["call_count"]
            audit["calls"] = audit["calls"][:audit["call_count"]]
            raise EnhancedSearchError(str(exc), audit) from None
        result["provider_audit"] = transport.last_audit
        return result
