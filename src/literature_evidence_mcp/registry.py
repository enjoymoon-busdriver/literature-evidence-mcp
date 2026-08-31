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
_SYSTEM_TOP_LEVEL_ALIASES = {
    "/tmp": Path("/private/tmp"),
    "/var": Path("/private/var"),
}


def default_application_root(*, home: Path | None = None) -> Path:
    """Return the fixed per-user application root without creating it."""
    try:
        base = Path.home() if home is None else Path(home)
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
            expected = _SYSTEM_TOP_LEVEL_ALIASES.get(str(top_level))
            if expected is None:
                raise LibraryRegistryError(
                    "受控应用路径仅允许 macOS 的 /var 与 /tmp 顶级系统别名。"
                )
            try:
                resolved = top_level.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise LibraryRegistryError("无法解析系统顶级路径别名。") from exc
            if resolved != expected or not resolved.is_dir():
                raise LibraryRegistryError("系统顶级路径别名目标不符合预期。")
            current = resolved
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


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _capture_directory_identities(path: Path) -> list[tuple[int, int] | None]:
    """Capture existing components through no-follow directory descriptors."""
    parts = path.parts[1:]
    identities: list[tuple[int, int] | None] = [None] * len(parts)
    try:
        descriptor = os.open(path.anchor, _directory_open_flags())
    except OSError as exc:
        raise LibraryRegistryError("无法固定受控应用路径身份。") from exc
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(
                    part,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                break
            except OSError as exc:
                raise LibraryRegistryError(
                    "受控应用路径不能经过符号链接或非目录条目。"
                ) from exc
            try:
                status = os.fstat(child)
            except OSError as exc:
                os.close(child)
                raise LibraryRegistryError(
                    "无法固定受控应用路径身份。"
                ) from exc
            if not stat.S_ISDIR(status.st_mode):
                os.close(child)
                raise LibraryRegistryError("受控应用路径的组件不是目录。")
            identities[index] = _identity(status)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return identities


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise LibraryRegistryError("无法同步受控应用目录。") from exc


class _RegistryWriteFailure(LibraryRegistryError):
    def __init__(self, message: str, *, published: bool) -> None:
        super().__init__(message)
        self.published = published


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
        self._component_identities = _capture_directory_identities(self._root)

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

    def _open_anchored_root(self, *, create: bool) -> int | None:
        try:
            descriptor = os.open(self._root.anchor, _directory_open_flags())
        except OSError as exc:
            raise LibraryRegistryError("无法打开受控应用路径锚点。") from exc
        try:
            for index, part in enumerate(self._root.parts[1:]):
                expected = self._component_identities[index]
                try:
                    child = os.open(
                        part,
                        _directory_open_flags(),
                        dir_fd=descriptor,
                    )
                except FileNotFoundError as exc:
                    if expected is not None:
                        raise LibraryRegistryError(
                            "受控应用路径的既有组件已被移除或替换。"
                        ) from exc
                    if not create:
                        os.close(descriptor)
                        return None
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    except OSError as mkdir_exc:
                        raise LibraryRegistryError(
                            "无法在受控锚点内创建应用目录。"
                        ) from mkdir_exc
                    try:
                        child = os.open(
                            part,
                            _directory_open_flags(),
                            dir_fd=descriptor,
                        )
                    except OSError as open_exc:
                        raise LibraryRegistryError(
                            "新建受控应用路径不能经过符号链接或非目录条目。"
                        ) from open_exc
                except OSError as exc:
                    raise LibraryRegistryError(
                        "受控应用路径不能经过符号链接或非目录条目。"
                    ) from exc

                try:
                    opened_status = os.fstat(child)
                except OSError as exc:
                    os.close(child)
                    raise LibraryRegistryError(
                        "无法核对受控应用路径身份。"
                    ) from exc
                opened_identity = _identity(opened_status)
                if not stat.S_ISDIR(opened_status.st_mode):
                    os.close(child)
                    raise LibraryRegistryError("受控应用路径的组件不是目录。")
                if expected is not None and opened_identity != expected:
                    os.close(child)
                    raise LibraryRegistryError("受控应用路径的目录身份已改变。")
                if expected is None:
                    self._component_identities[index] = opened_identity
                os.close(descriptor)
                descriptor = child
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @contextmanager
    def _application_root(self, *, create: bool) -> Iterator[int | None]:
        descriptor = self._open_anchored_root(create=create)
        try:
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _entry_status(
        self, parent_descriptor: int, name: str, *, label: str
    ) -> os.stat_result | None:
        try:
            return os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LibraryRegistryError(f"无法读取{label}状态。") from exc

    def _open_directory_entry(
        self,
        parent_descriptor: int,
        name: str,
        *,
        label: str,
        allow_missing: bool,
        create: bool = False,
    ) -> int | None:
        status = self._entry_status(parent_descriptor, name, label=label)
        if status is None and create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise LibraryRegistryError(f"无法创建{label}。") from exc
            status = self._entry_status(parent_descriptor, name, label=label)
        if status is None:
            if allow_missing:
                return None
            raise LibraryRegistryError(f"{label}不存在。")
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise LibraryRegistryError(f"{label}必须是普通目录且不能是符号链接。")
        try:
            descriptor = os.open(
                name,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise LibraryRegistryError(
                f"{label}必须是普通目录且不能是符号链接。"
            ) from exc
        try:
            opened_status = os.fstat(descriptor)
        except OSError as exc:
            os.close(descriptor)
            raise LibraryRegistryError(f"无法核对{label}身份。") from exc
        if not stat.S_ISDIR(opened_status.st_mode) or _identity(
            opened_status
        ) != _identity(status):
            os.close(descriptor)
            raise LibraryRegistryError(f"{label}在安全检查与打开之间发生变化。")
        return descriptor

    def _open_libraries(
        self, root_descriptor: int, *, allow_missing: bool, create: bool = False
    ) -> int | None:
        return self._open_directory_entry(
            root_descriptor,
            LIBRARIES_DIRECTORY_NAME,
            label="资料库集合目录",
            allow_missing=allow_missing,
            create=create,
        )

    def _validate_library_directory(
        self, libraries_descriptor: int, library_id: str
    ) -> None:
        library_id = _validated_library_id(library_id)
        descriptor = self._open_directory_entry(
            libraries_descriptor,
            library_id,
            label="资料库目录",
            allow_missing=False,
        )
        assert descriptor is not None
        os.close(descriptor)

    def _safe_library_directory(
        self, root_descriptor: int, library_id: str
    ) -> Path:
        library_id = _validated_library_id(library_id)
        libraries_descriptor = self._open_libraries(
            root_descriptor,
            allow_missing=False,
        )
        assert libraries_descriptor is not None
        try:
            self._validate_library_directory(libraries_descriptor, library_id)
        finally:
            os.close(libraries_descriptor)
        return self._libraries_root / library_id

    def _managed_library_entry_exists(self, libraries_descriptor: int) -> bool:
        try:
            names = os.listdir(libraries_descriptor)
        except OSError as exc:
            raise LibraryRegistryError("无法检查资料库集合目录。") from exc
        for name in names:
            if _LIBRARY_ID.fullmatch(name) is None:
                continue
            status = self._entry_status(
                libraries_descriptor,
                name,
                label="缺失注册表对应的资料库条目",
            )
            if status is None:
                continue
            if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
                raise LibraryRegistryError(
                    "资料库注册表缺失，且集合目录含受控格式的异常条目。"
                )
            return True
        return False

    def _read_registry_json(
        self,
        root_descriptor: int,
        libraries_descriptor: int | None,
    ) -> dict[str, Any]:
        status = self._entry_status(
            root_descriptor,
            REGISTRY_NAME,
            label="资料库注册表",
        )
        if status is None:
            if libraries_descriptor is not None and self._managed_library_entry_exists(
                libraries_descriptor
            ):
                raise LibraryRegistryError(
                    "资料库注册表缺失，但发现已有受管资料库目录；已停止以避免覆盖。"
                )
            return self._empty_registry()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise LibraryRegistryError("资料库注册表必须是普通文件且不能是符号链接。")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor: int | None = None
        try:
            descriptor = os.open(REGISTRY_NAME, flags, dir_fd=root_descriptor)
            opened_status = os.fstat(descriptor)
            if not stat.S_ISREG(opened_status.st_mode) or _identity(
                opened_status
            ) != _identity(status):
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

    def _load(self, root_descriptor: int) -> dict[str, Any]:
        libraries_descriptor = self._open_libraries(
            root_descriptor,
            allow_missing=True,
        )
        try:
            value = self._read_registry_json(
                root_descriptor,
                libraries_descriptor,
            )
            if set(value) != {
                "format",
                "version",
                "selected_library_id",
                "libraries",
            }:
                raise LibraryRegistryError("资料库注册表字段不符合当前格式。")
            if (
                value["format"] != REGISTRY_FORMAT
                or value["version"] != REGISTRY_VERSION
            ):
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
            if normalized_libraries and libraries_descriptor is None:
                raise LibraryRegistryError("资料库集合目录不存在。")
            if libraries_descriptor is not None:
                for item in normalized_libraries:
                    self._validate_library_directory(
                        libraries_descriptor,
                        item["library_id"],
                    )
            return {
                "format": REGISTRY_FORMAT,
                "version": REGISTRY_VERSION,
                "selected_library_id": selected,
                "libraries": normalized_libraries,
            }
        finally:
            if libraries_descriptor is not None:
                os.close(libraries_descriptor)

    @contextmanager
    def _root_lock(
        self, root_descriptor: int, *, exclusive: bool
    ) -> Iterator[None]:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        locked = False
        try:
            try:
                fcntl.flock(root_descriptor, operation | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LibraryRegistryError(
                    "另一个本地资料库写操作正在进行，请稍后重新执行。"
                ) from exc
            except OSError as exc:
                raise LibraryRegistryError("无法安全获取资料库目录锁。") from exc
            locked = True
            yield
        finally:
            if locked:
                try:
                    fcntl.flock(root_descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass

    @contextmanager
    def _visible_write_lock(self, root_descriptor: int) -> Iterator[None]:
        status = self._entry_status(
            root_descriptor,
            LOCK_NAME,
            label="资料库写锁",
        )
        if status is not None and (
            stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode)
        ):
            raise LibraryRegistryError("资料库写锁必须是普通文件且不能是符号链接。")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor: int | None = None
        locked = False
        try:
            descriptor = os.open(LOCK_NAME, flags, 0o600, dir_fd=root_descriptor)
            opened_status = os.fstat(descriptor)
            if not stat.S_ISREG(opened_status.st_mode):
                raise LibraryRegistryError("资料库写锁不是普通文件。")
            if status is not None and _identity(opened_status) != _identity(status):
                raise LibraryRegistryError("资料库写锁的文件身份已改变。")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LibraryRegistryError(
                    "另一个本地资料库写操作正在进行，请稍后重新执行。"
                ) from exc
            locked = True
            current_status = self._entry_status(
                root_descriptor,
                LOCK_NAME,
                label="资料库写锁",
            )
            if current_status is None or _identity(current_status) != _identity(
                opened_status
            ):
                raise LibraryRegistryError("资料库写锁的固定文件身份已改变。")
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise LibraryRegistryError("无法安全获取资料库写锁。") from exc
        except LibraryRegistryError:
            if descriptor is not None:
                if locked:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
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

    @contextmanager
    def _exclusive_write_lock(
        self, root_descriptor: int | None = None
    ) -> Iterator[None]:
        if root_descriptor is None:
            with self._application_root(create=False) as opened_root:
                if opened_root is None:
                    raise LibraryRegistryError("受控应用根目录不存在。")
                with self._exclusive_write_lock(opened_root):
                    yield
            return
        with self._root_lock(root_descriptor, exclusive=True):
            with self._visible_write_lock(root_descriptor):
                yield

    def _write(self, root_descriptor: int, registry: dict[str, Any]) -> None:
        current_status = self._entry_status(
            root_descriptor,
            REGISTRY_NAME,
            label="资料库注册表",
        )
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
        temporary = f".{REGISTRY_NAME}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor: int | None = None
        published = False
        try:
            descriptor = os.open(
                temporary,
                flags,
                0o600,
                dir_fd=root_descriptor,
            )
            handle = os.fdopen(descriptor, "wb")
            descriptor = None
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(
                temporary,
                REGISTRY_NAME,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
            published = True
            _fsync_directory(root_descriptor)
        except LibraryRegistryError as exc:
            raise _RegistryWriteFailure(
                str(exc),
                published=published,
            ) from exc
        except (OSError, ValueError) as exc:
            raise _RegistryWriteFailure(
                "无法安全保存资料库注册表。",
                published=published,
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=root_descriptor)
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
        self,
        root_descriptor: int,
        item: dict[str, str],
        *,
        selected_library_id: str | None,
    ) -> dict[str, Any]:
        library_id = item["library_id"]
        path = self._safe_library_directory(root_descriptor, library_id)
        return {
            "library_id": library_id,
            "name": item["name"],
            "description": item["description"],
            "selected": library_id == selected_library_id,
            "library_root": str(path),
        }

    def list_libraries(self) -> list[dict[str, Any]]:
        """List registered libraries without creating or changing local state."""
        with self._application_root(create=False) as root_descriptor:
            if root_descriptor is None:
                return []
            with self._root_lock(root_descriptor, exclusive=False):
                registry = self._load(root_descriptor)
                return [
                    self._public_record(
                        root_descriptor,
                        item,
                        selected_library_id=registry["selected_library_id"],
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
        with self._application_root(create=True) as root_descriptor:
            assert root_descriptor is not None
            with self._root_lock(root_descriptor, exclusive=True):
                registry = self._load(root_descriptor)
                with self._visible_write_lock(root_descriptor):
                    libraries_descriptor = self._open_libraries(
                        root_descriptor,
                        allow_missing=False,
                        create=True,
                    )
                    assert libraries_descriptor is not None
                    try:
                        library_id = f"lib_{uuid.uuid4().hex}"
                        if any(
                            item["library_id"] == library_id
                            for item in registry["libraries"]
                        ):
                            raise LibraryRegistryError(
                                "新 library_id 与现有记录冲突。"
                            )
                        try:
                            os.mkdir(
                                library_id,
                                mode=0o700,
                                dir_fd=libraries_descriptor,
                            )
                            _fsync_directory(libraries_descriptor)
                        except FileExistsError as exc:
                            raise LibraryRegistryError(
                                "新 library_id 对应目录已存在。"
                            ) from exc
                        except (LibraryRegistryError, OSError) as exc:
                            try:
                                os.rmdir(
                                    library_id,
                                    dir_fd=libraries_descriptor,
                                )
                            except OSError:
                                pass
                            if isinstance(exc, LibraryRegistryError):
                                raise
                            raise LibraryRegistryError(
                                "无法创建新的资料库目录。"
                            ) from exc
                        item = {
                            "library_id": library_id,
                            "name": name,
                            "description": description,
                        }
                        registry["libraries"].append(item)
                        if registry["selected_library_id"] is None:
                            registry["selected_library_id"] = library_id
                        try:
                            self._write(root_descriptor, registry)
                        except _RegistryWriteFailure as exc:
                            if not exc.published:
                                try:
                                    os.rmdir(
                                        library_id,
                                        dir_fd=libraries_descriptor,
                                    )
                                    _fsync_directory(libraries_descriptor)
                                except (LibraryRegistryError, OSError):
                                    pass
                            raise
                        except Exception:
                            try:
                                os.rmdir(
                                    library_id,
                                    dir_fd=libraries_descriptor,
                                )
                                _fsync_directory(libraries_descriptor)
                            except (LibraryRegistryError, OSError):
                                pass
                            raise
                        return self._public_record(
                            root_descriptor,
                            item,
                            selected_library_id=registry["selected_library_id"],
                        )
                    finally:
                        os.close(libraries_descriptor)

    def select(self, library_id: str) -> dict[str, Any]:
        """Explicitly persist the library selected by the local user."""
        with self._application_root(create=False) as root_descriptor:
            if root_descriptor is None:
                raise LibraryRegistryError("受控应用根目录不存在。")
            with self._root_lock(root_descriptor, exclusive=True):
                registry = self._load(root_descriptor)
                item = self._record(registry, library_id)
                with self._visible_write_lock(root_descriptor):
                    registry["selected_library_id"] = item["library_id"]
                    self._write(root_descriptor, registry)
                return self._public_record(
                    root_descriptor,
                    item,
                    selected_library_id=item["library_id"],
                )

    def rename(self, library_id: str, name: str) -> dict[str, Any]:
        """Change display metadata without changing ID or physical location."""
        name = _validated_text(
            name,
            label="资料库名称",
            maximum=_NAME_MAX_CHARS,
            allow_empty=False,
        )
        with self._application_root(create=False) as root_descriptor:
            if root_descriptor is None:
                raise LibraryRegistryError("受控应用根目录不存在。")
            with self._root_lock(root_descriptor, exclusive=True):
                registry = self._load(root_descriptor)
                item = self._record(registry, library_id)
                with self._visible_write_lock(root_descriptor):
                    item["name"] = name
                    self._write(root_descriptor, registry)
                return self._public_record(
                    root_descriptor,
                    item,
                    selected_library_id=registry["selected_library_id"],
                )

    def update_description(self, library_id: str, description: str) -> dict[str, Any]:
        """Change description metadata without changing ID or physical location."""
        description = _validated_text(
            description,
            label="资料库描述",
            maximum=_DESCRIPTION_MAX_CHARS,
            allow_empty=True,
        )
        with self._application_root(create=False) as root_descriptor:
            if root_descriptor is None:
                raise LibraryRegistryError("受控应用根目录不存在。")
            with self._root_lock(root_descriptor, exclusive=True):
                registry = self._load(root_descriptor)
                item = self._record(registry, library_id)
                with self._visible_write_lock(root_descriptor):
                    item["description"] = description
                    self._write(root_descriptor, registry)
                return self._public_record(
                    root_descriptor,
                    item,
                    selected_library_id=registry["selected_library_id"],
                )

    def library_path(self, library_id: str) -> Path:
        """Resolve a registered ID to its direct, non-symlink physical root."""
        with self._application_root(create=False) as root_descriptor:
            if root_descriptor is None:
                self._record(self._empty_registry(), library_id)
                raise AssertionError("unreachable")
            with self._root_lock(root_descriptor, exclusive=False):
                registry = self._load(root_descriptor)
                item = self._record(registry, library_id)
                return self._safe_library_directory(
                    root_descriptor,
                    item["library_id"],
                )

    def selected_library_path(self) -> Path:
        """Resolve the explicit local selection without inventing a latest/default alias."""
        with self._application_root(create=False) as root_descriptor:
            if root_descriptor is None:
                raise LibraryRegistryError("尚未选择资料库。")
            with self._root_lock(root_descriptor, exclusive=False):
                registry = self._load(root_descriptor)
                selected = registry["selected_library_id"]
                if selected is None:
                    raise LibraryRegistryError("尚未选择资料库。")
                return self._safe_library_directory(root_descriptor, selected)


__all__ = [
    "LIBRARIES_DIRECTORY_NAME",
    "LOCK_NAME",
    "REGISTRY_FORMAT",
    "REGISTRY_NAME",
    "REGISTRY_VERSION",
    "LibraryRegistry",
    "default_application_root",
]
