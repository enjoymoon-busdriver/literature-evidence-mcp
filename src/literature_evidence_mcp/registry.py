from __future__ import annotations

import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import fcntl

from .errors import LibraryRegistryError


REGISTRY_FORMAT = "literature-evidence-library-registry"
REGISTRY_VERSION = 1
REGISTRY_NAME = "registry.json"
LIBRARIES_DIRECTORY_NAME = "libraries"
LOCK_NAME = ".registry.lock"

_LIBRARY_ID = re.compile(r"\Alib_[0-9a-f]{32}\Z")
_NAME_MAX_CHARS = 120
_DESCRIPTION_MAX_CHARS = 1000
_REGISTRY_MAX_BYTES = 1024 * 1024


def default_application_root(*, home: Path | None = None) -> Path:
    """Return the fixed per-user application root without creating it."""
    base = Path.home() if home is None else Path(home)
    try:
        return (
            base.expanduser().resolve()
            / "Library"
            / "Application Support"
            / "literature-evidence-mcp"
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise LibraryRegistryError("无法确定受控应用目录。") from exc


def _validated_text(
    value: str, *, label: str, maximum: int, allow_empty: bool
) -> str:
    if type(value) is not str:
        raise LibraryRegistryError(f"{label}必须是文本。")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise LibraryRegistryError(f"{label}不能为空。")
    if len(normalized) > maximum:
        raise LibraryRegistryError(f"{label}不能超过 {maximum} 个字符。")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LibraryRegistryError(f"{label}不是有效的 UTF-8 文本。") from exc
    if "\x00" in normalized:
        raise LibraryRegistryError(f"{label}不能包含空字符。")
    return normalized


def _validated_library_id(library_id: str) -> str:
    if type(library_id) is not str or _LIBRARY_ID.fullmatch(library_id) is None:
        raise LibraryRegistryError("library_id 格式无效。")
    return library_id


def _lstat(path: Path, *, label: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LibraryRegistryError(f"无法读取{label}状态。") from exc


def _absolute_without_symlink_components(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise LibraryRegistryError("无法固定受控应用根目录。") from exc
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    if parts:
        top_level = current / parts[0]
        top_status = _lstat(top_level, label="受控应用路径")
        if top_status is not None and stat.S_ISLNK(top_status.st_mode):
            try:
                current = top_level.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise LibraryRegistryError("无法解析系统顶级路径别名。") from exc
            if not current.is_dir():
                raise LibraryRegistryError("系统顶级路径别名没有指向目录。")
            parts = parts[1:]
    for index, part in enumerate(parts):
        current /= part
        status = _lstat(current, label="受控应用路径")
        if status is not None and stat.S_ISLNK(status.st_mode):
            raise LibraryRegistryError("受控应用路径不能经过符号链接。")
        if index < len(parts) - 1 and status is not None and not stat.S_ISDIR(
            status.st_mode
        ):
            raise LibraryRegistryError("受控应用路径的父级不是目录。")
    return current


def _require_directory(path: Path, *, label: str, allow_missing: bool) -> bool:
    status = _lstat(path, label=label)
    if status is None:
        if allow_missing:
            return False
        raise LibraryRegistryError(f"{label}不存在。")
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise LibraryRegistryError(f"{label}必须是普通目录且不能是符号链接。")
    return True


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise LibraryRegistryError("无法同步受控应用目录。") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise LibraryRegistryError("无法同步受控应用目录。") from exc
    finally:
        os.close(descriptor)


class LibraryRegistry:
    """Persistent local registry for physically isolated snapshot libraries."""

    def __init__(self, application_root: Path | None = None) -> None:
        requested = (
            default_application_root()
            if application_root is None
            else Path(application_root).expanduser()
        )
        if requested.is_symlink():
            raise LibraryRegistryError("受控应用根目录不能是符号链接。")
        self._root = _absolute_without_symlink_components(requested)
        self._libraries_root = self._root / LIBRARIES_DIRECTORY_NAME
        self._registry_path = self._root / REGISTRY_NAME
        self._lock_path = self._root / LOCK_NAME

    @property
    def application_root(self) -> Path:
        return self._root

    def _empty_registry(self) -> dict[str, Any]:
        return {
            "format": REGISTRY_FORMAT,
            "version": REGISTRY_VERSION,
            "selected_library_id": None,
            "libraries": [],
        }

    def _safe_library_directory(self, library_id: str, *, require_exists: bool) -> Path:
        library_id = _validated_library_id(library_id)
        _require_directory(
            self._libraries_root,
            label="资料库集合目录",
            allow_missing=not require_exists,
        )
        candidate = self._libraries_root / library_id
        _require_directory(
            candidate,
            label="资料库目录",
            allow_missing=not require_exists,
        )
        return candidate

    def _read_registry_json(self) -> dict[str, Any]:
        status = _lstat(self._registry_path, label="资料库注册表")
        if status is None:
            return self._empty_registry()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise LibraryRegistryError("资料库注册表必须是普通文件且不能是符号链接。")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(self._registry_path, flags)
            opened_status = os.fstat(descriptor)
            if not stat.S_ISREG(opened_status.st_mode) or (
                opened_status.st_dev,
                opened_status.st_ino,
            ) != (status.st_dev, status.st_ino):
                raise LibraryRegistryError(
                    "资料库注册表在安全检查与读取之间发生变化。"
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                payload = handle.read(_REGISTRY_MAX_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise LibraryRegistryError("无法安全读取资料库注册表。") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(payload) > _REGISTRY_MAX_BYTES:
            raise LibraryRegistryError("资料库注册表过大。")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LibraryRegistryError("资料库注册表不是有效的 UTF-8 JSON。") from exc
        if not isinstance(value, dict):
            raise LibraryRegistryError("资料库注册表根必须是对象。")
        return value

    def _load(self) -> dict[str, Any]:
        if not _require_directory(
            self._root, label="受控应用根目录", allow_missing=True
        ):
            return self._empty_registry()
        libraries_root_exists = _require_directory(
            self._libraries_root,
            label="资料库集合目录",
            allow_missing=True,
        )
        value = self._read_registry_json()
        if set(value) != {
            "format",
            "version",
            "selected_library_id",
            "libraries",
        }:
            raise LibraryRegistryError("资料库注册表字段不符合当前格式。")
        if value["format"] != REGISTRY_FORMAT or value["version"] != REGISTRY_VERSION:
            raise LibraryRegistryError("资料库注册表格式或版本不受支持。")
        libraries = value["libraries"]
        if not isinstance(libraries, list):
            raise LibraryRegistryError("资料库注册表中的 libraries 必须是列表。")

        normalized_libraries: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for item in libraries:
            if not isinstance(item, dict) or set(item) != {
                "library_id",
                "name",
                "description",
            }:
                raise LibraryRegistryError("资料库记录字段无效。")
            library_id = _validated_library_id(item["library_id"])
            if library_id in seen_ids:
                raise LibraryRegistryError("资料库注册表含重复 library_id。")
            seen_ids.add(library_id)
            normalized_libraries.append(
                {
                    "library_id": library_id,
                    "name": _validated_text(
                        item["name"],
                        label="资料库名称",
                        maximum=_NAME_MAX_CHARS,
                        allow_empty=False,
                    ),
                    "description": _validated_text(
                        item["description"],
                        label="资料库描述",
                        maximum=_DESCRIPTION_MAX_CHARS,
                        allow_empty=True,
                    ),
                }
            )
        selected = value["selected_library_id"]
        if selected is not None:
            selected = _validated_library_id(selected)
            if selected not in seen_ids:
                raise LibraryRegistryError("所选 library_id 不属于注册表。")
        if normalized_libraries and not libraries_root_exists:
            raise LibraryRegistryError("资料库集合目录不存在。")
        for item in normalized_libraries:
            self._safe_library_directory(item["library_id"], require_exists=True)
        return {
            "format": REGISTRY_FORMAT,
            "version": REGISTRY_VERSION,
            "selected_library_id": selected,
            "libraries": normalized_libraries,
        }

    def _ensure_layout(self) -> None:
        if not _require_directory(
            self._root, label="受控应用根目录", allow_missing=True
        ):
            try:
                self._root.mkdir(parents=True, mode=0o700)
            except OSError as exc:
                raise LibraryRegistryError("无法创建受控应用根目录。") from exc
            _require_directory(
                self._root, label="受控应用根目录", allow_missing=False
            )
        if not _require_directory(
            self._libraries_root,
            label="资料库集合目录",
            allow_missing=True,
        ):
            try:
                self._libraries_root.mkdir(mode=0o700)
            except OSError as exc:
                raise LibraryRegistryError("无法创建资料库集合目录。") from exc
            _require_directory(
                self._libraries_root,
                label="资料库集合目录",
                allow_missing=False,
            )

    @contextmanager
    def _exclusive_write_lock(self) -> Iterator[None]:
        _require_directory(
            self._root, label="受控应用根目录", allow_missing=False
        )
        _require_directory(
            self._libraries_root,
            label="资料库集合目录",
            allow_missing=False,
        )
        status = _lstat(self._lock_path, label="资料库写锁")
        if status is not None and (
            stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode)
        ):
            raise LibraryRegistryError("资料库写锁必须是普通文件且不能是符号链接。")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        locked = False
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
            opened_status = os.fstat(descriptor)
            if not stat.S_ISREG(opened_status.st_mode):
                raise LibraryRegistryError("资料库写锁不是普通文件。")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LibraryRegistryError(
                    "另一个本地资料库写操作正在进行，请稍后重新执行。"
                ) from exc
            locked = True
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise LibraryRegistryError("无法安全获取资料库写锁。") from exc
        except LibraryRegistryError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        try:
            yield
        finally:
            if descriptor is not None:
                if locked:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(descriptor)

    def _write(self, registry: dict[str, Any]) -> None:
        self._ensure_layout()
        current_status = _lstat(self._registry_path, label="资料库注册表")
        if current_status is not None and (
            stat.S_ISLNK(current_status.st_mode)
            or not stat.S_ISREG(current_status.st_mode)
        ):
            raise LibraryRegistryError("资料库注册表必须是普通文件且不能是符号链接。")
        try:
            payload = (
                json.dumps(
                    registry,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, UnicodeEncodeError) as exc:
            raise LibraryRegistryError("无法编码资料库注册表。") from exc
        if len(payload) > _REGISTRY_MAX_BYTES:
            raise LibraryRegistryError("资料库注册表超过大小上限。")
        temporary = self._root / f".{REGISTRY_NAME}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._registry_path)
            _fsync_directory(self._root)
        except (OSError, ValueError) as exc:
            raise LibraryRegistryError("无法安全保存资料库注册表。") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _record(self, registry: dict[str, Any], library_id: str) -> dict[str, str]:
        library_id = _validated_library_id(library_id)
        for item in registry["libraries"]:
            if item["library_id"] == library_id:
                return item
        raise LibraryRegistryError("找不到指定的 library_id。")

    def _public_record(
        self, item: dict[str, str], *, selected_library_id: str | None
    ) -> dict[str, Any]:
        library_id = item["library_id"]
        path = self._safe_library_directory(library_id, require_exists=True)
        return {
            "library_id": library_id,
            "name": item["name"],
            "description": item["description"],
            "selected": library_id == selected_library_id,
            "library_root": str(path),
        }

    def list_libraries(self) -> list[dict[str, Any]]:
        """List registered libraries without creating or changing local state."""
        registry = self._load()
        return [
            self._public_record(
                item, selected_library_id=registry["selected_library_id"]
            )
            for item in registry["libraries"]
        ]

    def create(self, name: str, *, description: str = "") -> dict[str, Any]:
        """Explicitly create one new physical library and persist its stable ID."""
        name = _validated_text(
            name,
            label="资料库名称",
            maximum=_NAME_MAX_CHARS,
            allow_empty=False,
        )
        description = _validated_text(
            description,
            label="资料库描述",
            maximum=_DESCRIPTION_MAX_CHARS,
            allow_empty=True,
        )
        self._ensure_layout()
        with self._exclusive_write_lock():
            registry = self._load()
            library_id = f"lib_{uuid.uuid4().hex}"
            if any(item["library_id"] == library_id for item in registry["libraries"]):
                raise LibraryRegistryError("新 library_id 与现有记录冲突。")
            directory = self._libraries_root / library_id
            try:
                directory.mkdir(mode=0o700)
                _fsync_directory(self._libraries_root)
            except FileExistsError as exc:
                raise LibraryRegistryError("新 library_id 对应目录已存在。") from exc
            except (LibraryRegistryError, OSError) as exc:
                try:
                    directory.rmdir()
                except OSError:
                    pass
                if isinstance(exc, LibraryRegistryError):
                    raise
                raise LibraryRegistryError("无法创建新的资料库目录。") from exc
            item = {
                "library_id": library_id,
                "name": name,
                "description": description,
            }
            registry["libraries"].append(item)
            if registry["selected_library_id"] is None:
                registry["selected_library_id"] = library_id
            try:
                self._write(registry)
            except Exception:
                remove_unpublished = False
                try:
                    persisted = self._load()
                except LibraryRegistryError:
                    pass
                else:
                    remove_unpublished = not any(
                        record["library_id"] == library_id
                        for record in persisted["libraries"]
                    )
                if remove_unpublished:
                    try:
                        directory.rmdir()
                        _fsync_directory(self._libraries_root)
                    except (LibraryRegistryError, OSError):
                        pass
                raise
            return self._public_record(
                item, selected_library_id=registry["selected_library_id"]
            )

    def select(self, library_id: str) -> dict[str, Any]:
        """Explicitly persist the library selected by the local user."""
        with self._exclusive_write_lock():
            registry = self._load()
            item = self._record(registry, library_id)
            registry["selected_library_id"] = item["library_id"]
            self._write(registry)
            return self._public_record(item, selected_library_id=item["library_id"])

    def rename(self, library_id: str, name: str) -> dict[str, Any]:
        """Change display metadata without changing ID or physical location."""
        name = _validated_text(
            name,
            label="资料库名称",
            maximum=_NAME_MAX_CHARS,
            allow_empty=False,
        )
        with self._exclusive_write_lock():
            registry = self._load()
            item = self._record(registry, library_id)
            item["name"] = name
            self._write(registry)
            return self._public_record(
                item, selected_library_id=registry["selected_library_id"]
            )

    def update_description(self, library_id: str, description: str) -> dict[str, Any]:
        """Change description metadata without changing ID or physical location."""
        description = _validated_text(
            description,
            label="资料库描述",
            maximum=_DESCRIPTION_MAX_CHARS,
            allow_empty=True,
        )
        with self._exclusive_write_lock():
            registry = self._load()
            item = self._record(registry, library_id)
            item["description"] = description
            self._write(registry)
            return self._public_record(
                item, selected_library_id=registry["selected_library_id"]
            )

    def library_path(self, library_id: str) -> Path:
        """Resolve a registered ID to its direct, non-symlink physical root."""
        registry = self._load()
        item = self._record(registry, library_id)
        return self._safe_library_directory(item["library_id"], require_exists=True)

    def selected_library_path(self) -> Path:
        """Resolve the explicit local selection without inventing a latest/default alias."""
        registry = self._load()
        selected = registry["selected_library_id"]
        if selected is None:
            raise LibraryRegistryError("尚未选择资料库。")
        return self._safe_library_directory(selected, require_exists=True)


__all__ = [
    "LIBRARIES_DIRECTORY_NAME",
    "LOCK_NAME",
    "REGISTRY_FORMAT",
    "REGISTRY_NAME",
    "REGISTRY_VERSION",
    "LibraryRegistry",
    "default_application_root",
]
