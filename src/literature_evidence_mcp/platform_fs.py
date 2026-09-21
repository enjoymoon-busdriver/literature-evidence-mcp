from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


IS_WINDOWS = os.name == "nt"
Identity = tuple[int, int]


class PathIdentityError(OSError):
    """An anchored path component no longer has its captured identity."""


class UnsafePathError(OSError):
    """A managed path contains a link, reparse point, or wrong entry type."""


if IS_WINDOWS:  # pragma: no cover - imported and exercised on Windows CI.
    import msvcrt
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _MOVEFILE_REPLACE_EXISTING = 0x00000001
    _MOVEFILE_WRITE_THROUGH = 0x00000008
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_SHARING_VIOLATION = 32
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_IO_PENDING = 997

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
    _GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
    _GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _GetFileInformationByHandle.restype = wintypes.BOOL
    _MoveFileExW = _kernel32.MoveFileExW
    _MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _MoveFileExW.restype = wintypes.BOOL
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _UnlockFileEx.restype = wintypes.BOOL
    _SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
    _SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _SetFileInformationByHandle.restype = wintypes.BOOL
else:
    import fcntl


def absolute_path(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise UnsafePathError("invalid absolute path") from exc
    if not absolute.is_absolute() or not absolute.anchor:
        raise UnsafePathError("managed path must be absolute")
    if IS_WINDOWS and (
        absolute.drive.startswith("\\\\")
        or str(absolute).startswith(("\\\\?\\", "\\\\.\\"))
    ):
        raise UnsafePathError("UNC and device paths are not supported")
    return absolute


def status_is_reparse(status: os.stat_result) -> bool:
    if stat.S_ISLNK(status.st_mode):
        return True
    attributes = getattr(status, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)
    return bool(attributes & reparse_flag)


def identity(status: os.stat_result) -> Identity:
    return int(status.st_dev), int(status.st_ino)


def _validate_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or os.path.basename(name) != name
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ValueError("managed entry name must be one path component")
    return name


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


class DirectoryHandle:
    """A directory pinned for the duration of one managed filesystem action."""

    def __init__(
        self,
        path: Path,
        *,
        descriptor: int | None = None,
        windows_handles: list[int] | None = None,
    ) -> None:
        self.path = path
        self.descriptor = descriptor
        self._windows_handles = windows_handles or []

    @property
    def native_handle(self) -> int | None:
        return self._windows_handles[-1] if self._windows_handles else None

    def close(self) -> None:
        if self.descriptor is not None:
            descriptor, self.descriptor = self.descriptor, None
            os.close(descriptor)
        if self._windows_handles:
            handles, self._windows_handles = self._windows_handles, []
            for handle in reversed(handles):
                _win_close(handle)

    def __enter__(self) -> DirectoryHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _win_error(error: int | None = None) -> OSError:
    assert IS_WINDOWS
    return ctypes.WinError(ctypes.get_last_error() if error is None else error)


def _win_close(handle: int) -> None:
    if IS_WINDOWS and handle not in {0, _INVALID_HANDLE_VALUE}:
        _CloseHandle(handle)


def _win_information(handle: int) -> object:
    assert IS_WINDOWS
    information = _BY_HANDLE_FILE_INFORMATION()
    if not _GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise _win_error()
    return information


def _win_identity(handle: int) -> Identity:
    information = _win_information(handle)
    file_index = (information.nFileIndexHigh << 32) | information.nFileIndexLow
    return int(information.dwVolumeSerialNumber), int(file_index)


def _win_open_directory(path: Path, *, delete_access: bool = False) -> int:
    assert IS_WINDOWS
    desired = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    if delete_access:
        desired |= _DELETE
    handle = _CreateFileW(
        str(path),
        desired,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = int(handle) if handle else 0
    if value == _INVALID_HANDLE_VALUE:
        raise _win_error()
    try:
        attributes = _win_information(value).dwFileAttributes
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafePathError("managed directory is a reparse point")
        if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise UnsafePathError("managed directory entry is not a directory")
        return value
    except BaseException:
        _win_close(value)
        raise


def _win_open_file(path: Path, flags: int, mode: int) -> int:
    assert IS_WINDOWS
    access_mode = flags & (os.O_WRONLY | os.O_RDWR)
    if access_mode == os.O_WRONLY:
        desired = _GENERIC_WRITE
    elif access_mode == os.O_RDWR:
        desired = _GENERIC_READ | _GENERIC_WRITE
    else:
        desired = _GENERIC_READ
    if flags & os.O_CREAT and flags & os.O_EXCL:
        disposition = _CREATE_NEW
    elif flags & os.O_CREAT:
        disposition = _OPEN_ALWAYS
    else:
        disposition = _OPEN_EXISTING
    handle = _CreateFileW(
        str(path),
        desired,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        disposition,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = int(handle) if handle else 0
    if value == _INVALID_HANDLE_VALUE:
        raise _win_error()
    try:
        attributes = _win_information(value).dwFileAttributes
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafePathError("managed file is a reparse point")
        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise UnsafePathError("managed file entry is a directory")
        crt_flags = access_mode | getattr(os, "O_BINARY", 0)
        if flags & getattr(os, "O_APPEND", 0):
            crt_flags |= os.O_APPEND
        descriptor = msvcrt.open_osfhandle(value, crt_flags)
        value = 0  # open_osfhandle transferred ownership to the CRT descriptor.
        if flags & getattr(os, "O_TRUNC", 0):
            os.ftruncate(descriptor, 0)
        return descriptor
    except BaseException:
        _win_close(value)
        raise


def _win_open_delete_file(path: Path) -> int:
    assert IS_WINDOWS
    handle = _CreateFileW(
        str(path),
        _DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = int(handle) if handle else 0
    if value == _INVALID_HANDLE_VALUE:
        raise _win_error()
    try:
        attributes = _win_information(value).dwFileAttributes
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafePathError("managed file is a reparse point")
        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise UnsafePathError("managed file entry is a directory")
        return value
    except BaseException:
        _win_close(value)
        raise


def capture_directory_identities(path: Path) -> list[Identity | None]:
    path = absolute_path(path)
    parts = path.parts[1:]
    identities: list[Identity | None] = [None] * len(parts)
    if not IS_WINDOWS:
        try:
            descriptor = os.open(path.anchor, _directory_open_flags())
        except OSError as exc:
            raise UnsafePathError("cannot open managed path anchor") from exc
        try:
            for index, part in enumerate(parts):
                try:
                    child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
                except FileNotFoundError:
                    break
                except OSError as exc:
                    raise UnsafePathError("managed path contains a link or non-directory") from exc
                try:
                    status = os.fstat(child)
                    if not stat.S_ISDIR(status.st_mode):
                        raise UnsafePathError("managed path component is not a directory")
                    identities[index] = identity(status)
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
        finally:
            os.close(descriptor)
        return identities

    handles: list[int] = []
    current = Path(path.anchor)
    try:
        handles.append(_win_open_directory(current))
        for index, part in enumerate(parts):
            current /= part
            try:
                child = _win_open_directory(current)
            except OSError as exc:
                if getattr(exc, "winerror", None) in {
                    _ERROR_FILE_NOT_FOUND,
                    _ERROR_PATH_NOT_FOUND,
                }:
                    break
                raise
            handles.append(child)
            identities[index] = _win_identity(child)
    finally:
        for handle in reversed(handles):
            _win_close(handle)
    return identities


def open_anchored_directory(
    path: Path,
    expected_identities: list[Identity | None],
    *,
    create: bool,
) -> DirectoryHandle | None:
    path = absolute_path(path)
    parts = path.parts[1:]
    if len(parts) != len(expected_identities):
        raise PathIdentityError("managed path identity shape changed")
    if not IS_WINDOWS:
        descriptor = os.open(path.anchor, _directory_open_flags())
        try:
            for index, part in enumerate(parts):
                expected = expected_identities[index]
                try:
                    child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
                except FileNotFoundError as exc:
                    if expected is not None:
                        raise PathIdentityError("managed path component was removed") from exc
                    if not create:
                        os.close(descriptor)
                        return None
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
                opened = os.fstat(child)
                opened_identity = identity(opened)
                if not stat.S_ISDIR(opened.st_mode):
                    os.close(child)
                    raise UnsafePathError("managed path component is not a directory")
                if expected is not None and opened_identity != expected:
                    os.close(child)
                    raise PathIdentityError("managed path component identity changed")
                if expected is None:
                    expected_identities[index] = opened_identity
                os.close(descriptor)
                descriptor = child
            return DirectoryHandle(path, descriptor=descriptor)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    handles: list[int] = []
    current = Path(path.anchor)
    try:
        handles.append(_win_open_directory(current))
        for index, part in enumerate(parts):
            current /= part
            expected = expected_identities[index]
            try:
                child = _win_open_directory(current)
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {
                    _ERROR_FILE_NOT_FOUND,
                    _ERROR_PATH_NOT_FOUND,
                }:
                    raise
                if expected is not None:
                    raise PathIdentityError("managed path component was removed") from exc
                if not create:
                    for handle in reversed(handles):
                        _win_close(handle)
                    return None
                try:
                    os.mkdir(current, mode=0o700)
                except FileExistsError:
                    pass
                child = _win_open_directory(current)
            opened_identity = _win_identity(child)
            if expected is not None and opened_identity != expected:
                _win_close(child)
                raise PathIdentityError("managed path component identity changed")
            if expected is None:
                expected_identities[index] = opened_identity
            handles.append(child)
        return DirectoryHandle(path, windows_handles=handles)
    except BaseException:
        for handle in reversed(handles):
            _win_close(handle)
        raise


def entry_status(parent: DirectoryHandle, name: str) -> os.stat_result | None:
    name = _validate_name(name)
    try:
        if IS_WINDOWS:
            return (parent.path / name).lstat()
        assert parent.descriptor is not None
        return os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def open_child_directory(
    parent: DirectoryHandle,
    name: str,
    *,
    create: bool = False,
    allow_missing: bool = False,
    delete_access: bool = False,
) -> DirectoryHandle | None:
    name = _validate_name(name)
    status = entry_status(parent, name)
    if status is None and create:
        mkdir_entry(parent, name)
        status = entry_status(parent, name)
    if status is None:
        if allow_missing:
            return None
        raise FileNotFoundError(name)
    if status_is_reparse(status) or not stat.S_ISDIR(status.st_mode):
        raise UnsafePathError("managed directory entry is a link or non-directory")
    if IS_WINDOWS:
        return DirectoryHandle(
            parent.path / name,
            windows_handles=[
                _win_open_directory(parent.path / name, delete_access=delete_access)
            ],
        )
    assert parent.descriptor is not None
    descriptor = os.open(name, _directory_open_flags(), dir_fd=parent.descriptor)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or identity(opened) != identity(status):
        os.close(descriptor)
        raise PathIdentityError("managed directory changed while opening")
    return DirectoryHandle(parent.path / name, descriptor=descriptor)


def list_directory(directory: DirectoryHandle) -> list[str]:
    if IS_WINDOWS:
        return os.listdir(directory.path)
    assert directory.descriptor is not None
    return os.listdir(directory.descriptor)


def directory_identity(directory: DirectoryHandle) -> Identity:
    if IS_WINDOWS:
        if directory.native_handle is None:
            raise OSError(errno.EBADF, "closed directory handle")
        return _win_identity(directory.native_handle)
    if directory.descriptor is None:
        raise OSError(errno.EBADF, "closed directory handle")
    return identity(os.fstat(directory.descriptor))


def mkdir_entry(parent: DirectoryHandle, name: str, *, mode: int = 0o700) -> None:
    name = _validate_name(name)
    if IS_WINDOWS:
        os.mkdir(parent.path / name, mode=mode)
        return
    assert parent.descriptor is not None
    os.mkdir(name, mode=mode, dir_fd=parent.descriptor)


def rmdir_entry(parent: DirectoryHandle, name: str) -> None:
    name = _validate_name(name)
    if IS_WINDOWS:
        os.rmdir(parent.path / name)
        return
    assert parent.descriptor is not None
    os.rmdir(name, dir_fd=parent.descriptor)


def unlink_entry(parent: DirectoryHandle, name: str) -> None:
    name = _validate_name(name)
    if IS_WINDOWS:
        os.unlink(parent.path / name)
        return
    assert parent.descriptor is not None
    os.unlink(name, dir_fd=parent.descriptor)


def open_file(
    parent: DirectoryHandle,
    name: str,
    flags: int,
    mode: int = 0o600,
) -> int:
    name = _validate_name(name)
    if IS_WINDOWS:
        return _win_open_file(parent.path / name, flags, mode)
    assert parent.descriptor is not None
    safe_flags = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, safe_flags, mode, dir_fd=parent.descriptor)


def open_path_file(path: Path, flags: int = os.O_RDONLY, mode: int = 0o600) -> int:
    path = Path(path)
    if IS_WINDOWS:
        return _win_open_file(path, flags, mode)
    safe_flags = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, safe_flags, mode)


def replace_entry(parent: DirectoryHandle, source_name: str, target_name: str) -> None:
    source_name = _validate_name(source_name)
    target_name = _validate_name(target_name)
    if IS_WINDOWS:
        _win_move(
            parent.path / source_name,
            parent.path / target_name,
            replace=True,
        )
        return
    assert parent.descriptor is not None
    os.replace(
        source_name,
        target_name,
        src_dir_fd=parent.descriptor,
        dst_dir_fd=parent.descriptor,
    )


def replace_file(source: Path, target: Path) -> None:
    if IS_WINDOWS:
        _win_move(source, target, replace=True)
    else:
        os.replace(source, target)


def _win_move(source: Path, target: Path, *, replace: bool) -> None:
    assert IS_WINDOWS
    flags = _MOVEFILE_WRITE_THROUGH
    if replace:
        flags |= _MOVEFILE_REPLACE_EXISTING
    if not _MoveFileExW(str(source), str(target), flags):
        error = ctypes.get_last_error()
        if not replace and error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
            raise _win_error(error)
        if not replace and error in {80, 183}:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
        raise _win_error(error)


def fsync_directory(directory: DirectoryHandle | Path) -> None:
    if IS_WINDOWS:
        # Windows cannot portably fsync a directory through a CRT descriptor.
        # File payloads are flushed before publication and MoveFileEx uses
        # MOVEFILE_WRITE_THROUGH for the metadata publication step.
        return
    if isinstance(directory, DirectoryHandle):
        if directory.descriptor is None:
            raise OSError(errno.EBADF, "closed directory handle")
        os.fsync(directory.descriptor)
        return
    descriptor = os.open(directory, _directory_open_flags())
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def file_lock(descriptor: int, *, exclusive: bool) -> Iterator[None]:
    if not IS_WINDOWS:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            raise
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        return

    handle = msvcrt.get_osfhandle(descriptor)
    overlapped = _OVERLAPPED()
    flags = _LOCKFILE_FAIL_IMMEDIATELY
    if exclusive:
        flags |= _LOCKFILE_EXCLUSIVE_LOCK
    if not _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        if error in {
            _ERROR_SHARING_VIOLATION,
            _ERROR_LOCK_VIOLATION,
            _ERROR_IO_PENDING,
        }:
            raise BlockingIOError(errno.EWOULDBLOCK, "file lock is busy")
        raise _win_error(error)
    try:
        yield
    finally:
        unlock = _OVERLAPPED()
        _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(unlock))


def publish_directory_no_replace(source: Path, target: Path) -> None:
    if sys.platform == "darwin":
        renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        if renamex_np(os.fsencode(source), os.fsencode(target), 0x00000004) == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(error_number, os.strerror(error_number), target)
        raise OSError(error_number, os.strerror(error_number), target)
    if IS_WINDOWS:
        _win_move(source, target, replace=False)
        return
    if target.exists():
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), target)
    os.rename(source, target)


