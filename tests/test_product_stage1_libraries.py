from __future__ import annotations

import io
import json
import multiprocessing
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from multiprocessing.connection import Connection
from pathlib import Path
from unittest import mock

import fcntl

from literature_evidence_mcp import build_snapshot, search_snapshot
from literature_evidence_mcp.cli import main as cli_main
from literature_evidence_mcp.errors import LibraryRegistryError
from literature_evidence_mcp.mcp_server import TOOLS
from literature_evidence_mcp.registry import (
    LIBRARIES_DIRECTORY_NAME,
    LOCK_NAME,
    REGISTRY_NAME,
    LibraryRegistry,
    default_application_root,
)


_LIBRARY_ID = re.compile(r"\Alib_[0-9a-f]{32}\Z")


def _tree_state(root: Path) -> tuple[tuple[str, str, object], ...]:
    entries: list[tuple[str, str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
        elif path.is_dir():
            entries.append((relative, "directory", None))
        else:
            entries.append((relative, "other", path.lstat().st_mode))
    return tuple(entries)


def _hold_registry_write_lock(
    application_root: str,
    ready: Connection,
    release: Connection,
) -> None:
    registry = LibraryRegistry(Path(application_root))
    with registry._exclusive_write_lock():
        ready.send("locked")
        release.recv()


class ProductStageOneLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lemcp-product-stage1-")
        self.root = Path(self.temporary.name).resolve()
        self.application_root = self.root / "Application Support" / "literature-evidence-mcp"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run_cli(self, arguments: list[str]) -> tuple[int, dict[str, object], str]:
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch(
            "literature_evidence_mcp.cli.default_application_root",
            return_value=self.application_root,
        ):
            with redirect_stdout(output), redirect_stderr(errors):
                status = cli_main(arguments)
        payload = json.loads(output.getvalue()) if output.getvalue() else {}
        return status, payload, errors.getvalue()

    def test_readonly_list_does_not_create_the_fixed_application_root(self) -> None:
        expected = (
            self.root.resolve()
            / "Library"
            / "Application Support"
            / "literature-evidence-mcp"
        )
        self.assertEqual(default_application_root(home=self.root), expected)
        alias_registry = LibraryRegistry(
            Path(self.temporary.name) / "system-alias-compatible-app"
        )
        self.assertEqual(alias_registry.list_libraries(), [])
        self.assertEqual(
            alias_registry.application_root,
            self.root / "system-alias-compatible-app",
        )
        registry = LibraryRegistry(self.application_root)
        self.assertEqual(registry.list_libraries(), [])
        with self.assertRaisesRegex(LibraryRegistryError, "尚未选择"):
            registry.selected_library_path()
        self.assertFalse(self.application_root.exists())

        self.assertEqual(len(TOOLS), 8)
        for tool in TOOLS:
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)

    def test_same_names_metadata_selection_and_restart_keep_stable_ids(self) -> None:
        registry = LibraryRegistry(self.application_root)
        first = registry.create("同名资料库", description="第一份")
        second = registry.create("同名资料库", description="第二份")

        self.assertRegex(first["library_id"], _LIBRARY_ID)
        self.assertRegex(second["library_id"], _LIBRARY_ID)
        self.assertNotEqual(first["library_id"], second["library_id"])
        self.assertTrue(first["selected"])
        self.assertFalse(second["selected"])
        first_root = Path(first["library_root"])
        second_root = Path(second["library_root"])
        self.assertEqual(
            first_root,
            self.application_root.resolve()
            / LIBRARIES_DIRECTORY_NAME
            / first["library_id"],
        )
        self.assertNotEqual(first_root, second_root)

        renamed = registry.rename(first["library_id"], "重命名后的资料库")
        described = registry.update_description(first["library_id"], "更新后的描述")
        selected = registry.select(second["library_id"])
        self.assertEqual(renamed["library_id"], first["library_id"])
        self.assertEqual(described["library_id"], first["library_id"])
        self.assertEqual(Path(described["library_root"]), first_root)
        self.assertEqual(selected["library_id"], second["library_id"])

        restarted = LibraryRegistry(self.application_root)
        listed = restarted.list_libraries()
        self.assertEqual(
            [item["library_id"] for item in listed],
            [first["library_id"], second["library_id"]],
        )
        self.assertEqual(listed[0]["name"], "重命名后的资料库")
        self.assertEqual(listed[0]["description"], "更新后的描述")
        self.assertFalse(listed[0]["selected"])
        self.assertTrue(listed[1]["selected"])
        self.assertEqual(restarted.selected_library_path(), second_root)

        persisted = (self.application_root / REGISTRY_NAME).read_text(encoding="utf-8")
        self.assertNotIn(str(self.application_root.resolve()), persisted)
        self.assertNotIn("library_root", persisted)

    def test_cli_create_rename_describe_select_and_list_are_explicit(self) -> None:
        status, payload, errors = self._run_cli(
            ["libraries", "create", "CLI 资料库", "--description", "初始描述"]
        )
        self.assertEqual(status, 0, errors)
        self.assertEqual(errors, "")
        library_id = payload["library"]["library_id"]
        registry_path = self.application_root / REGISTRY_NAME
        before_list = registry_path.read_bytes()

        status, listed, errors = self._run_cli(["libraries", "list"])
        self.assertEqual(status, 0, errors)
        self.assertEqual(listed["libraries"][0]["library_id"], library_id)
        self.assertEqual(registry_path.read_bytes(), before_list)

        for arguments, key, expected in (
            (["libraries", "rename", library_id, "CLI 新名称"], "name", "CLI 新名称"),
            (
                ["libraries", "describe", library_id, "CLI 新描述"],
                "description",
                "CLI 新描述",
            ),
            (["libraries", "select", library_id], "selected", True),
        ):
            status, changed, errors = self._run_cli(arguments)
            self.assertEqual(status, 0, errors)
            self.assertEqual(changed["library"]["library_id"], library_id)
            self.assertEqual(changed["library"][key], expected)

    def test_same_filenames_and_identical_bytes_stay_physically_isolated(self) -> None:
        registry = LibraryRegistry(self.application_root)
        first = registry.create("A")
        second = registry.create("B")
        first_root = registry.library_path(first["library_id"])
        second_root = registry.library_path(second["library_id"])

        first_inputs = self.root / "first-inputs"
        second_inputs = self.root / "second-inputs"
        first_inputs.mkdir()
        second_inputs.mkdir()
        first_source = first_inputs / "same-name.md"
        second_source = second_inputs / "same-name.md"
        first_source.write_text("# A\n\nalphaonly sharedterm\n", encoding="utf-8")
        second_source.write_text("# B\n\nbetaonly sharedterm\n", encoding="utf-8")

        first_snapshot = build_snapshot(first_root, [first_source])
        second_snapshot = build_snapshot(second_root, [second_source])
        first_snapshot_path = Path(first_snapshot["snapshot_path"])
        second_snapshot_path = Path(second_snapshot["snapshot_path"])
        self.assertTrue(search_snapshot(first_snapshot_path, "alphaonly")["found"])
        self.assertFalse(search_snapshot(first_snapshot_path, "betaonly")["found"])
        self.assertTrue(search_snapshot(second_snapshot_path, "betaonly")["found"])
        self.assertFalse(search_snapshot(second_snapshot_path, "alphaonly")["found"])
        self.assertTrue(first_snapshot_path.is_relative_to(first_root))
        self.assertTrue(second_snapshot_path.is_relative_to(second_root))

        identical = b"# Identical\n\nbyte identical evidence\n"
        first_identical = first_inputs / "identical.md"
        second_identical = second_inputs / "identical.md"
        first_identical.write_bytes(identical)
        second_identical.write_bytes(identical)
        first_copy_snapshot = Path(
            build_snapshot(first_root, [first_identical])["snapshot_path"]
        )
        second_copy_snapshot = Path(
            build_snapshot(second_root, [second_identical])["snapshot_path"]
        )
        first_copy = next((first_copy_snapshot / "sources").iterdir())
        second_copy = next((second_copy_snapshot / "sources").iterdir())
        self.assertEqual(first_copy.read_bytes(), second_copy.read_bytes())
        self.assertNotEqual(first_copy.resolve(), second_copy.resolve())
        self.assertFalse(first_copy.samefile(second_copy))
        self.assertTrue(first_copy.resolve().is_relative_to(first_root))
        self.assertTrue(second_copy.resolve().is_relative_to(second_root))

    def test_library_id_traversal_and_managed_library_symlink_are_rejected(self) -> None:
        registry = LibraryRegistry(self.application_root)
        created = registry.create("安全边界")
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")

        for value in ("../outside", "/absolute/path", "lib_" + "1" * 31 + "/"):
            with self.subTest(value=value):
                with self.assertRaises(LibraryRegistryError):
                    registry.select(value)
                with self.assertRaises(LibraryRegistryError):
                    registry.library_path(value)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

        managed = registry.library_path(created["library_id"])
        managed.rmdir()
        managed.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(LibraryRegistryError, "符号链接"):
            registry.list_libraries()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_root_collection_and_registry_symlinks_are_rejected(self) -> None:
        outside = self.root / "outside-root"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")

        intermediate = self.root / "redirected-parent"
        intermediate.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(LibraryRegistryError, "不能经过符号链接"):
            LibraryRegistry(intermediate / "nested-application-root")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

        linked_root = self.root / "linked-application-root"
        linked_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(LibraryRegistryError, "根目录不能是符号链接"):
            LibraryRegistry(linked_root)

        collection_app = self.root / "collection-app"
        collection_app.mkdir()
        (collection_app / LIBRARIES_DIRECTORY_NAME).symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaisesRegex(LibraryRegistryError, "符号链接"):
            LibraryRegistry(collection_app).list_libraries()

        registry_app = self.root / "registry-app"
        registry_app.mkdir()
        external_registry = outside / "external-registry.json"
        external_registry.write_text("{}\n", encoding="utf-8")
        (registry_app / REGISTRY_NAME).symlink_to(external_registry)
        with self.assertRaisesRegex(LibraryRegistryError, "符号链接"):
            LibraryRegistry(registry_app).list_libraries()
        self.assertEqual(external_registry.read_text(encoding="utf-8"), "{}\n")

        lock_app = self.root / "lock-symlink-app"
        lock_registry = LibraryRegistry(lock_app)
        lock_record = lock_registry.create("锁路径")
        (lock_app / LOCK_NAME).unlink()
        external_lock = outside / "external-lock"
        external_lock.write_text("unchanged", encoding="utf-8")
        (lock_app / LOCK_NAME).symlink_to(external_lock)
        with self.assertRaisesRegex(LibraryRegistryError, "写锁.*符号链接"):
            lock_registry.rename(lock_record["library_id"], "不应更新")
        self.assertEqual(external_lock.read_text(encoding="utf-8"), "unchanged")

    def test_write_lock_and_failure_paths_keep_the_registry_consistent(self) -> None:
        registry = LibraryRegistry(self.application_root)
        created = registry.create("锁测试")
        descriptor = os.open(self.application_root / LOCK_NAME, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(LibraryRegistryError, "另一个本地资料库写操作"):
                registry.rename(created["library_id"], "不应写入")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.assertEqual(registry.list_libraries()[0]["name"], "锁测试")

        size_root = self.root / "size-limited-app"
        size_registry = LibraryRegistry(size_root)
        with mock.patch(
            "literature_evidence_mcp.registry._REGISTRY_MAX_BYTES", 100
        ):
            with self.assertRaisesRegex(LibraryRegistryError, "大小上限"):
                size_registry.create("不能写出自我不可读的注册表")
        self.assertEqual(size_registry.list_libraries(), [])
        self.assertEqual(list((size_root / LIBRARIES_DIRECTORY_NAME).iterdir()), [])

        fsync_root = self.root / "post-replace-fsync-app"
        fsync_registry = LibraryRegistry(fsync_root)
        failure = LibraryRegistryError("模拟目录同步失败。")
        with mock.patch(
            "literature_evidence_mcp.registry._fsync_directory",
            side_effect=[None, failure],
        ):
            with self.assertRaisesRegex(LibraryRegistryError, "模拟目录同步失败"):
                fsync_registry.create("已发布但同步结果不确定")
        restarted = LibraryRegistry(fsync_root)
        listed = restarted.list_libraries()
        self.assertEqual(len(listed), 1)
        self.assertTrue(Path(listed[0]["library_root"]).is_dir())

    def test_post_construction_ancestor_swap_rejects_all_mutations(self) -> None:
        for action_name in ("create", "select", "rename", "describe"):
            with self.subTest(action=action_name):
                case_root = self.root / f"ancestor-swap-{action_name}"
                managed_parent = case_root / "managed-parent"
                outside_parent = case_root / "outside-parent"
                managed_parent.mkdir(parents=True)

                registry = LibraryRegistry(managed_parent / "application")
                first = registry.create("第一库")
                second = registry.create("第二库")

                managed_parent.rename(outside_parent)
                managed_parent.symlink_to(outside_parent, target_is_directory=True)
                before = _tree_state(outside_parent)
                actions = {
                    "create": lambda: registry.create("不应创建"),
                    "select": lambda: registry.select(second["library_id"]),
                    "rename": lambda: registry.rename(
                        first["library_id"], "不应重命名"
                    ),
                    "describe": lambda: registry.update_description(
                        first["library_id"], "不应更新"
                    ),
                }

                with self.assertRaises(LibraryRegistryError):
                    actions[action_name]()
                self.assertEqual(_tree_state(outside_parent), before)

    def test_missing_registry_with_managed_directory_fails_closed(self) -> None:
        for action_name in ("list", "create"):
            with self.subTest(action=action_name):
                application_root = self.root / f"missing-registry-{action_name}"
                registry = LibraryRegistry(application_root)
                created = registry.create("已有库")
                sentinel = Path(created["library_root"]) / "sentinel.txt"
                sentinel.write_text("unchanged", encoding="utf-8")
                (application_root / REGISTRY_NAME).unlink()
                before = _tree_state(application_root)

                restarted = LibraryRegistry(application_root)
                action = (
                    restarted.list_libraries
                    if action_name == "list"
                    else lambda: restarted.create("不应新建")
                )
                with self.assertRaisesRegex(LibraryRegistryError, "注册表缺失"):
                    action()

                self.assertEqual(_tree_state(application_root), before)
                self.assertFalse((application_root / REGISTRY_NAME).exists())
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

        manual_root = self.root / "missing-registry-without-lock"
        managed = (
            manual_root
            / LIBRARIES_DIRECTORY_NAME
            / ("lib_" + "a" * 32)
        )
        managed.mkdir(parents=True)
        (managed / "sentinel.txt").write_text("unchanged", encoding="utf-8")
        before = _tree_state(manual_root)
        with self.assertRaisesRegex(LibraryRegistryError, "注册表缺失"):
            LibraryRegistry(manual_root).create("不应新建")
        self.assertEqual(_tree_state(manual_root), before)
        self.assertFalse((manual_root / LOCK_NAME).exists())

        empty_root = self.root / "pure-empty-layout"
        (empty_root / LIBRARIES_DIRECTORY_NAME).mkdir(parents=True)
        created = LibraryRegistry(empty_root).create("空布局可创建")
        self.assertTrue(Path(created["library_root"]).is_dir())

    def test_recreated_lock_path_cannot_bypass_held_directory_lock(self) -> None:
        holder = LibraryRegistry(self.application_root)
        created = holder.create("原名称")
        registry_path = self.application_root / REGISTRY_NAME
        lock_path = self.application_root / LOCK_NAME

        context = multiprocessing.get_context("fork")
        ready_reader, ready_writer = context.Pipe(duplex=False)
        release_reader, release_writer = context.Pipe(duplex=False)
        process = context.Process(
            target=_hold_registry_write_lock,
            args=(str(self.application_root), ready_writer, release_reader),
        )
        process.start()
        ready_writer.close()
        release_reader.close()
        try:
            self.assertTrue(ready_reader.poll(5), "持锁子进程未及时就绪")
            self.assertEqual(ready_reader.recv(), "locked")

            old_inode = lock_path.stat().st_ino
            lock_path.unlink()
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(descriptor)
            self.assertNotEqual(lock_path.stat().st_ino, old_inode)
            contender = LibraryRegistry(self.application_root)
            before = _tree_state(self.application_root)
            before_registry = registry_path.read_bytes()

            with self.assertRaisesRegex(
                LibraryRegistryError, "另一个本地资料库写操作"
            ):
                contender.rename(created["library_id"], "不应并发写入")
            self.assertEqual(registry_path.read_bytes(), before_registry)
            self.assertEqual(_tree_state(self.application_root), before)
        finally:
            try:
                release_writer.send("release")
            except (BrokenPipeError, EOFError, OSError):
                pass
            release_writer.close()
            ready_reader.close()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(holder.list_libraries()[0]["name"], "原名称")

    def test_path_home_runtime_error_becomes_registry_error(self) -> None:
        with mock.patch(
            "literature_evidence_mcp.registry.Path.home",
            side_effect=RuntimeError("synthetic home failure"),
        ):
            with self.assertRaisesRegex(
                LibraryRegistryError, "无法确定受控应用目录"
            ) as caught:
                default_application_root()
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch(
            "literature_evidence_mcp.registry.Path.home",
            side_effect=RuntimeError("synthetic home failure"),
        ):
            with redirect_stdout(output), redirect_stderr(errors):
                status = cli_main(["libraries", "list"])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("无法确定受控应用目录", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_macos_var_and_tmp_top_level_aliases_remain_supported(self) -> None:
        if not Path("/var").is_symlink() or not Path("/tmp").is_symlink():
            self.skipTest("仅适用于 macOS 顶级系统路径别名")
        if self.root.parts[1:3] != ("private", "var"):
            self.skipTest("当前测试临时目录不位于 /private/var")

        var_alias = Path("/var").joinpath(*self.root.parts[3:])
        with tempfile.TemporaryDirectory(
            prefix="lemcp-tmp-alias-",
            dir="/tmp",
        ) as temporary_name:
            for label, raw_base in (
                ("var", var_alias),
                ("tmp", Path(temporary_name)),
            ):
                with self.subTest(alias=label):
                    requested = raw_base / f"{label}-application"
                    expected = requested.resolve()
                    registry = LibraryRegistry(requested)
                    self.assertEqual(registry.application_root, expected)
                    self.assertEqual(registry.list_libraries(), [])
                    created = registry.create(label)
                    self.assertTrue(Path(created["library_root"]).is_dir())

        if Path("/etc").is_symlink():
            with self.assertRaisesRegex(LibraryRegistryError, "仅允许.*var.*tmp"):
                LibraryRegistry(Path("/etc") / "lemcp-must-not-follow")


if __name__ == "__main__":
    unittest.main()
