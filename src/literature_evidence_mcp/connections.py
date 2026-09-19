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


MODEL_ROLES = ("query_rewrite", "vector_recall", "candidate_rerank")


def _recommended_model_ids() -> dict[str, str]:
    from .aliyun import EMBEDDING_MODEL, RERANK_MODEL, REWRITE_MODEL
    return {
        "query_rewrite": REWRITE_MODEL,
        "vector_recall": EMBEDDING_MODEL,
        "candidate_rerank": RERANK_MODEL,
    }


def _empty_settings() -> dict[str, Any]:
    return {
        "version": 2,
        "aliyun_configured": False,
        "openai_tunnel_configured": False,
        "tunnel_id": "",
        "tunnel_backoff_accepted": False,
        "models_enabled": False,
        "model_ids": _recommended_model_ids(),
    }


class ConnectionError(LiteratureEvidenceError):
    pass


class Connections:
    def __init__(
        self,
        application_root: Path,
        *,
        credentials: Any = None,
        tunnel: Any = None,
        transport_factory: Any = None,
        embedder_factory: Any = None,
    ):
        from .credentials import MacOSKeychain
        from .real_tunnel import RealTunnel

        self.registry = LibraryRegistry(application_root)
        self.root = self.registry.application_root
        self.credentials = credentials if credentials is not None else MacOSKeychain(self.root)
        self.tunnel = tunnel if tunnel is not None else RealTunnel(
            self.root, lambda: self.credentials.get("openai_tunnel")
        )
        self._lock = threading.Lock()
        self._vector_operations: dict[tuple[str, str, str], str] = {}
        self._transport_factory = transport_factory
        self._embedder_factory = embedder_factory
        self.enhanced = _ConfiguredSearch(self)

    def settings(self) -> dict[str, Any]:
        empty = _empty_settings()
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
            legacy_keys = set(empty) - {"models_enabled", "model_ids"}
            if isinstance(value, dict) and set(value) == legacy_keys and value.get("version") == 1:
                value = {**value, "version": 2, "models_enabled": False,
                         "model_ids": _recommended_model_ids()}
            if (not isinstance(value, dict) or set(value) != set(empty)
                    or type(value["version"]) is not int or value["version"] != 2
                    or any(type(value[k]) is not bool for k in
                           ("aliyun_configured", "openai_tunnel_configured",
                            "tunnel_backoff_accepted", "models_enabled"))
                    or not isinstance(value["tunnel_id"], str)
                    or (value["tunnel_id"] and not re.fullmatch(r"tunnel_[0-9a-f]{32}", value["tunnel_id"]))
                    or not isinstance(value["model_ids"], dict)
                    or set(value["model_ids"]) != set(MODEL_ROLES)):
                raise ValueError
            self._config(value)
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

    def _config(self, settings: dict[str, Any] | None = None) -> Any:
        from .aliyun import enhanced_config
        value = self.settings() if settings is None else settings
        try:
            return enhanced_config(
                query_rewrite_model_id=value["model_ids"]["query_rewrite"],
                vector_recall_model_id=value["model_ids"]["vector_recall"],
                candidate_rerank_model_id=value["model_ids"]["candidate_rerank"],
            )
        except (KeyError, TypeError, ValueError):
            raise ConnectionError("模型设置无效；未尝试联网。") from None

    def model_settings(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        from .aliyun import PROVIDER
        value = self.settings() if settings is None else settings
        return {
            "enabled": value["models_enabled"],
            "provider": PROVIDER,
            "region": "cn-beijing",
            "model_ids": dict(value["model_ids"]),
            "recommended_model_ids": _recommended_model_ids(),
        }

    def save_models(self, enabled: bool, model_ids: dict[str, str]) -> dict[str, Any]:
        if (type(enabled) is not bool or not isinstance(model_ids, dict)
                or set(model_ids) != set(MODEL_ROLES)):
            raise ConnectionError("模型设置格式无效。")
        with self._lock:
            value = self.settings()
            candidate = {**value, "models_enabled": enabled, "model_ids": dict(model_ids)}
            self._config(candidate)
            self._write(candidate)
        return self.status()

    def restore_recommended_models(self) -> dict[str, Any]:
        with self._lock:
            value = self.settings()
            value["model_ids"] = _recommended_model_ids()
            self._write(value)
        return self.status()

    def _transport(self, config: Any) -> Any:
        from .aliyun import AliyunTransport
        factory = self._transport_factory or AliyunTransport
        return factory(lambda: self.credentials.get("aliyun"), config)

    def _embedder(self, config: Any) -> Any:
        from .aliyun import AliyunEmbedder
        factory = self._embedder_factory or AliyunEmbedder
        return factory(lambda: self.credentials.get("aliyun"), config)

    @staticmethod
    def _check_classification(audit: dict[str, Any]) -> str:
        calls = audit.get("calls", [])
        status = calls[-1].get("http_status") if calls else None
        if status in {401, 403}:
            return "authentication_failed"
        if status == 429:
            return "rate_limited"
        if status in {400, 404}:
            return "model_unavailable"
        if type(status) is int and status >= 500:
            return "provider_unavailable"
        if audit.get("call_count") == 0:
            return "credential_unavailable"
        return "connection_failed"

    def check_models(self) -> dict[str, Any]:
        from .aliyun import AliyunError
        settings = self.settings()
        config = self._config(settings)
        if not settings["aliyun_configured"]:
            results = [{"role": role, "model_id": config.model_id(role), "passed": False,
                        "classification": "not_configured",
                        "message": "请先保存本项目的阿里云 API Key。",
                        "audit": {"call_count": 0, "calls": []}}
                       for role in MODEL_ROLES]
            return {"passed": False, "results": results}
        payloads = {
            "query_rewrite": {"query": "FolioHook 连接检查"},
            "vector_recall": {"query": "FolioHook 连接检查"},
            "candidate_rerank": {"query": "FolioHook 连接检查", "candidates": [
                {"chunk_id": "chunk_" + "0" * 24, "text": "FolioHook connection check."}
            ]},
        }
        results = []
        for role in MODEL_ROLES:
            transport = self._transport(config)
            try:
                transport.invoke(role=role, provider=config.provider,
                                 model_id=config.model_id(role), payload=payloads[role])
            except Exception as exc:
                audit = dict(getattr(transport, "last_audit", {"call_count": 0, "calls": []}))
                results.append({"role": role, "model_id": config.model_id(role),
                                "passed": False,
                                "classification": self._check_classification(audit),
                                "message": str(exc) if isinstance(exc, AliyunError)
                                else "模型连接检查失败；未重试。", "audit": audit})
            else:
                results.append({"role": role, "model_id": config.model_id(role),
                                "passed": True, "classification": "available",
                                "message": "连接检查通过。", "audit": transport.last_audit})
        return {"passed": all(item["passed"] for item in results), "results": results}

    def status(self) -> dict[str, Any]:
        settings = self.settings()
        return {**settings, "credential_storage": "macos_keychain",
                "model_settings": self.model_settings(settings),
                "models": self.enhanced.public_summary(settings),
                "tunnel": self.tunnel.status()}

    def vector_states(self, library_id: str) -> dict[str, str]:
        from .vectors import profile_id, vector_readiness
        profile = self._config().vector_profile
        selected_profile = profile_id(profile)
        try:
            states = vector_readiness(self.registry.library_path(library_id), profile)
        except (LiteratureEvidenceError, OSError):
            states = {}
        with self._lock:
            for (library, snapshot, identity), operation in self._vector_operations.items():
                if library == library_id and identity == selected_profile:
                    if operation == "preparing" or states.get(snapshot) != "ready":
                        states[snapshot] = operation
        return states

    def document_vector_states(self, library_id: str, snapshot_id: str) -> dict[str, Any]:
        from .vectors import document_vector_readiness
        try:
            return document_vector_readiness(self.registry.library_path(library_id),
                                             snapshot_id, self._config().vector_profile)
        except (LiteratureEvidenceError, OSError):
            return {}

    def preview_vectors(self, library_id: str, snapshot_id: str) -> dict[str, Any]:
        from .vectors import preview_vectors
        config = self._config()
        root = self.registry.library_path(library_id)
        return {"library_id": library_id, "snapshot_id": snapshot_id,
                **preview_vectors(root, snapshot_id, config.vector_profile)}

    def build_vectors(self, library_id: str, snapshot_id: str) -> dict[str, Any]:
        from .vectors import build_vectors, profile_id
        settings = self.settings()
        if not settings["models_enabled"]:
            raise ConnectionError("增强模型当前已关闭。")
        if not settings["aliyun_configured"]:
            raise ConnectionError("请先在本机填写阿里云 API Key。")
        config = self._config(settings)
        root = self.registry.library_path(library_id)
        operation = (library_id, snapshot_id, profile_id(config.vector_profile))
        with self._lock:
            self._vector_operations[operation] = "preparing"
        try:
            result = build_vectors(root, snapshot_id, config.vector_profile,
                                   self._embedder(config))
        except Exception:
            with self._lock:
                self._vector_operations[operation] = "failed"
            raise
        else:
            with self._lock:
                self._vector_operations.pop(operation, None)
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

    def public_summary(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        from .enhanced import EnhancedSearchService
        config = self.connections._config(settings)
        return EnhancedSearchService(config, self.connections._transport(config)).public_summary()

    def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from .enhanced import EnhancedSearchService
        settings = self.connections.settings()
        if not settings["models_enabled"]:
            raise EnhancedSearchError("增强搜索当前不可用：增强模型已关闭。")
        if not settings["aliyun_configured"]:
            raise EnhancedSearchError("增强搜索尚未配置；请先在本机填写阿里云 API Key。")
        config = self.connections._config(settings)
        transport = self.connections._transport(config)
        service = EnhancedSearchService(config, transport)
        try:
            result = service.search(*args, **kwargs)
        except EnhancedSearchError as exc:
            audit = dict(exc.audit)
            audit["call_count"] = transport.last_audit["call_count"]
            audit["calls"] = audit["calls"][:audit["call_count"]]
            raise EnhancedSearchError(str(exc), audit) from None
        result["provider_audit"] = transport.last_audit
        return result
