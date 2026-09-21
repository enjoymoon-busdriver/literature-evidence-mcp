from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literature_evidence_mcp.errors import LibraryRegistryError
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.platform_fs import (
    publish_directory_no_replace,
    replace_file,
)
from literature_evidence_mcp.registry import (
    REGISTRY_FORMAT,
    LibraryRegistry,
    default_application_root,
)


def _hold_registry_lock(application_root: str, ready, release) -> None:
    registry = LibraryRegistry(Path(application_root))
    with registry._exclusive_write_lock():
        ready.send("locked")
        release.recv()


@unittest.skipUnless(os.name == "nt", "real Windows filesystem coverage")
class WindowsStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="foliohook-windows-")
        self.root = Path(self.temporary.name)
        self.application_root = self.root / "application"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _junction(self, link: Path, target: Path) -> None:
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_default_root_uses_local_app_data_without_creating_it(self) -> None:
        local_app_data = self.root / "LocalAppData"
        with mock.patch.dict(
            os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=False
        ):
            expected = local_app_data / "literature-evidence-mcp"
            self.assertEqual(default_application_root(), expected)
            self.assertFalse(expected.exists())

    def test_read_with_only_installer_shim_does_not_create_lock(self) -> None:
        self.application_root.mkdir()
        (self.application_root / "mcp-server.cmd").write_text(
            "@echo off\r\n", encoding="utf-8"
        )
        registry = LibraryRegistry(self.application_root)
        self.assertEqual(registry.list_libraries(), [])
        self.assertFalse((self.application_root / ".registry.lock").exists())

        (self.application_root / "libraries").mkdir()
        with self.assertRaisesRegex(LibraryRegistryError, "缺少 Windows 协调锁"):
            registry.list_libraries()
        self.assertFalse((self.application_root / ".registry.lock").exists())

    def test_crud_two_snapshots_old_read_and_physical_deletion(self) -> None:
        registry = LibraryRegistry(self.application_root)
        first = registry.create("Windows 练习库", description="初始描述")
        second = registry.create("保留库")
        library_id = first["library_id"]
        library_root = registry.library_path(library_id)

        registry.rename(library_id, "Windows 本地库")
        registry.update_description(library_id, "NTFS 合成验收")
        registry.select(library_id)
        restarted = LibraryRegistry(self.application_root)
        selected = restarted.list_libraries()[0]
        self.assertEqual(selected["name"], "Windows 本地库")
        self.assertEqual(selected["description"], "NTFS 合成验收")
        self.assertTrue(selected["selected"])

        first_source = self.root / "first.md"
        second_source = self.root / "second.md"
        first_source.write_text(
            "# First\n\nwindowsoldsnapshotevidence\n", encoding="utf-8"
        )
        second_source.write_text(
            "# Second\n\nwindowsnewsnapshotevidence\n", encoding="utf-8"
        )
        library = FixedLibrary(library_root, library_id=library_id)
        first_snapshot = library.build([first_source])
        second_snapshot = library.build(
            [second_source], base_snapshot_id=first_snapshot["snapshot_id"]
        )

        self.assertTrue(
            library.search(
                first_snapshot["snapshot_id"], "windowsoldsnapshotevidence"
            )["found"]
        )
        self.assertFalse(
            library.search(
                first_snapshot["snapshot_id"], "windowsnewsnapshotevidence"
            )["found"]
        )
        self.assertTrue(
            library.search(
                second_snapshot["snapshot_id"], "windowsnewsnapshotevidence"
            )["found"]
        )
        self.assertEqual(len(library.list_snapshots()), 2)

        result = restarted.delete(library_id, "Windows 本地库")
        self.assertTrue(result["deleted"])
        self.assertFalse(library_root.exists())
        self.assertEqual(
            {path.name for path in (self.application_root / "libraries").iterdir()},
            {second["library_id"]},
        )
        self.assertEqual(result["selected_library_id"], second["library_id"])
        self.assertEqual(
            [item["library_id"] for item in restarted.list_libraries()],
            [second["library_id"]],
        )
        registry_payload = json.loads(
            (self.application_root / "registry.json").read_text(encoding="utf-8")
        )
        self.assertEqual(registry_payload["format"], REGISTRY_FORMAT)

    def test_nonblocking_lock_conflict_is_reported(self) -> None:
        registry = LibraryRegistry(self.application_root)
        created = registry.create("锁冲突")
        context = multiprocessing.get_context("spawn")
        ready_reader, ready_writer = context.Pipe(duplex=False)
        release_reader, release_writer = context.Pipe(duplex=False)
        holder = context.Process(
            target=_hold_registry_lock,
            args=(str(self.application_root), ready_writer, release_reader),
        )
        holder.start()
        ready_writer.close()
        release_reader.close()
        try:
            self.assertTrue(ready_reader.poll(10), "Windows lock holder did not start")
            self.assertEqual(ready_reader.recv(), "locked")
            with self.assertRaisesRegex(
                LibraryRegistryError, "另一个本地资料库写操作"
            ):
                registry.rename(created["library_id"], "不应写入")
        finally:
            try:
                release_writer.send("release")
            except (BrokenPipeError, EOFError, OSError):
                pass
            release_writer.close()
            ready_reader.close()
            holder.join(10)
            if holder.is_alive():
                holder.terminate()
                holder.join(10)
        self.assertEqual(holder.exitcode, 0)
        self.assertEqual(registry.list_libraries()[0]["name"], "锁冲突")

    def test_replace_and_no_replace_publication_are_distinct(self) -> None:
        replace_source = self.root / "replace-source"
        replace_target = self.root / "replace-target"
        replace_source.write_text("new", encoding="utf-8")
        replace_target.write_text("old", encoding="utf-8")
        replace_file(replace_source, replace_target)
        self.assertFalse(replace_source.exists())
        self.assertEqual(replace_target.read_text(encoding="utf-8"), "new")

        source = self.root / ".building-source"
        target = self.root / "existing-snapshot"
        source.mkdir()
        target.mkdir()
        (source / "new.txt").write_text("new", encoding="utf-8")
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            publish_directory_no_replace(source, target)
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual((source / "new.txt").read_text(encoding="utf-8"), "new")

    def test_application_root_junction_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        junction = self.root / "junction"
        self._junction(junction, outside)

        with self.assertRaisesRegex(
            LibraryRegistryError, "重解析点|符号链接"
        ):
            LibraryRegistry(junction / "application")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((outside / "application").exists())


if __name__ == "__main__":
    unittest.main()
