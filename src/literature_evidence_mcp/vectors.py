from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .catalog import (
    SNAPSHOT_ID_PATTERN,
    _root_guard,
    _validated_root,
    load_snapshot_catalog,
    snapshot_catalog_lock,
    snapshot_record,
)
from .errors import VectorError
from .object_store import (
    _fsync_directory,
    _publish_directory_no_replace,
    _read_regular_file,
    _validated_directory,
    _write_new_file,
)
from .registry import LibraryRegistry
from .snapshot import _open_verified_snapshot


VECTOR_ARTIFACT_FORMAT = "literature-evidence-vector-artifact"
VECTOR_FORMAT_VERSION = 1
DERIVED_DIRECTORY = "derived"
VECTORS_DIRECTORY = "vectors"
VECTOR_PAYLOAD_NAME = "payload"
VECTOR_ARTIFACT_NAME = "manifest.json"
VECTOR_CATALOG_NAME = "catalog.json"
VECTOR_CATALOG_FORMAT = "literature-evidence-vector-artifact-catalog"
VECTOR_ENCODING = {
    "format": "ieee754",
    "bits": 64,
    "byte_order": "big",
}

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_PROFILE_ID = re.compile(r"\Aprofile_[0-9a-f]{64}\Z")
_VECTOR_OBJECT_ID = re.compile(r"\Avec_[0-9a-f]{64}\Z")
_PROFILE_KEYS = {
    "provider",
    "model_id",
    "model_revision",
    "dimensions",
    "input_role",
    "instruction",
    "preprocessing",
}
_CREDENTIAL_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
_FLOAT_BYTES = 8


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise VectorError("向量身份必须是有限、可规范编码的 JSON。") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validated_identifier(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise VectorError(f"{label}格式无效。")
    return value


def _validated_name(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise VectorError(f"向量 profile 的 {label} 必须是明确的非空字符串。")
    return value


def _reject_credentials(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise VectorError("预处理配置的 JSON 对象键必须是字符串。")
            normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            if normalized in _CREDENTIAL_KEYS or normalized.endswith(
                ("_api_key", "_token", "_password", "_secret", "_credential")
            ):
                raise VectorError("向量 profile 不允许包含凭据字段。")
            _reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item)


def validate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return one strict, deeply copied embedding-profile identity."""
    if not isinstance(profile, Mapping):
        raise VectorError("向量 profile 必须是映射。")
    raw = dict(profile)
    if set(raw) != _PROFILE_KEYS:
        raise VectorError("向量 profile 字段不完整或含未允许字段。")

    provider = _validated_name(raw["provider"], "provider")
    model_id = _validated_name(raw["model_id"], "model_id")
    revision = _validated_name(raw["model_revision"], "model_revision")
    if revision.casefold() in {"auto", "current", "default", "latest", "unspecified"}:
        raise VectorError("model_revision 必须是明确版本，不能使用浮动版本别名。")
    dimensions = raw["dimensions"]
    if type(dimensions) is not int or dimensions < 1:
        raise VectorError("向量 dimensions 必须是正整数且不能是 bool。")
    input_role = _validated_name(raw["input_role"], "input_role")
    instruction = raw["instruction"]
    if not isinstance(instruction, str) or "\x00" in instruction:
        raise VectorError("向量 instruction 必须是显式字符串。")

    preprocessing = raw["preprocessing"]
    if not isinstance(preprocessing, Mapping):
        raise VectorError("向量 preprocessing 必须是对象。")
    preprocessing = dict(preprocessing)
    if set(preprocessing) != {"implementation", "version", "config"}:
        raise VectorError("向量 preprocessing 字段不完整或含未允许字段。")
    implementation = _validated_name(
        preprocessing["implementation"], "preprocessing.implementation"
    )
    version = preprocessing["version"]
    if type(version) is not int or version < 1:
        raise VectorError("preprocessing.version 必须是正整数且不能是 bool。")
    config = preprocessing["config"]
    if not isinstance(config, Mapping):
        raise VectorError("preprocessing.config 必须是 JSON 对象。")
    config = json.loads(_canonical_json(dict(config)).decode("utf-8"))
    _reject_credentials(config)

    validated = {
        "provider": provider,
        "model_id": model_id,
        "model_revision": revision,
        "dimensions": dimensions,
        "input_role": input_role,
        "instruction": instruction,
        "preprocessing": {
            "implementation": implementation,
            "version": version,
            "config": config,
        },
    }
    return json.loads(_canonical_json(validated).decode("utf-8"))


def profile_id(profile: Mapping[str, Any]) -> str:
    return "profile_" + _sha256(_canonical_json(validate_profile(profile)))


def offline_fake_profile(
    *,
    dimensions: int = 8,
    input_role: str = "document",
    instruction: str = "Represent the exact evidence chunk for offline deterministic testing.",
) -> dict[str, Any]:
    return validate_profile(
        {
            "provider": "offline-deterministic-fake",
            "model_id": "sha256-seeded-float64",
            "model_revision": "1",
            "dimensions": dimensions,
            "input_role": input_role,
            "instruction": instruction,
            "preprocessing": {
                "implementation": "exact-text-noop",
                "version": 1,
                "config": {
                    "encoding": "utf-8",
                    "newline_normalization": "none",
                    "unicode_normalization": "none",
                    "whitespace": "preserve",
                },
            },
        }
    )


class OfflineDeterministicFakeEmbedder:
    """Deterministic, recordable, offline-only development adapter."""

    offline = True
    simulated = True
    adapter_identity = {
        "implementation": "offline_deterministic_fake",
        "version": 1,
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        texts: Sequence[str],
        profile: Mapping[str, Any],
    ) -> list[list[float]]:
        exact_texts = tuple(texts)
        self.calls.append(exact_texts)
        dimensions = validate_profile(profile)["dimensions"]
        vectors: list[list[float]] = []
        for text in exact_texts:
            seed = _canonical_json({"profile": profile, "text": text})
            raw = hashlib.shake_256(seed).digest(dimensions * _FLOAT_BYTES)
            vectors.append(
                [
                    (int.from_bytes(raw[index : index + 8], "big") / 2**64) * 2.0
                    - 1.0
                    for index in range(0, len(raw), 8)
                ]
            )
        return vectors


OfflineEmbedder = Callable[
    [Sequence[str], Mapping[str, Any]], Sequence[Sequence[int | float]]
]


def _validated_adapter(embedder: OfflineEmbedder) -> dict[str, Any]:
    if not callable(embedder):
        raise VectorError("离线 embedder 必须是可调用对象。")
    if (
        getattr(embedder, "offline", None) is not True
        or getattr(embedder, "simulated", None) is not True
    ):
        raise VectorError("本阶段只接受明确标记 offline/simulated 的 embedder。")
    identity = getattr(embedder, "adapter_identity", None)
    if not isinstance(identity, Mapping) or set(identity) != {
        "implementation",
        "version",
    }:
        raise VectorError("离线 embedder 缺少明确的 adapter identity。")
    implementation = _validated_name(identity["implementation"], "adapter implementation")
    version = identity["version"]
    if type(version) is not int or version < 1:
        raise VectorError("离线 embedder adapter version 无效。")
    return {"implementation": implementation, "version": version}


def _vector_object_id(profile: Mapping[str, Any], text: str) -> str:
    return "vec_" + _sha256(_canonical_json({"profile": profile, "text": text}))


def _relative_object_path(vector_object_id: str, payload_sha256: str) -> str:
    return (
        f"{DERIVED_DIRECTORY}/{VECTORS_DIRECTORY}/objects/"
        f"{vector_object_id}/{payload_sha256}/{VECTOR_PAYLOAD_NAME}"
    )


def _relative_artifact_path(snapshot_id: str, selected_profile_id: str) -> str:
    return (
        f"{DERIVED_DIRECTORY}/{VECTORS_DIRECTORY}/artifacts/"
        f"{snapshot_id}/{selected_profile_id}/{VECTOR_ARTIFACT_NAME}"
    )


def _vector_root(root: Path) -> Path:
    return root / DERIVED_DIRECTORY / VECTORS_DIRECTORY


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise VectorError("无法读取向量派生路径状态。") from exc
    return True


def _ensure_directory(path: Path, *, label: str) -> None:
    created = False
    try:
        path.mkdir()
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise VectorError(f"无法建立{label}。") from exc
    _validated_directory(path, label=label)
    if created:
        _fsync_directory(path.parent)


def _prepare_store(root: Path) -> tuple[Path, Path]:
    _validated_directory(root, label="资料库根目录")
    derived = root / DERIVED_DIRECTORY
    vectors = derived / VECTORS_DIRECTORY
    objects = vectors / "objects"
    artifacts = vectors / "artifacts"
    for path, label in (
        (derived, "derived 目录"),
        (vectors, "向量派生目录"),
        (objects, "向量对象目录"),
        (artifacts, "向量 artifact 目录"),
    ):
        _ensure_directory(path, label=label)
    return objects, artifacts


def _empty_vector_catalog() -> dict[str, Any]:
    return {
        "format": VECTOR_CATALOG_FORMAT,
        "version": VECTOR_FORMAT_VERSION,
        "artifacts": [],
    }


def _validated_vector_catalog(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"format", "version", "artifacts"}:
        raise VectorError("向量 artifact 登记表结构无效。")
    if (
        value["format"] != VECTOR_CATALOG_FORMAT
        or type(value["version"]) is not int
        or value["version"] != VECTOR_FORMAT_VERSION
        or not isinstance(value["artifacts"], list)
    ):
        raise VectorError("向量 artifact 登记表格式或版本无效。")
    identities: list[tuple[str, str]] = []
    for record in value["artifacts"]:
        if not isinstance(record, dict) or set(record) != {
            "snapshot_id",
            "profile_id",
            "path",
            "artifact_sha256",
        }:
            raise VectorError("向量 artifact 登记记录无效。")
        snapshot_id = _validated_identifier(
            record["snapshot_id"], SNAPSHOT_ID_PATTERN, "snapshot_id"
        )
        selected_profile_id = _validated_identifier(
            record["profile_id"], _PROFILE_ID, "profile_id"
        )
        _validated_identifier(record["artifact_sha256"], _SHA256, "artifact SHA-256")
        if record["path"] != _relative_artifact_path(snapshot_id, selected_profile_id):
            raise VectorError("向量 artifact 登记路径与身份不一致。")
        identities.append((snapshot_id, selected_profile_id))
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        raise VectorError("向量 artifact 登记必须唯一且按身份排序。")
    return value


def _load_vector_catalog(root: Path) -> dict[str, Any]:
    vectors = _vector_root(root)
    if not _path_exists(vectors):
        return _empty_vector_catalog()
    _validated_directory(root / DERIVED_DIRECTORY, label="derived 目录")
    _validated_directory(vectors, label="向量派生目录")
    path = vectors / VECTOR_CATALOG_NAME
    if not _path_exists(path):
        return _empty_vector_catalog()
    raw = _read_regular_file(path, label="向量 artifact 登记表")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise VectorError("向量 artifact 登记表不是有效 UTF-8 JSON。") from exc
    if _canonical_json(value) != raw:
        raise VectorError("向量 artifact 登记表不是规范 JSON。")
    return _validated_vector_catalog(value)


def _write_vector_catalog(root: Path, catalog: dict[str, Any]) -> None:
    catalog = _validated_vector_catalog(catalog)
    _objects, _artifacts = _prepare_store(root)
    vectors = _vector_root(root)
    path = vectors / VECTOR_CATALOG_NAME
    if _path_exists(path):
        try:
            status = path.lstat()
        except OSError as exc:
            raise VectorError("无法读取向量 artifact 登记表目标。") from exc
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
        ):
            raise VectorError("向量 artifact 登记表目标无效。")

    payload = _canonical_json(catalog)
    descriptor = -1
    temporary = ""
    committed = False
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".catalog-", dir=vectors)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        committed = True
        temporary = ""
        _fsync_directory(vectors)
    except OSError as exc:
        if committed:
            return
        raise VectorError("无法安全保存向量 artifact 登记表。") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _snapshot_chunks(
    root: Path,
    catalog: dict[str, Any],
    snapshot_id: str,
) -> tuple[dict[str, Any], tuple[tuple[str, str], ...]]:
    record = snapshot_record(catalog, snapshot_id)
    status, database = _open_verified_snapshot(root / "snapshots" / snapshot_id)
    try:
        if status["manifest_sha256"] != record["manifest_sha256"]:
            raise VectorError("向量输入快照与目录册绑定不一致。")
        try:
            rows = database.execute(
                "SELECT chunk_id,chunk_text FROM chunk ORDER BY chunk_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise VectorError("无法读取已核验快照的完整 chunk 映射。") from exc
    finally:
        database.close()
    if not rows:
        raise VectorError("向量 artifact 不能绑定空 chunk 集合。")
    return status, tuple((row["chunk_id"], row["chunk_text"]) for row in rows)


def _expected_inputs(
    profile: Mapping[str, Any], chunks: Sequence[tuple[str, str]]
) -> tuple[list[dict[str, str]], dict[str, str]]:
    mappings: list[dict[str, str]] = []
    texts: dict[str, str] = {}
    for chunk_id, text in chunks:
        vector_object_id = _vector_object_id(profile, text)
        previous = texts.setdefault(vector_object_id, text)
        if previous != text:
            raise VectorError("向量输入身份发生 SHA-256 冲突。")
        mappings.append(
            {"chunk_id": chunk_id, "vector_object_id": vector_object_id}
        )
    return mappings, texts


def _object_record(
    vector_object_id: str,
    text: str,
    payload: bytes,
    *,
    created: bool,
) -> dict[str, Any]:
    digest = _sha256(payload)
    encoded_text = text.encode("utf-8")
    return {
        "vector_object_id": vector_object_id,
        "input_sha256": _sha256(encoded_text),
        "input_byte_size": len(encoded_text),
        "payload_sha256": digest,
        "payload_byte_size": len(payload),
        "path": _relative_object_path(vector_object_id, digest),
        "created_in_artifact": created,
    }


def _read_vector_payload(
    root: Path, record: Mapping[str, Any], dimensions: int
) -> tuple[float, ...]:
    expected = {
        "vector_object_id",
        "input_sha256",
        "input_byte_size",
        "payload_sha256",
        "payload_byte_size",
        "path",
        "created_in_artifact",
    }
    if not isinstance(record, Mapping) or set(record) != expected:
        raise VectorError("向量对象描述符无效。")
    vector_object_id = _validated_identifier(
        record["vector_object_id"], _VECTOR_OBJECT_ID, "vector_object_id"
    )
    payload_sha256 = _validated_identifier(
        record["payload_sha256"], _SHA256, "向量 payload SHA-256"
    )
    _validated_identifier(record["input_sha256"], _SHA256, "向量输入 SHA-256")
    if (
        type(record["input_byte_size"]) is not int
        or record["input_byte_size"] < 0
        or type(record["payload_byte_size"]) is not int
        or record["payload_byte_size"] != dimensions * _FLOAT_BYTES
        or type(record["created_in_artifact"]) is not bool
    ):
        raise VectorError("向量对象字节数、维度或复用状态无效。")
    expected_path = _relative_object_path(vector_object_id, payload_sha256)
    if record["path"] != expected_path:
        raise VectorError("向量对象路径与身份或摘要不一致。")
    path = root.joinpath(*Path(expected_path).parts)
    payload = _read_regular_file(path, label="向量对象 payload")
    if len(payload) != record["payload_byte_size"] or _sha256(payload) != payload_sha256:
        raise VectorError("向量对象 payload 字节数或 SHA-256 不匹配。")
    try:
        values = struct.unpack(f">{dimensions}d", payload)
    except struct.error as exc:
        raise VectorError("向量对象 payload 编码或维度无效。") from exc
    if not all(math.isfinite(value) for value in values):
        raise VectorError("向量对象 payload 含 NaN 或 Inf。")
    return values


def _validate_orphan_object(path: Path) -> dict[str, Any]:
    _validated_directory(path, label="向量对象")
    _validated_identifier(path.name, _VECTOR_OBJECT_ID, "vector_object_id")
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        raise VectorError("无法读取向量对象目录。") from exc
    if len(entries) != 1 or _SHA256.fullmatch(entries[0].name) is None:
        raise VectorError("向量对象目录内容无效。")
    payload_root = entries[0]
    _validated_directory(payload_root, label="向量 payload 摘要目录")
    try:
        payload_entries = {entry.name for entry in payload_root.iterdir()}
    except OSError as exc:
        raise VectorError("无法读取向量 payload 摘要目录。") from exc
    if payload_entries != {VECTOR_PAYLOAD_NAME}:
        raise VectorError("向量 payload 摘要目录内容无效。")
    payload = _read_regular_file(payload_root / VECTOR_PAYLOAD_NAME, label="孤儿向量 payload")
    if not payload or len(payload) % _FLOAT_BYTES != 0 or _sha256(payload) != payload_root.name:
        raise VectorError("孤儿向量对象不完整或摘要不匹配。")
    try:
        values = struct.unpack(f">{len(payload) // _FLOAT_BYTES}d", payload)
    except struct.error as exc:
        raise VectorError("孤儿向量对象编码无效。") from exc
    if not all(math.isfinite(value) for value in values):
        raise VectorError("孤儿向量对象含 NaN 或 Inf。")
    return {
        "vector_object_id": path.name,
        "payload_sha256": payload_root.name,
        "payload_byte_size": len(payload),
        "path": _relative_object_path(path.name, payload_root.name),
    }


def _statistics(
    mappings: Sequence[Mapping[str, Any]],
    objects: Sequence[Mapping[str, Any]],
    dimensions: int,
) -> dict[str, int]:
    payload_bytes = dimensions * _FLOAT_BYTES
    new_objects = sum(1 for item in objects if item["created_in_artifact"])
    unique_inputs = len(objects)
    vector_references = len(mappings)
    return {
        "vector_references": vector_references,
        "unique_inputs": unique_inputs,
        "new_objects": new_objects,
        "reused_objects": unique_inputs - new_objects,
        "referenced_vector_bytes": vector_references * payload_bytes,
        "unique_vector_bytes": unique_inputs * payload_bytes,
        "new_object_bytes": new_objects * payload_bytes,
        "reused_object_bytes": (unique_inputs - new_objects) * payload_bytes,
        "deduplicated_vector_bytes": (vector_references - unique_inputs) * payload_bytes,
    }


def _validated_artifact(
    root: Path,
    value: object,
    status: Mapping[str, Any],
    chunks: Sequence[tuple[str, str]],
    expected_profile_id: str,
) -> tuple[dict[str, Any], dict[str, tuple[float, ...]]]:
    expected_top = {
        "format",
        "version",
        "snapshot",
        "profile_id",
        "profile",
        "generation",
        "encoding",
        "objects",
        "mappings",
        "statistics",
    }
    if not isinstance(value, dict) or set(value) != expected_top:
        raise VectorError("向量 artifact 结构无效。")
    if (
        value["format"] != VECTOR_ARTIFACT_FORMAT
        or type(value["version"]) is not int
        or value["version"] != VECTOR_FORMAT_VERSION
        or value["encoding"] != VECTOR_ENCODING
    ):
        raise VectorError("向量 artifact 格式、版本或编码无效。")
    snapshot = value["snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "snapshot_id",
        "manifest_sha256",
    }:
        raise VectorError("向量 artifact 的快照绑定无效。")
    if (
        snapshot["snapshot_id"] != status["snapshot_id"]
        or snapshot["manifest_sha256"] != status["manifest_sha256"]
    ):
        raise VectorError("向量 artifact 未绑定所选快照 manifest。")

    profile = validate_profile(value["profile"])
    actual_profile_id = profile_id(profile)
    if value["profile_id"] != actual_profile_id or actual_profile_id != expected_profile_id:
        raise VectorError("向量 artifact 的 profile 身份不一致。")
    dimensions = profile["dimensions"]

    generation = value["generation"]
    if not isinstance(generation, dict) or set(generation) != {
        "mode",
        "adapter",
        "embedder_calls",
        "submitted_unique_inputs",
    }:
        raise VectorError("向量 artifact 的离线生成记录无效。")
    adapter = generation["adapter"]
    if (
        generation["mode"] != "offline_simulated"
        or not isinstance(adapter, dict)
        or set(adapter) != {"implementation", "version"}
        or not isinstance(adapter["implementation"], str)
        or not adapter["implementation"]
        or type(adapter["version"]) is not int
        or adapter["version"] < 1
        or type(generation["embedder_calls"]) is not int
        or generation["embedder_calls"] not in {0, 1}
        or type(generation["submitted_unique_inputs"]) is not int
        or generation["submitted_unique_inputs"] < 0
    ):
        raise VectorError("向量 artifact 未明确标记有效的 offline/simulated 生成。")

    expected_mappings, texts = _expected_inputs(profile, chunks)
    mappings = value["mappings"]
    objects = value["objects"]
    if mappings != expected_mappings or not isinstance(objects, list):
        raise VectorError("向量 artifact 不是所选快照的完整 chunk 映射。")
    if not all(isinstance(item, dict) for item in objects):
        raise VectorError("向量 artifact 的对象描述符无效。")
    object_ids = [item.get("vector_object_id") for item in objects]
    if object_ids != sorted(texts) or len(objects) != len(texts):
        raise VectorError("向量 artifact 的唯一对象集合无效或未排序。")

    vectors: dict[str, tuple[float, ...]] = {}
    for record in objects:
        vector_object_id = record.get("vector_object_id")
        if vector_object_id not in texts:
            raise VectorError("向量 artifact 引用了未知输入身份。")
        text = texts[vector_object_id]
        encoded_text = text.encode("utf-8")
        if (
            record.get("input_sha256") != _sha256(encoded_text)
            or record.get("input_byte_size") != len(encoded_text)
        ):
            raise VectorError("向量对象未绑定精确输入文本。")
        vectors[vector_object_id] = _read_vector_payload(root, record, dimensions)

    expected_statistics = _statistics(mappings, objects, dimensions)
    if value["statistics"] != expected_statistics:
        raise VectorError("向量 artifact 统计与完整映射不一致。")
    if (
        generation["submitted_unique_inputs"] != expected_statistics["new_objects"]
        or generation["embedder_calls"] != int(expected_statistics["new_objects"] > 0)
    ):
        raise VectorError("向量 artifact 的假模型调用记录与新增对象不一致。")
    return value, vectors


def _read_artifact(
    root: Path,
    profile_directory: Path,
    status: Mapping[str, Any],
    chunks: Sequence[tuple[str, str]],
    expected_record: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, tuple[float, ...]], dict[str, Any]]:
    _validated_directory(profile_directory, label="向量 artifact profile 目录")
    selected_profile_id = _validated_identifier(
        profile_directory.name, _PROFILE_ID, "profile_id"
    )
    try:
        entries = {entry.name for entry in profile_directory.iterdir()}
    except OSError as exc:
        raise VectorError("无法读取向量 artifact profile 目录。") from exc
    if entries != {VECTOR_ARTIFACT_NAME}:
        raise VectorError("向量 artifact profile 目录内容无效。")
    path = profile_directory / VECTOR_ARTIFACT_NAME
    raw = _read_regular_file(path, label="向量 artifact")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise VectorError("向量 artifact 不是有效 UTF-8 JSON。") from exc
    if _canonical_json(value) != raw:
        raise VectorError("向量 artifact 不是规范 JSON。")
    artifact, vectors = _validated_artifact(
        root, value, status, chunks, selected_profile_id
    )
    artifact_sha256 = _sha256(raw)
    relative_path = _relative_artifact_path(status["snapshot_id"], selected_profile_id)
    if expected_record is not None and (
        expected_record.get("snapshot_id") != status["snapshot_id"]
        or expected_record.get("profile_id") != selected_profile_id
        or expected_record.get("path") != relative_path
        or expected_record.get("artifact_sha256") != artifact_sha256
    ):
        raise VectorError("向量 artifact 与成功登记的路径或摘要不一致。")
    record = {
        "artifact_sha256": artifact_sha256,
        "artifact_byte_size": len(raw),
        "path": relative_path,
    }
    return artifact, vectors, record


def _verify_vector_state(
    root: Path,
    snapshot_catalog: dict[str, Any],
    vector_catalog: dict[str, Any],
    snapshot_cache: dict[str, tuple[dict[str, Any], tuple[tuple[str, str], ...]]] | None = None,
) -> tuple[
    dict[tuple[str, str], tuple[dict[str, Any], dict[str, tuple[float, ...]], dict[str, Any]]],
    dict[str, dict[str, Any]],
]:
    cache = {} if snapshot_cache is None else snapshot_cache
    catalog_records = {
        (record["snapshot_id"], record["profile_id"]): record
        for record in vector_catalog["artifacts"]
    }
    vectors = _vector_root(root)
    if not _path_exists(vectors):
        if catalog_records:
            raise VectorError("向量 artifact 已登记但派生目录缺失。")
        return {}, {}
    _validated_directory(root / DERIVED_DIRECTORY, label="derived 目录")
    _validated_directory(vectors, label="向量派生目录")
    expected_root_entries = {"objects", "artifacts"}
    if _path_exists(vectors / VECTOR_CATALOG_NAME):
        expected_root_entries.add(VECTOR_CATALOG_NAME)
    try:
        if {entry.name for entry in vectors.iterdir()} != expected_root_entries:
            raise VectorError("向量派生目录含未登记条目或缺少固定目录。")
    except OSError as exc:
        raise VectorError("无法读取向量派生目录。") from exc
    objects_root = vectors / "objects"
    artifacts_root = vectors / "artifacts"
    _validated_directory(objects_root, label="向量对象目录")
    _validated_directory(artifacts_root, label="向量 artifact 目录")

    try:
        object_entries = list(objects_root.iterdir())
    except OSError as exc:
        raise VectorError("无法读取向量对象目录。") from exc
    existing_objects = {
        entry.name: _validate_orphan_object(entry) for entry in object_entries
    }

    verified: dict[
        tuple[str, str],
        tuple[dict[str, Any], dict[str, tuple[float, ...]], dict[str, Any]],
    ] = {}
    referenced_objects: dict[str, dict[str, Any]] = {}
    seen_artifacts: dict[tuple[str, str], Path] = {}
    try:
        snapshot_directories = list(artifacts_root.iterdir())
    except OSError as exc:
        raise VectorError("无法读取向量 artifact 目录。") from exc
    for snapshot_directory in snapshot_directories:
        _validated_directory(snapshot_directory, label="向量 artifact snapshot 目录")
        snapshot_id = _validated_identifier(
            snapshot_directory.name, SNAPSHOT_ID_PATTERN, "artifact snapshot_id"
        )
        try:
            profile_directories = list(snapshot_directory.iterdir())
        except OSError as exc:
            raise VectorError("无法读取向量 artifact snapshot 目录。") from exc
        for profile_directory in profile_directories:
            selected_profile_id = _validated_identifier(
                profile_directory.name, _PROFILE_ID, "artifact profile_id"
            )
            key = (snapshot_id, selected_profile_id)
            if key in seen_artifacts:
                raise VectorError("同一 snapshot/profile 存在重复向量 artifact。")
            seen_artifacts[key] = profile_directory
    if set(seen_artifacts) != set(catalog_records):
        raise VectorError("向量 artifact 固定目录与成功登记不一致。")

    for key, catalog_record in catalog_records.items():
        snapshot_id, _selected_profile_id = key
        if snapshot_id not in cache:
            cache[snapshot_id] = _snapshot_chunks(root, snapshot_catalog, snapshot_id)
        status, chunks = cache[snapshot_id]
        artifact, vector_values, record = _read_artifact(
            root,
            seen_artifacts[key],
            status,
            chunks,
            expected_record=catalog_record,
        )
        verified[key] = (artifact, vector_values, record)
        for object_record in artifact["objects"]:
            vector_object_id = object_record["vector_object_id"]
            comparable = {
                name: item
                for name, item in object_record.items()
                if name != "created_in_artifact"
            }
            previous = referenced_objects.setdefault(vector_object_id, comparable)
            if previous != comparable:
                raise VectorError("同一向量输入身份引用了不一致的物理对象。")
    if set(referenced_objects) - set(existing_objects):
        raise VectorError("向量 artifact 引用的对象目录缺失。")
    return verified, existing_objects


def _validated_outputs(
    raw_vectors: object,
    count: int,
    dimensions: int,
) -> list[bytes]:
    if (
        isinstance(raw_vectors, (str, bytes, bytearray))
        or not isinstance(raw_vectors, Sequence)
        or len(raw_vectors) != count
    ):
        raise VectorError("离线 embedder 输出条数与缺失唯一输入不一致。")
    encoded: list[bytes] = []
    for vector in raw_vectors:
        if (
            isinstance(vector, (str, bytes, bytearray))
            or not isinstance(vector, Sequence)
            or len(vector) != dimensions
        ):
            raise VectorError("离线 embedder 输出维度与 profile 不一致。")
        values: list[float] = []
        for value in vector:
            if type(value) not in {int, float}:
                raise VectorError("离线 embedder 只允许 int/float，bool 不被接受。")
            try:
                converted = float(value)
            except (OverflowError, ValueError) as exc:
                raise VectorError("离线 embedder 输出无法表示为有限 float64。") from exc
            if not math.isfinite(converted):
                raise VectorError("离线 embedder 输出含 NaN 或 Inf。")
            values.append(converted)
        try:
            encoded.append(struct.pack(f">{dimensions}d", *values))
        except (OverflowError, struct.error) as exc:
            raise VectorError("离线 embedder 输出无法编码为 float64。") from exc
    return encoded


def _publish_object(
    root: Path, vector_object_id: str, text: str, payload: bytes
) -> dict[str, Any]:
    objects_root, _artifacts = _prepare_store(root)
    target = objects_root / vector_object_id
    if _path_exists(target):
        raise VectorError("已有向量对象缺少可核验 artifact 引用；不会覆盖或重算。")
    descriptor = _object_record(vector_object_id, text, payload, created=True)
    digest = descriptor["payload_sha256"]
    temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=objects_root))
    try:
        payload_root = temporary / digest
        payload_root.mkdir()
        _write_new_file(payload_root / VECTOR_PAYLOAD_NAME, payload)
        _fsync_directory(payload_root)
        _fsync_directory(temporary)
        try:
            _publish_directory_no_replace(temporary, target)
        except FileExistsError as exc:
            raise VectorError("向量对象目标已存在；不会覆盖或静默复用。") from exc
        _fsync_directory(objects_root)
        _read_vector_payload(root, descriptor, len(payload) // _FLOAT_BYTES)
        return descriptor
    finally:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)


def _publish_artifact(
    root: Path,
    artifact: dict[str, Any],
    status: Mapping[str, Any],
    chunks: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    _objects, artifacts_root = _prepare_store(root)
    snapshot_root = artifacts_root / status["snapshot_id"]
    _ensure_directory(snapshot_root, label="向量 artifact snapshot 目录")
    selected_profile_id = artifact["profile_id"]
    target = snapshot_root / selected_profile_id
    if _path_exists(target):
        raise VectorError("向量 artifact 目标已存在；不会覆盖或静默修复。")
    payload = _canonical_json(artifact)
    _validated_artifact(root, artifact, status, chunks, selected_profile_id)
    temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=snapshot_root))
    published = False
    try:
        _write_new_file(temporary / VECTOR_ARTIFACT_NAME, payload)
        _fsync_directory(temporary)
        if _read_regular_file(
            temporary / VECTOR_ARTIFACT_NAME, label="待发布向量 artifact"
        ) != payload:
            raise VectorError("待发布向量 artifact 写后核验失败。")
        try:
            _publish_directory_no_replace(temporary, target)
        except FileExistsError as exc:
            raise VectorError("向量 artifact 目标已存在；不会覆盖。") from exc
        published = True
        _fsync_directory(snapshot_root)
        built, _vectors, record = _read_artifact(root, target, status, chunks)
        if built != artifact:
            raise VectorError("新发布向量 artifact 核验失败。")
        return record
    except BaseException:
        if published and target.exists() and not target.is_symlink():
            try:
                shutil.rmtree(target)
                _fsync_directory(snapshot_root)
            except OSError:
                pass
        raise
    finally:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)


def _artifact_status(
    artifact: Mapping[str, Any], record: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "verified": True,
        "offline": True,
        "simulated": True,
        "snapshot_id": artifact["snapshot"]["snapshot_id"],
        "snapshot_manifest_sha256": artifact["snapshot"]["manifest_sha256"],
        "profile_id": artifact["profile_id"],
        "profile": artifact["profile"],
        "artifact_sha256": record["artifact_sha256"],
        "artifact_byte_size": record["artifact_byte_size"],
        "artifact_path": record["path"],
        "generation": artifact["generation"],
        "statistics": artifact["statistics"],
    }


def build_vectors(
    library: Path | LibraryRegistry,
    snapshot_id: str,
    profile: Mapping[str, Any],
    embedder: OfflineEmbedder,
) -> dict[str, Any]:
    """Build one complete offline/simulated vector mapping under the library lock."""
    snapshot_id = _validated_identifier(snapshot_id, SNAPSHOT_ID_PATTERN, "snapshot_id")
    profile = validate_profile(profile)
    if not profile["provider"].casefold().startswith("offline-"):
        raise VectorError("本阶段 fake profile 的 provider 必须明确使用 offline- 前缀。")
    selected_profile_id = profile_id(profile)
    adapter = _validated_adapter(embedder)
    root_guard = _root_guard(library)

    with snapshot_catalog_lock(root_guard):
        root = _validated_root(root_guard)
        assert root is not None
        snapshot_catalog = load_snapshot_catalog(root_guard)
        status, chunks = _snapshot_chunks(root, snapshot_catalog, snapshot_id)
        vector_catalog = _load_vector_catalog(root)
        verified, existing_objects = _verify_vector_state(
            root,
            snapshot_catalog,
            vector_catalog,
            {snapshot_id: (status, chunks)},
        )
        target_key = (snapshot_id, selected_profile_id)
        if target_key in verified:
            artifact, _vectors, record = verified[target_key]
            if artifact["profile"] != profile:
                raise VectorError("已有向量 artifact 的 profile 与请求不一致。")
            result = _artifact_status(artifact, record)
            result.update(
                {
                    "already_exists": True,
                    "embedder_calls": 0,
                    "submitted_unique_inputs": 0,
                }
            )
            return result

        mappings, texts = _expected_inputs(profile, chunks)
        reusable: dict[str, dict[str, Any]] = {}
        for (_candidate_snapshot, candidate_profile), (
            artifact,
            _vectors,
            _record,
        ) in verified.items():
            if candidate_profile != selected_profile_id:
                continue
            for object_record in artifact["objects"]:
                reusable[object_record["vector_object_id"]] = {
                    **object_record,
                    "created_in_artifact": False,
                }

        for vector_object_id in sorted(texts):
            if vector_object_id in reusable or vector_object_id not in existing_objects:
                continue
            existing = existing_objects[vector_object_id]
            payload = _read_regular_file(
                root.joinpath(*Path(existing["path"]).parts),
                label="未登记孤儿向量 payload",
            )
            orphan_record = _object_record(
                vector_object_id,
                texts[vector_object_id],
                payload,
                created=False,
            )
            if any(
                orphan_record[name] != existing[name]
                for name in ("payload_sha256", "payload_byte_size", "path")
            ):
                raise VectorError("未登记孤儿向量对象的摘要或路径不一致。")
            _read_vector_payload(root, orphan_record, profile["dimensions"])
            reusable[vector_object_id] = orphan_record

        missing_ids = [item for item in sorted(texts) if item not in reusable]
        missing_texts = tuple(texts[vector_object_id] for vector_object_id in missing_ids)
        encoded: list[bytes] = []
        if missing_texts:
            try:
                raw_vectors = embedder(missing_texts, profile)
            except VectorError:
                raise
            except Exception as exc:
                raise VectorError("离线模拟 embedder 调用失败；未重试。") from exc
            encoded = _validated_outputs(
                raw_vectors, len(missing_texts), profile["dimensions"]
            )

        object_records = dict(reusable)
        for vector_object_id, text, payload in zip(
            missing_ids, missing_texts, encoded, strict=True
        ):
            object_records[vector_object_id] = _publish_object(
                root, vector_object_id, text, payload
            )
        ordered_objects = [object_records[item] for item in sorted(texts)]
        statistics = _statistics(mappings, ordered_objects, profile["dimensions"])
        artifact = {
            "format": VECTOR_ARTIFACT_FORMAT,
            "version": VECTOR_FORMAT_VERSION,
            "snapshot": {
                "snapshot_id": snapshot_id,
                "manifest_sha256": status["manifest_sha256"],
            },
            "profile_id": selected_profile_id,
            "profile": profile,
            "generation": {
                "mode": "offline_simulated",
                "adapter": adapter,
                "embedder_calls": int(bool(missing_ids)),
                "submitted_unique_inputs": len(missing_ids),
            },
            "encoding": VECTOR_ENCODING,
            "objects": ordered_objects,
            "mappings": mappings,
            "statistics": statistics,
        }
        record = _publish_artifact(root, artifact, status, chunks)
        catalog_record = {
            "snapshot_id": snapshot_id,
            "profile_id": selected_profile_id,
            "path": record["path"],
            "artifact_sha256": record["artifact_sha256"],
        }
        next_catalog = {
            "format": VECTOR_CATALOG_FORMAT,
            "version": VECTOR_FORMAT_VERSION,
            "artifacts": sorted(
                [*vector_catalog["artifacts"], catalog_record],
                key=lambda item: (item["snapshot_id"], item["profile_id"]),
            ),
        }
        try:
            _write_vector_catalog(root, next_catalog)
        except BaseException:
            registered: bool | None = None
            try:
                persisted = _load_vector_catalog(root)
            except (VectorError, OSError):
                pass
            else:
                registered = any(
                    item["snapshot_id"] == snapshot_id
                    and item["profile_id"] == selected_profile_id
                    and item["artifact_sha256"] == record["artifact_sha256"]
                    for item in persisted["artifacts"]
                )
            if registered is False:
                target = root.joinpath(*Path(record["path"]).parts).parent
                try:
                    shutil.rmtree(target)
                    _fsync_directory(target.parent)
                except OSError:
                    pass
            raise
        result = _artifact_status(artifact, record)
        result.update(
            {
                "already_exists": False,
                "embedder_calls": int(bool(missing_ids)),
                "submitted_unique_inputs": len(missing_ids),
            }
        )
        return result


def _load_verified(
    library: Path | LibraryRegistry,
    snapshot_id: str,
    selected_profile_id: str,
) -> tuple[dict[str, Any], dict[str, tuple[float, ...]], dict[str, Any]]:
    snapshot_id = _validated_identifier(snapshot_id, SNAPSHOT_ID_PATTERN, "snapshot_id")
    selected_profile_id = _validated_identifier(
        selected_profile_id, _PROFILE_ID, "profile_id"
    )
    root_guard = _root_guard(library)
    with snapshot_catalog_lock(root_guard):
        root = _validated_root(root_guard)
        assert root is not None
        snapshot_catalog = load_snapshot_catalog(root_guard)
        status, chunks = _snapshot_chunks(root, snapshot_catalog, snapshot_id)
        vector_catalog = _load_vector_catalog(root)
        verified, _objects = _verify_vector_state(
            root,
            snapshot_catalog,
            vector_catalog,
            {snapshot_id: (status, chunks)},
        )
        try:
            return verified[(snapshot_id, selected_profile_id)]
        except KeyError:
            raise VectorError("找不到固定 snapshot/profile 向量 artifact。") from None


def verify_vectors(
    library: Path | LibraryRegistry,
    snapshot_id: str,
    selected_profile_id: str,
) -> dict[str, Any]:
    """Verify the bound snapshot, complete mapping, and every vector payload."""
    artifact, _vectors, record = _load_verified(
        library, snapshot_id, selected_profile_id
    )
    return _artifact_status(artifact, record)


def load_verified_vectors(
    library: Path | LibraryRegistry,
    snapshot_id: str,
    selected_profile_id: str,
) -> dict[str, Any]:
    """Return a fully verified chunk-to-vector mapping for a later retrieval stage."""
    artifact, vectors, record = _load_verified(
        library, snapshot_id, selected_profile_id
    )
    return {
        **_artifact_status(artifact, record),
        "vectors": [
            {
                "chunk_id": mapping["chunk_id"],
                "vector": vectors[mapping["vector_object_id"]],
            }
            for mapping in artifact["mappings"]
        ],
    }


__all__ = [
    "OfflineDeterministicFakeEmbedder",
    "build_vectors",
    "load_verified_vectors",
    "offline_fake_profile",
    "profile_id",
    "validate_profile",
    "verify_vectors",
]
