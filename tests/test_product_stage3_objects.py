from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literature_evidence_mcp import ingest
from literature_evidence_mcp import snapshot as snapshot_module
from literature_evidence_mcp.errors import SnapshotError
from literature_evidence_mcp.library import FixedLibrary


def _write_markdown(path: Path, marker: str) -> None:
    path.write_text(f"# {path.stem}\n\n{marker} evidence.\n", encoding="utf-8")


def _snapshot_path(library_root: Path, result: dict[str, object]) -> Path:
    return library_root / "snapshots" / str(result["snapshot_id"])


def _manifest(snapshot: Path) -> dict[str, object]:
    return json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))


def _sources(snapshot: Path) -> dict[str, dict[str, object]]:
    return {item["source_name"]: item for item in _manifest(snapshot)["sources"]}


def _object_identity(record: dict[str, object]) -> tuple[str, int, str]:
    return record["sha256"], record["byte_size"], record["path"]


def _object_path(library_root: Path, record: dict[str, object]) -> Path:
    return library_root / str(record["path"])


def _object_payloads(library_root: Path) -> dict[str, str]:
    return {
        path.relative_to(library_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((library_root / "objects").glob("*/*/payload"))
    }


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ProductStageThreeObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lemcp-product-stage3-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_incremental_objects_keep_complete_frozen_snapshots(self) -> None:
        library_root = self.root / "library"
        library = FixedLibrary(library_root)
        a = self.root / "A.md"
        b = self.root / "B.md"
        c = self.root / "C.md"
        _write_markdown(a, "alphaunique")
        _write_markdown(b, "bravounique")
        _write_markdown(c, "charlieunique")

        first = library.build([a, b])
        first_path = _snapshot_path(library_root, first)
        first_tree = _tree_hashes(first_path)
        first_objects = _object_payloads(library_root)
        first_sources = _sources(first_path)
        first_alpha = library.search(first["snapshot_id"], "alphaunique")

        second = library.build([c], base_snapshot_id=first["snapshot_id"])
        second_path = _snapshot_path(library_root, second)
        second_sources = _sources(second_path)
        second_objects = _object_payloads(library_root)

        self.assertEqual(set(second_sources), {"A.md", "B.md", "C.md"})
        for source_name in ("A.md", "B.md"):
            for kind in ("source", "parsed", "chunks"):
                self.assertEqual(
                    _object_identity(first_sources[source_name]["objects"][kind]),
                    _object_identity(second_sources[source_name]["objects"][kind]),
                )
                self.assertFalse(
                    second_sources[source_name]["objects"][kind][
                        "created_in_snapshot"
                    ]
                )
        new_c_paths = {
            record["path"] for record in second_sources["C.md"]["objects"].values()
        }
        self.assertEqual(set(second_objects) - set(first_objects), new_c_paths)
        self.assertEqual(second["storage"]["object_references"], 9)
        self.assertEqual(second["storage"]["new_objects"], 3)
        self.assertEqual(second["storage"]["reused_objects"], 6)
        self.assertEqual(
            second["storage"]["logical_object_bytes"],
            second["storage"]["new_object_bytes"]
            + second["storage"]["reused_object_bytes"],
        )
        self.assertEqual(first_tree, _tree_hashes(first_path))
        self.assertEqual(first_alpha, library.search(first["snapshot_id"], "alphaunique"))
        self.assertNotEqual(
            (first_path / "evidence.sqlite").resolve(),
            (second_path / "evidence.sqlite").resolve(),
        )
        self.assertEqual((first_path / "evidence.sqlite").stat().st_nlink, 1)
        self.assertEqual((second_path / "evidence.sqlite").stat().st_nlink, 1)

        second_tree = _tree_hashes(second_path)
        b_document_id = second_sources["B.md"]["document_id"]
        _write_markdown(b, "bravoreplacementunique")
        third = library.build(
            [],
            base_snapshot_id=second["snapshot_id"],
            replacements={b_document_id: b},
        )
        third_path = _snapshot_path(library_root, third)
        third_sources = _sources(third_path)
        for source_name in ("A.md", "C.md"):
            for kind in ("source", "parsed", "chunks"):
                self.assertEqual(
                    _object_identity(second_sources[source_name]["objects"][kind]),
                    _object_identity(third_sources[source_name]["objects"][kind]),
                )
        for kind in ("source", "parsed", "chunks"):
            self.assertNotEqual(
                _object_identity(second_sources["B.md"]["objects"][kind]),
                _object_identity(third_sources["B.md"]["objects"][kind]),
            )
        self.assertEqual(third["storage"]["new_objects"], 3)
        self.assertEqual(third["storage"]["reused_objects"], 6)
        self.assertEqual(first_tree, _tree_hashes(first_path))
        self.assertEqual(second_tree, _tree_hashes(second_path))
        self.assertTrue(
            library.search(second["snapshot_id"], "bravounique")["found"]
        )
        self.assertFalse(
            library.search(second["snapshot_id"], "bravoreplacementunique")[
                "found"
            ]
        )
        self.assertTrue(
            library.search(third["snapshot_id"], "bravoreplacementunique")["found"]
        )

    def test_parser_and_chunker_identity_invalidate_only_downstream_layers(self) -> None:
        library_root = self.root / "config-library"
        library = FixedLibrary(library_root)
        source = self.root / "config.md"
        _write_markdown(source, "configurationidentity")
        first = library.build([source])
        first_source = _sources(_snapshot_path(library_root, first))["config.md"]

        with mock.patch.object(ingest, "CHUNKER_VERSION", 2):
            chunker_version = library.build(
                [], base_snapshot_id=first["snapshot_id"]
            )
        version_source = _sources(_snapshot_path(library_root, chunker_version))[
            "config.md"
        ]
        self.assertEqual(
            _object_identity(first_source["objects"]["source"]),
            _object_identity(version_source["objects"]["source"]),
        )
        self.assertEqual(
            _object_identity(first_source["objects"]["parsed"]),
            _object_identity(version_source["objects"]["parsed"]),
        )
        self.assertNotEqual(
            _object_identity(first_source["objects"]["chunks"]),
            _object_identity(version_source["objects"]["chunks"]),
        )
        self.assertEqual(chunker_version["storage"]["new_objects"], 1)

        with mock.patch.object(ingest, "MAX_CHUNK_CHARS", 1000):
            chunker_config = library.build(
                [], base_snapshot_id=first["snapshot_id"]
            )
        config_source = _sources(_snapshot_path(library_root, chunker_config))[
            "config.md"
        ]
        self.assertEqual(
            _object_identity(first_source["objects"]["parsed"]),
            _object_identity(config_source["objects"]["parsed"]),
        )
        self.assertNotEqual(
            _object_identity(first_source["objects"]["chunks"]),
            _object_identity(config_source["objects"]["chunks"]),
        )
        self.assertEqual(chunker_config["storage"]["new_objects"], 1)

        with mock.patch.object(ingest, "MARKDOWN_PARSER_VERSION", 2):
            parser_version = library.build(
                [], base_snapshot_id=first["snapshot_id"]
            )
        parser_source = _sources(_snapshot_path(library_root, parser_version))[
            "config.md"
        ]
        self.assertEqual(
            _object_identity(first_source["objects"]["source"]),
            _object_identity(parser_source["objects"]["source"]),
        )
        self.assertNotEqual(
            _object_identity(first_source["objects"]["parsed"]),
            _object_identity(parser_source["objects"]["parsed"]),
        )
        self.assertNotEqual(
            _object_identity(first_source["objects"]["chunks"]),
            _object_identity(parser_source["objects"]["chunks"]),
        )
        self.assertEqual(parser_version["storage"]["new_objects"], 2)
        for result in (first, chunker_version, chunker_config, parser_version):
            self.assertTrue(library.verify(result["snapshot_id"])["verified"])

    def test_identical_content_is_never_shared_across_libraries(self) -> None:
        first_root = self.root / "first-library"
        second_root = self.root / "second-library"
        first_input = self.root / "first" / "same.md"
        second_input = self.root / "second" / "same.md"
        first_input.parent.mkdir()
        second_input.parent.mkdir()
        payload = "# Same\n\nidentical cross library evidence\n"
        first_input.write_text(payload, encoding="utf-8")
        second_input.write_text(payload, encoding="utf-8")

        first = FixedLibrary(first_root).build([first_input])
        second = FixedLibrary(second_root).build([second_input])
        first_source = _sources(_snapshot_path(first_root, first))["same.md"]
        second_source = _sources(_snapshot_path(second_root, second))["same.md"]
        self.assertEqual(first["storage"]["new_objects"], 3)
        self.assertEqual(second["storage"]["new_objects"], 3)
        for kind in ("source", "parsed", "chunks"):
            first_record = first_source["objects"][kind]
            second_record = second_source["objects"][kind]
            self.assertEqual(
                _object_identity(first_record), _object_identity(second_record)
            )
            first_path = _object_path(first_root, first_record)
            second_path = _object_path(second_root, second_record)
            self.assertFalse(first_path.samefile(second_path))
            self.assertEqual(first_path.stat().st_nlink, 1)
            self.assertEqual(second_path.stat().st_nlink, 1)
            self.assertTrue(first_path.resolve().is_relative_to(first_root.resolve()))
            self.assertTrue(second_path.resolve().is_relative_to(second_root.resolve()))

    def test_missing_or_tampered_objects_fail_closed_without_repair(self) -> None:
        for kind in ("source", "parsed", "chunks"):
            for damage in ("missing", "tampered"):
                with self.subTest(kind=kind, damage=damage):
                    library_root = self.root / f"{kind}-{damage}"
                    library = FixedLibrary(library_root)
                    source = self.root / f"{kind}-{damage}.md"
                    _write_markdown(source, f"{kind}{damage}unique")
                    built = library.build([source])
                    snapshot = _snapshot_path(library_root, built)
                    record = _sources(snapshot)[source.name]["objects"][kind]
                    target = _object_path(library_root, record)
                    before_catalog = (library_root / "snapshot-catalog.json").read_bytes()
                    before_manifest = (snapshot / "manifest.json").read_bytes()
                    if damage == "missing":
                        target.unlink()
                    else:
                        target.write_bytes(target.read_bytes() + b"tampered")

                    for action in (
                        lambda: library.verify(built["snapshot_id"]),
                        lambda: library.search(
                            built["snapshot_id"], f"{kind}{damage}unique"
                        ),
                        lambda: library.build(
                            [], base_snapshot_id=built["snapshot_id"]
                        ),
                        lambda: library.build([source]),
                    ):
                        with self.assertRaises(SnapshotError):
                            action()
                    self.assertEqual(
                        before_catalog,
                        (library_root / "snapshot-catalog.json").read_bytes(),
                    )
                    self.assertEqual(
                        before_manifest,
                        (snapshot / "manifest.json").read_bytes(),
                    )
                    if damage == "missing":
                        self.assertFalse(target.exists())
                    else:
                        self.assertTrue(target.read_bytes().endswith(b"tampered"))

    def test_catalog_failure_does_not_publish_a_successful_snapshot(self) -> None:
        library_root = self.root / "atomic-library"
        library = FixedLibrary(library_root)
        first_source = self.root / "first.md"
        second_source = self.root / "second.md"
        _write_markdown(first_source, "firstatomic")
        _write_markdown(second_source, "secondatomic")
        first = library.build([first_source])
        before_catalog = (library_root / "snapshot-catalog.json").read_bytes()
        before_snapshots = _tree_hashes(library_root / "snapshots")

        with mock.patch.object(
            snapshot_module,
            "write_snapshot_catalog",
            side_effect=SnapshotError("forced catalog failure"),
        ):
            with self.assertRaisesRegex(SnapshotError, "forced catalog failure"):
                library.build([second_source])

        self.assertEqual(
            before_catalog, (library_root / "snapshot-catalog.json").read_bytes()
        )
        self.assertEqual(before_snapshots, _tree_hashes(library_root / "snapshots"))
        self.assertTrue(library.verify(first["snapshot_id"])["verified"])
        self.assertFalse(
            any(path.name.startswith(".building-") for path in library_root.rglob("*"))
        )
        for payload in (library_root / "objects").glob("*/*/payload"):
            self.assertEqual(payload.parent.name, hashlib.sha256(payload.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
