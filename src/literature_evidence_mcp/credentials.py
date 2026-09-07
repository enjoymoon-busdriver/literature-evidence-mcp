"""Instance-scoped macOS Keychain storage; no Keychain access during construction."""

from __future__ import annotations

import ctypes
import hashlib
import sys
from contextlib import ExitStack
from pathlib import Path

from .errors import LiteratureEvidenceError


_MESSAGES = {
    "invalid_kind": "不支持此密钥类别。",
    "invalid_key": "请输入有效密钥，密钥中不能包含空白或控制字符。",
    "missing": "尚未保存此密钥，请先填写并保存。",
    "denied": "钥匙串访问未获允许，请允许访问后手动重试。",
    "unavailable": "macOS 钥匙串当前不可用。",
    "read_failed": "无法读取密钥，请检查钥匙串后手动重试。",
    "write_failed": "无法保存密钥，请检查钥匙串后手动重试。",
}


class CredentialError(LiteratureEvidenceError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])


def _kind(kind: str) -> str:
    if kind not in ("aliyun", "openai_tunnel"):
        raise CredentialError("invalid_kind")
    return kind


def _key(value: str) -> str:
    if not isinstance(value, str):
        raise CredentialError("invalid_key")
    value = value.strip()
    if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise CredentialError("invalid_key")
    return value


class MacOSKeychain:
    def __init__(self, application_root: Path) -> None:
        identity = str(Path(application_root).expanduser().resolve()).encode("utf-8")
        self.service = "literature-evidence-mcp." + hashlib.sha256(identity).hexdigest()

    def get(self, kind: str) -> str:
        account = _kind(kind)
        try:
            return _key(_SecurityBridge().get(self.service, account))
        except CredentialError as error:
            code = error.code
        except Exception:
            code = "read_failed"
        # Raise outside the handler: even exception context must not retain a secret.
        raise CredentialError(code) from None

    def set(self, kind: str, key: str) -> None:
        account, value = _kind(kind), _key(key)
        try:
            _SecurityBridge().set(self.service, account, value)
            return
        except CredentialError as error:
            code = error.code
        except Exception:
            code = "write_failed"
        raise CredentialError(code) from None


class _SecurityBridge:
    """The secret crosses the C API in memory, never a subprocess or environment."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise CredentialError("unavailable")
        self.cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self.sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        pointer, index = ctypes.c_void_p, ctypes.c_long
        declarations = (
            (
                self.cf.CFStringCreateWithCString,
                [pointer, ctypes.c_char_p, ctypes.c_uint32],
                pointer,
            ),
            (self.cf.CFDataCreate, [pointer, ctypes.c_void_p, index], pointer),
            (self.cf.CFDataGetLength, [pointer], index),
            (self.cf.CFDataGetBytePtr, [pointer], pointer),
            (
                self.cf.CFDictionaryCreate,
                [pointer, ctypes.POINTER(pointer), ctypes.POINTER(pointer), index, pointer, pointer],
                pointer,
            ),
            (self.cf.CFRelease, [pointer], None),
            (
                self.sec.SecItemCopyMatching,
                [pointer, ctypes.POINTER(pointer)],
                ctypes.c_int32,
            ),
            (self.sec.SecItemUpdate, [pointer, pointer], ctypes.c_int32),
            (self.sec.SecItemAdd, [pointer, ctypes.POINTER(pointer)], ctypes.c_int32),
        )
        for function, arguments, result in declarations:
            function.argtypes, function.restype = arguments, result

    def _constant(self, name: str) -> int:
        library = self.cf if name.startswith("kCF") else self.sec
        return ctypes.c_void_p.in_dll(library, name).value

    def _own(self, stack: ExitStack, value: int) -> int:
        if not value:
            raise CredentialError("unavailable")
        stack.callback(self.cf.CFRelease, value)
        return value

    def _query(self, stack: ExitStack, service: str, account: str) -> dict[str, int]:
        return {
            "kSecClass": self._constant("kSecClassGenericPassword"),
            "kSecAttrService": self._own(
                stack,
                self.cf.CFStringCreateWithCString(None, service.encode("utf-8"), 0x08000100),
            ),
            "kSecAttrAccount": self._own(
                stack,
                self.cf.CFStringCreateWithCString(None, account.encode("utf-8"), 0x08000100),
            ),
        }

    def _dictionary(self, stack: ExitStack, entries: dict[str, int]) -> int:
        array = ctypes.c_void_p * len(entries)
        keys = array(*(self._constant(name) for name in entries))
        values = array(*entries.values())
        # Constant keys and all values outlive this dictionary via the same stack.
        return self._own(
            stack, self.cf.CFDictionaryCreate(None, keys, values, len(entries), None, None)
        )

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if status == 0:
            return
        if status == -25300:  # errSecItemNotFound
            code = "missing"
        elif status in (-128, -25293, -25308):
            code = "denied"
        elif status == -25291:  # errSecNotAvailable
            code = "unavailable"
        else:
            code = operation + "_failed"
        raise CredentialError(code)

    def get(self, service: str, account: str) -> str:
        with ExitStack() as stack:
            entries = self._query(stack, service, account)
            entries["kSecReturnData"] = self._constant("kCFBooleanTrue")
            entries["kSecMatchLimit"] = self._constant("kSecMatchLimitOne")
            query = self._dictionary(stack, entries)
            result = ctypes.c_void_p()
            status = self.sec.SecItemCopyMatching(query, ctypes.byref(result))
            if result.value:
                self._own(stack, result.value)
            self._check(status, "read")
            if not result.value:
                raise CredentialError("read_failed")
            data = ctypes.string_at(
                self.cf.CFDataGetBytePtr(result.value),
                self.cf.CFDataGetLength(result.value),
            )
            return data.decode("utf-8")

    def set(self, service: str, account: str, key: str) -> None:
        with ExitStack() as stack:
            entries = self._query(stack, service, account)
            query = self._dictionary(stack, entries)
            raw = key.encode("utf-8")
            data = self._own(stack, self.cf.CFDataCreate(None, raw, len(raw)))
            attributes = self._dictionary(stack, {"kSecValueData": data})
            status = self.sec.SecItemUpdate(query, attributes)
            if status == -25300:
                entries["kSecValueData"] = data
                status = self.sec.SecItemAdd(self._dictionary(stack, entries), None)
            self._check(status, "write")
