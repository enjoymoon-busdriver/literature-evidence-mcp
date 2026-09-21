from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import LibraryRegistryError, SnapshotError
from .platform_fs import (
    file_lock,
    fsync_directory,
    open_file,
    open_path_file,
    replace_file,
    status_is_reparse,
)
from .registry import LibraryRegistry, _identity


CATALOG_FORMAT = "literature-evidence-snapshot-catalog"
CATALOG_VERSION = 1
CATALOG_NAME = "snapshot-catalog.json"
CATALOG_LOCK_NAME = ".snapshot-catalog.lock"
SNAPSHOT_ID_PATTERN = re.compile(
    r"\A[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}-[0-9a-f]{8}\Z"
)
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_CATALOG_MAX_BYTES = 4 * 1024 * 1024


def empty_snapshot_catalog() -> dict[str, Any]:
    return {
        "format": CATALOG_FORMAT,
        "version": CATALOG_VERSION,
        "current_snapshot_id": None,
        "last_successful_snapshot_id": None,
        "snapshots": [],
    }


def _root_guard(library: Path | LibraryRegistry) -> LibraryRegistry:
    if isinstance(library, LibraryRegistry):
        return library
    try:
        return LibraryRegistry(Path(library).expanduser())
    except (LibraryRegistryError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SnapshotError("无法固定资料库根目录。") from exc


def _validated_root(
    library: Path | LibraryRegistry,
    *,
    allow_missing: bool = False,
    create: bool = False,
) -> Path | None:
    guard = _root_guard(library)
    try:
        with guard._application_root(create=create) as descriptor:
            if descriptor is None:
                if allow_missing:
                    return None
                raise SnapshotError("资料库尚未安全建立。")
    except LibraryRegistryError as exc:
        raise SnapshotError("资料库根目录身份无效或已经改变。") from exc
    return guard.application_root


def _validate_catalog(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "format",
        "version",
        "current_snapshot_id",
        "last_successful_snapshot_id",
        "snapshots",
    }:
        raise SnapshotError("快照目录册结构无效。")
    if (
        payload["format"] != CATALOG_FORMAT
        or type(payload["version"]) is not int
        or payload["version"] != CATALOG_VERSION
    ):
        raise SnapshotError("快照目录册格式或版本不受支持。")
    snapshots = payload["snapshots"]
    if not isinstance(snapshots, list):
        raise SnapshotError("快照目录册 snapshots 必须是列表。")

    seen: set[str] = set()
    for record in snapshots:
        if not isinstance(record, dict) or set(record) != {
            "snapshot_id",
            "manifest_sha256",
            "base_snapshot_id",
        }:
            raise SnapshotError("快照目录册记录无效。")
        snapshot_id = record["snapshot_id"]
        manifest_sha256 = record["manifest_sha256"]
        base_snapshot_id = record["base_snapshot_id"]
        if (
            not isinstance(snapshot_id, str)
            or SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
            or snapshot_id in seen
        ):
            raise SnapshotError("快照目录册中的 snapshot_id 无效或重复。")
        if (
            not isinstance(manifest_sha256, str)
            or _SHA256_PATTERN.fullmatch(manifest_sha256) is None
        ):
            raise SnapshotError("快照目录册中的 manifest_sha256 无效。")
        if base_snapshot_id is not None and (
            not isinstance(base_snapshot_id, str) or base_snapshot_id not in seen
        ):
            raise SnapshotError("快照目录册中的 base_snapshot_id 无效。")
        seen.add(snapshot_id)

    current = payload["current_snapshot_id"]
    last_successful = payload["last_successful_snapshot_id"]
    if current is not None and (
        not isinstance(current, str)
        or SNAPSHOT_ID_PATTERN.fullmatch(current) is None
    ):
        raise SnapshotError("快照目录册的 current_snapshot_id 无效。")
    if last_successful is not None and (
        not isinstance(last_successful, str)
        or SNAPSHOT_ID_PATTERN.fullmatch(last_successful) is None
    ):
        raise SnapshotError("快照目录册的 last_successful_snapshot_id 无效。")
    if snapshots:
        if current not in seen or last_successful != snapshots[-1]["snapshot_id"]:
            raise SnapshotError("快照目录册的当前或上次成功指针无效。")
    elif current is not None or last_successful is not None:
        raise SnapshotError("空快照目录册不能包含当前或上次成功指针。")
    return payload


def _unregistered_snapshot_exists(root: Path) -> bool:
    snapshots = root / "snapshots"
    try:
        status = snapshots.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SnapshotError("无法读取 snapshots 目录状态。") from exc
    if status_is_reparse(status) or not stat.S_ISDIR(status.st_mode):
        raise SnapshotError("snapshots 必须是普通目录且不能是符号链接。")
    try:
        with os.scandir(snapshots) as entries:
            return any(
                SNAPSHOT_ID_PATTERN.fullmatch(entry.name) is not None
                and entry.is_dir(follow_symlinks=False)
                for entry in entries
            )
    except OSError as exc:
        raise SnapshotError("无法检查未登记快照。") from exc


def snapshot_catalog_exists(library: Path | LibraryRegistry) -> bool:
    root = _validated_root(library)
    assert root is not None
    try:
        status = (root / CATALOG_NAME).lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SnapshotError("无法读取快照目录册状态。") from exc
    if status_is_reparse(status) or not stat.S_ISREG(status.st_mode):
        raise SnapshotError(
            "快照目录册必须是普通文件且不能是符号链接或重解析点。"
        )
    return True


def load_snapshot_catalog(library: Path | LibraryRegistry) -> dict[str, Any]:
    root = _validated_root(library)
    assert root is not None
    path = root / CATALOG_NAME
    try:
        before = path.lstat()
    except FileNotFoundError:
        if _unregistered_snapshot_exists(root):
            raise SnapshotError("资料库含未登记旧快照；本阶段不会自动迁移。") from None
        return empty_snapshot_catalog()
    except OSError as exc:
        raise SnapshotError("无法读取快照目录册状态。") from exc
    if status_is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise SnapshotError(
            "快照目录册必须是普通文件且不能是符号链接或重解析点。"
        )
    if before.st_nlink != 1 or before.st_size > _CATALOG_MAX_BYTES:
        raise SnapshotError("快照目录册链接数或大小无效。")

    descriptor = -1
    try:
        descriptor = open_path_file(path, os.O_RDONLY)
        raw = bytearray()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            raw.extend(block)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SnapshotError("读取期间快照目录册发生变化。")
        payload = json.loads(bytes(raw).decode("utf-8"))
    except SnapshotError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise SnapshotError("快照目录册不是有效 UTF-8 JSON。") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _validate_catalog(payload)


def snapshot_record(catalog: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    if (
        not isinstance(snapshot_id, str)
        or SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
    ):
        raise SnapshotError("snapshot_id 格式无效。")
    for record in catalog["snapshots"]:
        if record["snapshot_id"] == snapshot_id:
            return record
    raise SnapshotError("找不到已成功发布的所选快照。")


@contextmanager
def snapshot_catalog_lock(
    library: Path | LibraryRegistry, *, exclusive: bool = True
) -> Iterator[None]:
    guard = _root_guard(library)
    try:
        with guard._application_root(create=False) as root_descriptor:
            if root_descriptor is None:
                raise SnapshotError("资料库尚未安全建立。")
            with guard._root_lock(root_descriptor, exclusive=exclusive):
                try:
                    before = guard._entry_status(
                        root_descriptor,
                        CATALOG_LOCK_NAME,
                        label="快照目录册锁文件",
                    )
                except FileNotFoundError:
                    before = None
                if before is not None and (
                    status_is_reparse(before)
                    or not stat.S_ISREG(before.st_mode)
                ):
                    raise SnapshotError(
                        "快照目录册锁文件必须是普通文件且不能是重解析点。"
                    )

                flags = os.O_RDWR if exclusive else os.O_RDONLY
                if exclusive:
                    flags |= os.O_CREAT
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                descriptor = -1
                locked = False
                try:
                    descriptor = open_file(
                        root_descriptor, CATALOG_LOCK_NAME, flags, 0o600
                    )
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or (before is not None and _identity(opened) != _identity(before))
                    ):
                        raise SnapshotError("快照目录册锁文件无效。")
                    try:
                        lock_context = file_lock(descriptor, exclusive=exclusive)
                        lock_context.__enter__()
                    except BlockingIOError:
                        raise SnapshotError(
                            "该资料库已有快照写操作正在进行。"
                        ) from None
                    locked = True
                    current = guard._entry_status(
                        root_descriptor,
                        CATALOG_LOCK_NAME,
                        label="快照目录册锁文件",
                    )
                    if current is None or _identity(current) != _identity(opened):
                        raise SnapshotError("快照目录册锁文件身份已改变。")
                    yield
                finally:
                    if descriptor >= 0:
                        if locked:
                            try:
                                lock_context.__exit__(None, None, None)
                            except (OSError, RuntimeError):
                                pass
                        os.close(descriptor)
    except LibraryRegistryError as exc:
        if isinstance(exc.__cause__, BlockingIOError):
            raise SnapshotError("该资料库已有快照写操作正在进行。") from None
        raise SnapshotError("无法锁定快照目录册。") from exc
    except OSError as exc:
        raise SnapshotError("无法锁定快照目录册。") from exc


def write_snapshot_catalog(
    library: Path | LibraryRegistry, catalog: dict[str, Any]
) -> None:
    root = _validated_root(library)
    assert root is not None
    _validate_catalog(catalog)
    path = root / CATALOG_NAME
    try:
        status = path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SnapshotError("无法读取快照目录册目标状态。") from exc
    else:
        if status_is_reparse(status) or not stat.S_ISREG(status.st_mode):
            raise SnapshotError("快照目录册目标必须是普通文件。")

    payload = json.dumps(
        catalog, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"
    descriptor = -1
    temporary = ""
    committed = False
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".snapshot-catalog-", dir=root
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(Path(temporary), path)
        committed = True
        temporary = ""
        fsync_directory(root)
    except (OSError, ValueError) as exc:
        if committed:
            # The visible state changed at os.replace; reporting a failed build here
            # would falsely imply that current/last_successful stayed unchanged.
            return
        raise SnapshotError("无法安全保存快照目录册。") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


__all__ = [
    "CATALOG_LOCK_NAME",
    "CATALOG_NAME",
    "SNAPSHOT_ID_PATTERN",
    "empty_snapshot_catalog",
    "load_snapshot_catalog",
    "snapshot_catalog_exists",
    "snapshot_catalog_lock",
    "snapshot_record",
    "write_snapshot_catalog",
]
