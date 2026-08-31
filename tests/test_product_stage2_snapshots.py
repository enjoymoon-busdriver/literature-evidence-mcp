from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literature_evidence_mcp import snapshot as snapshot_module
from literature_evidence_mcp.catalog import (
    load_snapshot_catalog,
    snapshot_catalog_lock,
    write_snapshot_catalog,
)
from literature_evidence_mcp.cli import main as cli_main
from literature_evidence_mcp.errors import SearchInputError, SnapshotError
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.mcp_tools import ReadOnlyEvidenceTools
from literature_evidence_mcp.registry import LibraryRegistry


def _write_markdown(path: Path, marker: str) -> None:
    path.write_text(f"# {path.stem}\n\n{marker} evidence.\n", encoding="utf-8")


def _manifest(snapshot: Path) -> dict[str, object]:
    return json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))


def _source_ids(snapshot: Path) -> dict[str, str]:
    return {
        item["source_name"]: item["document_id"]
        for item in _manifest(snapshot)["sources"]
    }


def _tree_identity(root: Path) -> dict[str, tuple[str, str]]:
    identity: dict[str, tuple[str, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            identity[relative] = ("symlink", str(path.readlink()))
        elif path.is_dir():
            identity[relative] = ("directory", "")
        elif path.is_file():
            identity[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return identity


class ProductStageTwoSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lemcp-product-stage2-")
        self.root = Path(self.temporary.name)
        self.application_root = self.root / "application"
        self.registry = LibraryRegistry(self.application_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_library(self, name: str) -> tuple[str, Path, FixedLibrary]:
        record = self.registry.create(name)
        path = Path(record["library_root"])
        return record["library_id"], path, FixedLibrary(path)

    def test_complete_membership_current_last_and_explicit_activation(self) -> None:
        _library_id, library_path, library = self._create_library("Lifecycle")
        source_root = self.root / "sources"
        source_root.mkdir()
        a = source_root / "A.md"
        b = source_root / "B.md"
        c = source_root / "C.md"
        replacement = source_root / "B-replacement.md"
        for path, marker in (
            (a, "alphaunique"),
            (b, "bravounique"),
            (c, "charlieunique"),
            (replacement, "bravoreplacementunique"),
        ):
            _write_markdown(path, marker)

        first = library.build([a, b])
        first_path = library_path / "snapshots" / first["snapshot_id"]
        first_identity = _tree_identity(first_path)
        self.assertEqual(set(_source_ids(first_path)), {"A.md", "B.md"})
        self.assertEqual(first["current_snapshot_id"], first["snapshot_id"])
        self.assertEqual(
            first["last_successful_snapshot_id"], first["snapshot_id"]
        )

        second = library.build([c], base_snapshot_id=first["snapshot_id"])
        second_path = library_path / "snapshots" / second["snapshot_id"]
        self.assertEqual(first_identity, _tree_identity(first_path))
        self.assertEqual(set(_source_ids(second_path)), {"A.md", "B.md", "C.md"})
        self.assertEqual(second["current_snapshot_id"], first["snapshot_id"])
        self.assertEqual(
            second["last_successful_snapshot_id"], second["snapshot_id"]
        )

        first_a = next((first_path / "sources").glob(f"{_source_ids(first_path)['A.md']}.*"))
        second_a = next((second_path / "sources").glob(f"{_source_ids(second_path)['A.md']}.*"))
        self.assertNotEqual(first_a.stat().st_ino, second_a.stat().st_ino)
        self.assertEqual(first_a.stat().st_nlink, 1)
        self.assertEqual(second_a.stat().st_nlink, 1)

        second_ids = _source_ids(second_path)
        third = library.build(
            [],
            base_snapshot_id=second["snapshot_id"],
            replacements={second_ids["B.md"]: replacement},
            remove_document_ids=[second_ids["A.md"]],
        )
        third_path = library_path / "snapshots" / third["snapshot_id"]
        self.assertEqual(
            set(_source_ids(third_path)), {"B-replacement.md", "C.md"}
        )
        self.assertEqual(first_identity, _tree_identity(first_path))
        self.assertEqual(third["current_snapshot_id"], first["snapshot_id"])
        self.assertEqual(third["last_successful_snapshot_id"], third["snapshot_id"])

        activated = library.activate(second["snapshot_id"])
        self.assertEqual(activated["current_snapshot_id"], second["snapshot_id"])
        self.assertEqual(
            activated["last_successful_snapshot_id"], third["snapshot_id"]
        )
        states = {item["snapshot_id"]: item for item in library.list_snapshots()}
        self.assertTrue(states[second["snapshot_id"]]["current"])
        self.assertTrue(states[third["snapshot_id"]]["last_successful"])

    def test_failed_build_and_invalid_activation_leave_pointers_unchanged(self) -> None:
        _library_id, library_path, library = self._create_library("Failure")
        a = self.root / "A.md"
        b = self.root / "B.md"
        _write_markdown(a, "alpha")
        _write_markdown(b, "bravo")
        first = library.build([a])
        before_catalog = (library_path / "snapshot-catalog.json").read_bytes()
        before_snapshots = _tree_identity(library_path / "snapshots")

        with mock.patch.object(
            snapshot_module,
            "write_snapshot_catalog",
            side_effect=SnapshotError("forced catalog failure"),
        ):
            with self.assertRaises(SnapshotError):
                library.build([b])
        self.assertEqual(
            before_catalog, (library_path / "snapshot-catalog.json").read_bytes()
        )
        self.assertEqual(before_snapshots, _tree_identity(library_path / "snapshots"))
        self.assertFalse(
            any(
                path.name.startswith(".building-")
                for path in (library_path / "snapshots").iterdir()
            )
        )

        missing_id = "20260101T000000000000Z-" + "0" * 12 + "-" + "0" * 8
        with self.assertRaises(SnapshotError):
            library.activate(missing_id)
        self.assertEqual(
            library.catalog_status()["current_snapshot_id"], first["snapshot_id"]
        )

        source = next((library_path / "snapshots" / first["snapshot_id"] / "sources").iterdir())
        source.write_bytes(source.read_bytes() + b"tampered")
        with self.assertRaises(SnapshotError):
            library.activate(first["snapshot_id"])
        self.assertEqual(
            before_catalog, (library_path / "snapshot-catalog.json").read_bytes()
        )

    def test_cataloged_snapshot_id_is_never_reused_after_directory_loss(self) -> None:
        _library_id, library_path, library = self._create_library("No reuse")
        source = self.root / "stable.md"
        _write_markdown(source, "stable")

        class FrozenDateTime:
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2026, 1, 1, tzinfo=timezone.utc)

        fixed_uuid = SimpleNamespace(hex="1" * 32)
        with (
            mock.patch.object(snapshot_module, "datetime", FrozenDateTime),
            mock.patch.object(snapshot_module.uuid, "uuid4", return_value=fixed_uuid),
        ):
            first = library.build([source])
            shutil.rmtree(library_path / "snapshots")
            listed = library.list_snapshots()
            self.assertEqual(
                [item["snapshot_id"] for item in listed], [first["snapshot_id"]]
            )
            self.assertFalse(listed[0]["verified"])
            before_catalog = (library_path / "snapshot-catalog.json").read_bytes()
            with self.assertRaises(SnapshotError):
                library.build([source])
        self.assertEqual(
            before_catalog, (library_path / "snapshot-catalog.json").read_bytes()
        )

    def test_interrupt_after_catalog_commit_keeps_registered_snapshot(self) -> None:
        _library_id, library_path, library = self._create_library("Commit point")
        first_source = self.root / "first.md"
        second_source = self.root / "second.md"
        _write_markdown(first_source, "first")
        _write_markdown(second_source, "second")
        first = library.build([first_source])

        def commit_then_interrupt(path: Path, payload: dict[str, object]) -> None:
            write_snapshot_catalog(path, payload)
            raise KeyboardInterrupt

        with mock.patch.object(
            snapshot_module,
            "write_snapshot_catalog",
            side_effect=commit_then_interrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                library.build([second_source])

        catalog = load_snapshot_catalog(library_path)
        second_id = catalog["last_successful_snapshot_id"]
        self.assertNotEqual(second_id, first["snapshot_id"])
        self.assertEqual(len(catalog["snapshots"]), 2)
        self.assertTrue((library_path / "snapshots" / second_id).is_dir())
        self.assertTrue(library.verify(second_id)["verified"])

    def test_same_snapshot_id_across_libraries_never_crosses_content_or_ids(self) -> None:
        first_id, first_path, first_library = self._create_library("First")
        second_id, second_path, second_library = self._create_library("Second")
        first_sources = self.root / "first-sources"
        second_sources = self.root / "second-sources"
        first_sources.mkdir()
        second_sources.mkdir()
        common_first = first_sources / "common.md"
        common_second = second_sources / "common.md"
        alpha = first_sources / "same-name.md"
        beta = second_sources / "same-name.md"
        _write_markdown(common_first, "commonshared")
        shutil.copyfile(common_first, common_second)
        _write_markdown(alpha, "alphaonlymarker")
        _write_markdown(beta, "betaonlymarker")

        first = first_library.build([common_first, alpha])
        second = second_library.build([common_second, beta])
        shared_snapshot_id = first["snapshot_id"]
        original_second = second_path / "snapshots" / second["snapshot_id"]
        renamed_second = second_path / "snapshots" / shared_snapshot_id
        original_second.rename(renamed_second)
        second_manifest_path = renamed_second / "manifest.json"
        second_manifest = json.loads(second_manifest_path.read_text(encoding="utf-8"))
        second_manifest["snapshot_id"] = shared_snapshot_id
        second_manifest_path.write_text(
            json.dumps(second_manifest, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        manifest_sha256 = hashlib.sha256(second_manifest_path.read_bytes()).hexdigest()
        with snapshot_catalog_lock(second_path):
            catalog = load_snapshot_catalog(second_path)
            catalog["snapshots"][0]["snapshot_id"] = shared_snapshot_id
            catalog["snapshots"][0]["manifest_sha256"] = manifest_sha256
            catalog["current_snapshot_id"] = shared_snapshot_id
            catalog["last_successful_snapshot_id"] = shared_snapshot_id
            write_snapshot_catalog(second_path, catalog)
        self.assertTrue(second_library.verify(shared_snapshot_id)["verified"])

        first_extra = first_sources / "first-extra.md"
        second_extra = second_sources / "second-extra.md"
        _write_markdown(first_extra, "firstextra")
        _write_markdown(second_extra, "secondextra")
        first_next = first_library.build(
            [first_extra], base_snapshot_id=shared_snapshot_id
        )
        second_next = second_library.build(
            [second_extra], base_snapshot_id=shared_snapshot_id
        )

        service = ReadOnlyEvidenceTools(self.application_root)
        before = _tree_identity(self.application_root)
        alpha_result = service.search_documents(
            first_id, shared_snapshot_id, "alphaonlymarker"
        )
        self.assertTrue(alpha_result["found"])
        self.assertFalse(
            service.search_documents(
                second_id, shared_snapshot_id, "alphaonlymarker"
            )["found"]
        )
        self.assertTrue(
            service.search_documents(
                second_id, shared_snapshot_id, "betaonlymarker"
            )["found"]
        )
        with self.assertRaises(SnapshotError):
            service.search_documents(
                second_id, first_next["snapshot_id"], "firstextra"
            )

        alpha_document = alpha_result["results"][0]
        with self.assertRaises(SearchInputError):
            service.get_excerpt(
                second_id,
                shared_snapshot_id,
                alpha_document["document_id"],
                alpha_document["chunk_id"],
            )
        with self.assertRaises(SearchInputError):
            service.get_multiple_excerpts(
                second_id,
                shared_snapshot_id,
                alpha_document["document_id"],
                [alpha_document["chunk_id"]],
            )
        with self.assertRaises(SearchInputError):
            service.get_document_metadata(
                second_id, shared_snapshot_id, alpha_document["document_id"]
            )
        with self.assertRaises(SearchInputError):
            service.get_document_toc(
                second_id, shared_snapshot_id, alpha_document["document_id"]
            )
        with self.assertRaises(SearchInputError):
            service.find_in_document(
                second_id,
                shared_snapshot_id,
                alpha_document["document_id"],
                "alphaonlymarker",
            )

        first_common = service.search_documents(
            first_id, shared_snapshot_id, "commonshared"
        )["results"][0]
        second_common = service.search_documents(
            second_id, shared_snapshot_id, "commonshared"
        )["results"][0]
        self.assertEqual(first_common["document_id"], second_common["document_id"])
        first_toc = service.get_document_toc(
            first_id, shared_snapshot_id, first_common["document_id"]
        )
        second_toc = service.get_document_toc(
            second_id, shared_snapshot_id, second_common["document_id"]
        )
        self.assertNotEqual(
            first_toc["items"][0]["section_id"],
            second_toc["items"][0]["section_id"],
        )
        with self.assertRaises(SearchInputError):
            service.read_document_section(
                second_id,
                shared_snapshot_id,
                second_common["document_id"],
                first_toc["items"][0]["section_id"],
            )

        discovery = service.retrieval_status()
        self.assertEqual(
            {item["library_id"] for item in discovery["libraries"]},
            {first_id, second_id},
        )
        self.assertEqual(
            {
                item["snapshot_id"]
                for item in service.retrieval_status(first_id)["snapshots"]
            },
            {shared_snapshot_id, first_next["snapshot_id"]},
        )
        self.assertEqual(
            {
                item["snapshot_id"]
                for item in service.retrieval_status(second_id)["snapshots"]
            },
            {shared_snapshot_id, second_next["snapshot_id"]},
        )
        self.assertEqual(
            service.retrieval_status(first_id, shared_snapshot_id)["library_id"],
            first_id,
        )
        self.assertEqual(before, _tree_identity(self.application_root))

    def test_cli_build_list_and_activate_are_explicit(self) -> None:
        _library_id, library_path, _library = self._create_library("CLI")
        a = self.root / "A.md"
        b = self.root / "B.md"
        _write_markdown(a, "alpha")
        _write_markdown(b, "bravo")

        def run(arguments: list[str]) -> tuple[int, dict[str, object], str]:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main(arguments)
            payload = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
            return code, payload, stderr.getvalue()

        code, first, stderr = run(
            ["build", "--library", str(library_path), str(a)]
        )
        self.assertEqual((code, stderr), (0, ""))
        code, second, stderr = run(
            [
                "build",
                "--library",
                str(library_path),
                "--base-snapshot-id",
                first["snapshot_id"],
                str(b),
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(second["current_snapshot_id"], first["snapshot_id"])

        code, activated, stderr = run(
            [
                "snapshots",
                "activate",
                "--library",
                str(library_path),
                second["snapshot_id"],
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(activated["current_snapshot_id"], second["snapshot_id"])
        code, listed, stderr = run(
            ["snapshots", "list", "--library", str(library_path)]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(listed["current_snapshot_id"], second["snapshot_id"])
        self.assertEqual(len(listed["snapshots"]), 2)


if __name__ == "__main__":
    unittest.main()