def _win_mark_delete(handle: int) -> None:
    assert IS_WINDOWS
    disposition = _FILE_DISPOSITION_INFO(True)
    if not _SetFileInformationByHandle(
        handle,
        4,  # FileDispositionInfo
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise _win_error()


def _win_delete_directory_contents(directory: DirectoryHandle) -> None:
    assert IS_WINDOWS and directory.native_handle is not None
    try:
        names = os.listdir(directory.path)
    except OSError:
        raise
    for name in names:
        _validate_name(name)
        path = directory.path / name
        status = path.lstat()
        if status_is_reparse(status):
            raise UnsafePathError("managed tree contains a reparse point")
        if stat.S_ISDIR(status.st_mode):
            child_handle = _win_open_directory(path, delete_access=True)
            child = DirectoryHandle(path, windows_handles=[child_handle])
            try:
                _win_delete_directory_contents(child)
                _win_mark_delete(child_handle)
            finally:
                child.close()
        elif stat.S_ISREG(status.st_mode):
            handle = _win_open_delete_file(path)
            try:
                _win_mark_delete(handle)
            finally:
                _win_close(handle)
        else:
            raise UnsafePathError("managed tree contains a non-regular entry")


def remove_directory_tree(
    directory: DirectoryHandle, *, parent: DirectoryHandle | None = None
) -> None:
    if IS_WINDOWS:
        if directory.native_handle is None:
            raise OSError(errno.EBADF, "closed directory handle")
        _win_delete_directory_contents(directory)
        _win_mark_delete(directory.native_handle)
        directory.close()
        return
    import shutil

    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError(errno.ENOTSUP, "safe descriptor-relative removal is unavailable")
    if parent is not None:
        if parent.descriptor is None:
            raise OSError(errno.EBADF, "closed parent directory handle")
        shutil.rmtree(directory.path.name, dir_fd=parent.descriptor)
        return
    parent_path = directory.path.parent
    parent_descriptor = os.open(parent_path, _directory_open_flags())
    try:
        shutil.rmtree(directory.path.name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


__all__ = [
    "DirectoryHandle",
    "IS_WINDOWS",
    "Identity",
    "PathIdentityError",
    "UnsafePathError",
    "absolute_path",
    "capture_directory_identities",
    "directory_identity",
    "entry_status",
    "file_lock",
    "fsync_directory",
    "identity",
    "list_directory",
    "mkdir_entry",
    "open_anchored_directory",
    "open_child_directory",
    "open_file",
    "open_path_file",
    "publish_directory_no_replace",
    "remove_directory_tree",
    "replace_entry",
    "replace_file",
    "rmdir_entry",
    "status_is_reparse",
    "unlink_entry",
]
