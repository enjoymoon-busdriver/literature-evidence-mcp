from __future__ import annotations

import ctypes
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literature_evidence_mcp.credentials import CredentialError, MacOSKeychain, _SecurityBridge


class FakeSecurityRuntime:
    """In-memory stand-in for both C frameworks; never loads or calls Keychain."""

    def __init__(self) -> None:
        self.references: dict[int, object] = {}
        self.constants: dict[str, int] = {}
        self.items: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.buffers: list[object] = []
        self.next_pointer = 1000
        self.read_status = 0
        self.write_status = 0
        self.cf = SimpleNamespace(
            CFStringCreateWithCString=mock.Mock(side_effect=lambda _, value, encoding: self.own(value.decode("utf-8"))),
            CFDataCreate=mock.Mock(side_effect=lambda _, value, size: self.own(bytes(value[:size]))),
            CFDataGetLength=mock.Mock(side_effect=lambda pointer: len(self.references[pointer])),
            CFDataGetBytePtr=mock.Mock(side_effect=self.byte_pointer),
            CFDictionaryCreate=mock.Mock(side_effect=self.dictionary),
            CFRelease=mock.Mock(side_effect=lambda pointer: self.references.pop(pointer)),
        )
        self.sec = SimpleNamespace(
            SecItemCopyMatching=mock.Mock(side_effect=self.read),
            SecItemUpdate=mock.Mock(side_effect=self.update),
            SecItemAdd=mock.Mock(side_effect=self.add),
        )

    def own(self, value: object) -> int:
        self.next_pointer += 1
        self.references[self.next_pointer] = value
        return self.next_pointer

    def constant(self, name: str) -> int:
        if name not in self.constants:
            self.constants[name] = len(self.constants) + 1
        return self.constants[name]

    def dictionary(self, allocator, keys, values, size, key_callbacks, value_callbacks):
        names = {pointer: name for name, pointer in self.constants.items()}
        entries = {
            names[keys[i]]: self.references.get(values[i], names.get(values[i]))
            for i in range(size)
        }
        return self.own(entries)

    def byte_pointer(self, pointer: int) -> int:
        buffer = ctypes.create_string_buffer(self.references[pointer])
        self.buffers.append(buffer)
        return ctypes.addressof(buffer)

    @staticmethod
    def identity(entries: dict) -> tuple[str, str]:
        return entries["kSecAttrService"], entries["kSecAttrAccount"]

    def read(self, query, output):
        entries = self.references[query]
        self.calls.append(("get", entries))
        if self.read_status:
            return self.read_status
        if self.identity(entries) not in self.items:
            return -25300
        pointer = self.own(self.items[self.identity(entries)])
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = pointer
        return 0

    def update(self, query, attributes):
        entries, changes = self.references[query], self.references[attributes]
        self.calls.append(("update", entries))
        if self.write_status:
            return self.write_status
        if self.identity(entries) not in self.items:
            return -25300
        self.items[self.identity(entries)] = changes["kSecValueData"]
        return 0

    def add(self, attributes, output):
        entries = self.references[attributes]
        self.calls.append(("add", entries))
        self.items[self.identity(entries)] = entries["kSecValueData"]
        return 0


class RealCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeSecurityRuntime()
        self.patches = [
            mock.patch("literature_evidence_mcp.credentials.sys.platform", "darwin"),
            mock.patch("literature_evidence_mcp.credentials.ctypes.CDLL", side_effect=lambda path: self.runtime.cf if "CoreFoundation" in path else self.runtime.sec),
            mock.patch.object(_SecurityBridge, "_constant", side_effect=self.runtime.constant),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.store = MacOSKeychain(Path("/private/tmp/synthetic-credentials-instance"))

    def tearDown(self) -> None:
        self.assertEqual(self.runtime.references, {}, "Every owned C object must be released")

    def test_construction_and_invalid_inputs_never_load_keychain(self) -> None:
        with mock.patch("literature_evidence_mcp.credentials.ctypes.CDLL") as loader:
            store = MacOSKeychain(Path("/private/tmp/other-synthetic-instance"))
            for kind in ("old_ragmcp", "", None):
                with self.assertRaises(CredentialError) as caught:
                    store.get(kind)
                self.assertEqual(caught.exception.code, "invalid_kind")
            for key in ("", "  ", "a\nb", "a\x00b", "a\tb", 1):
                with self.assertRaises(CredentialError) as caught:
                    store.set("aliyun", key)
                self.assertEqual(caught.exception.code, "invalid_key")
            loader.assert_not_called()
        self.assertEqual(self.runtime.calls, [])

    def test_create_replace_read_and_two_kinds_use_exact_instance_identity(self) -> None:
        self.store.set("aliyun", " SYNTHETIC_FIRST ")
        self.store.set("aliyun", "SYNTHETIC_REPLACEMENT")
        self.store.set("openai_tunnel", "SYNTHETIC_TUNNEL")
        self.assertEqual(self.store.get("aliyun"), "SYNTHETIC_REPLACEMENT")
        self.assertEqual(self.store.get("openai_tunnel"), "SYNTHETIC_TUNNEL")
        self.assertEqual([call[0] for call in self.runtime.calls], ["update", "add", "update", "update", "add", "get", "get"])
        for action, query in self.runtime.calls:
            self.assertEqual(query["kSecClass"], "kSecClassGenericPassword")
            self.assertEqual(query["kSecAttrService"], self.store.service)
            self.assertIn(query["kSecAttrAccount"], ("aliyun", "openai_tunnel"))
            if action == "get":
                self.assertEqual(query["kSecMatchLimit"], "kSecMatchLimitOne")
                self.assertEqual(query["kSecReturnData"], "kCFBooleanTrue")
                self.assertNotIn("kSecValueData", query)
        self.assertEqual(self.runtime.sec.SecItemCopyMatching.restype, ctypes.c_int32)
        self.assertEqual(self.runtime.cf.CFDataGetBytePtr.restype, ctypes.c_void_p)

    def test_new_application_root_cannot_read_another_instances_key(self) -> None:
        self.store.set("aliyun", "SYNTHETIC_INSTANCE_ONE")
        other = MacOSKeychain(Path("/private/tmp/synthetic-credentials-instance-two"))
        equivalent = MacOSKeychain(Path("/private/tmp/synthetic-credentials-instance/./"))
        self.assertEqual(equivalent.service, self.store.service)
        self.assertNotEqual(other.service, self.store.service)
        self.assertTrue(other.service.startswith("literature-evidence-mcp."))
        self.assertNotIn("/private", other.service)
        with self.assertRaises(CredentialError) as caught:
            other.get("aliyun")
        self.assertEqual(caught.exception.code, "missing")
        self.assertEqual(equivalent.get("aliyun"), "SYNTHETIC_INSTANCE_ONE")

    def test_denial_missing_and_unknown_errors_do_not_retry(self) -> None:
        for status, expected in ((-128, "denied"), (-25293, "denied"), (-25308, "denied"), (-25291, "unavailable"), (-25300, "missing"), (-50, "read_failed")):
            with self.subTest(status=status):
                self.runtime.calls.clear()
                self.runtime.read_status = status
                with self.assertRaises(CredentialError) as caught:
                    self.store.get("aliyun")
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(len(self.runtime.calls), 1)
        for status, expected in ((-128, "denied"), (-50, "write_failed")):
            self.runtime.calls.clear()
            self.runtime.write_status = status
            with self.assertRaises(CredentialError) as caught:
                self.store.set("aliyun", "SYNTHETIC_SECRET")
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual([call[0] for call in self.runtime.calls], ["update"])

    def test_bridge_exceptions_cannot_expose_secret_or_exception_context(self) -> None:
        for method in ("get", "set"):
            with self.subTest(method=method), mock.patch.object(
                _SecurityBridge, method, side_effect=RuntimeError("SYNTHETIC_SECRET")
            ):
                with self.assertRaises(CredentialError) as caught:
                    if method == "get":
                        self.store.get("aliyun")
                    else:
                        self.store.set("aliyun", "SYNTHETIC_SECRET")
                self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)

    def test_corrupt_stored_bytes_are_redacted_and_released(self) -> None:
        self.runtime.items[(self.store.service, "aliyun")] = b"SYNTHETIC_SECRET\xff"
        with self.assertRaises(CredentialError) as caught:
            self.store.get("aliyun")
        self.assertEqual(caught.exception.code, "read_failed")
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))

    def test_non_mac_platform_fails_only_on_explicit_access(self) -> None:
        with mock.patch("literature_evidence_mcp.credentials.sys.platform", "linux"), mock.patch("literature_evidence_mcp.credentials.ctypes.CDLL") as loader:
            store = MacOSKeychain(Path("/private/tmp/synthetic-nonmac-instance"))
            with self.assertRaises(CredentialError) as caught:
                store.get("aliyun")
            self.assertEqual(caught.exception.code, "unavailable")
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
