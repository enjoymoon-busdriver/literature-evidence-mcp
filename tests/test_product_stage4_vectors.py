from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from literature_evidence_mcp import vectors as vectors_module
from literature_evidence_mcp.cli import main as cli_main
from literature_evidence_mcp.errors import VectorError
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.mcp_server import TOOLS
from literature_evidence_mcp.vectors import (
    OfflineDeterministicFakeEmbedder,
    build_vectors,
    load_verified_vectors,
    offline_fake_profile,
    profile_id,
    verify_vectors,
)


def _write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _snapshot_path(library: Path, result: dict[str, object]) -> Path:
    return library / "snapshots" / str(result["snapshot_id"])


def _manifest(path: Path) -> dict[str, object]:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def _vector_manifest(library: Path, result: dict[str, object]) -> dict[str, object]:
    return json.loads((library / str(result["artifact_path"])).read_text(encoding="utf-8"))


def _tree_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _legacy_object_hashes(library: Path) -> dict[str, str]:
    return {
        path.relative_to(library).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((library / "objects").glob("*/*/payload"))
    }


def _run_cli(arguments: list[str]) -> tuple[int, dict[str, object], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(arguments)
    payload = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
    return code, payload, stderr.getvalue()


class _FixedOutputEmbedder:
    offline = True
    simulated = True
    adapter_identity = {"implementation": "test_offline_fake", "version": 1}

    def __init__(self, output: object) -> None:
        self.output = output
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, texts: list[str] | tuple[str, ...], profile: dict[str, object]) -> object:
        self.calls.append(tuple(texts))
        return self.output


class ProductStageFourVectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lemcp-product-stage4-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_s1_s2_only_submit_c_and_keep_complete_mappings(self) -> None:
        library_root = self.root / "incremental-library"
        library = FixedLibrary(library_root)
        a = self.root / "A.md"
        b = self.root / "B.md"
        c = self.root / "C.md"
        _write_markdown(a, "# A\n\nalphaunique evidence.\n")
        _write_markdown(b, "# B\n\nbravounique evidence.\n")
        _write_markdown(c, "# C\n\ncharlieunique evidence.\n")
        profile = offline_fake_profile()

        first = library.build([a, b])
        first_path = _snapshot_path(library_root, first)
        first_snapshot_before = _tree_hashes(first_path)
        first_catalog_before = (library_root / "snapshot-catalog.json").read_bytes()
        first_objects_before = _legacy_object_hashes(library_root)
        first_fake = OfflineDeterministicFakeEmbedder()
        first_vectors = build_vectors(
            library_root, first["snapshot_id"], profile, first_fake
        )

        self.assertEqual(len(first_fake.calls), 1)
        self.assertEqual(len(first_fake.calls[0]), 2)
        self.assertTrue(any("alphaunique" in text for text in first_fake.calls[0]))
        self.assertTrue(any("bravounique" in text for text in first_fake.calls[0]))
        self.assertEqual(
            first_vectors["statistics"],
            {
                "vector_references": 2,
                "unique_inputs": 2,
                "new_objects": 2,
                "reused_objects": 0,
                "referenced_vector_bytes": 128,
                "unique_vector_bytes": 128,
                "new_object_bytes": 128,
                "reused_object_bytes": 0,
                "deduplicated_vector_bytes": 0,
            },
        )
        self.assertEqual(first_snapshot_before, _tree_hashes(first_path))
        self.assertEqual(
            first_catalog_before, (library_root / "snapshot-catalog.json").read_bytes()
        )
        self.assertEqual(first_objects_before, _legacy_object_hashes(library_root))

        second = library.build([c], base_snapshot_id=first["snapshot_id"])
        second_path = _snapshot_path(library_root, second)
        snapshots_before = {
            first["snapshot_id"]: _tree_hashes(first_path),
            second["snapshot_id"]: _tree_hashes(second_path),
        }
        catalog_before = (library_root / "snapshot-catalog.json").read_bytes()
        objects_before = _legacy_object_hashes(library_root)
        second_fake = OfflineDeterministicFakeEmbedder()
        second_vectors = build_vectors(
            library_root, second["snapshot_id"], profile, second_fake
        )

        self.assertEqual(len(second_fake.calls), 1)
        self.assertEqual(len(second_fake.calls[0]), 1)
        self.assertIn("charlieunique", second_fake.calls[0][0])
        self.assertNotIn("alphaunique", second_fake.calls[0][0])
        self.assertNotIn("bravounique", second_fake.calls[0][0])
        self.assertEqual(second_vectors["statistics"]["vector_references"], 3)
        self.assertEqual(second_vectors["statistics"]["unique_inputs"], 3)
        self.assertEqual(second_vectors["statistics"]["new_objects"], 1)
        self.assertEqual(second_vectors["statistics"]["reused_objects"], 2)
        self.assertEqual(second_vectors["statistics"]["new_object_bytes"], 64)
        self.assertEqual(second_vectors["statistics"]["reused_object_bytes"], 128)
        self.assertEqual(
            len(
                load_verified_vectors(
                    library_root, first["snapshot_id"], first_vectors["profile_id"]
                )["vectors"]
            ),
            2,
        )
        self.assertEqual(
            len(
                load_verified_vectors(
                    library_root, second["snapshot_id"], second_vectors["profile_id"]
                )["vectors"]
            ),
            3,
        )
        self.assertTrue(
            verify_vectors(
                library_root, second["snapshot_id"], second_vectors["profile_id"]
            )["verified"]
        )
        self.assertEqual(snapshots_before[first["snapshot_id"]], _tree_hashes(first_path))
        self.assertEqual(snapshots_before[second["snapshot_id"]], _tree_hashes(second_path))
        self.assertEqual(catalog_before, (library_root / "snapshot-catalog.json").read_bytes())
        self.assertEqual(objects_before, _legacy_object_hashes(library_root))
        self.assertEqual((first_path / "evidence.sqlite").stat().st_nlink, 1)
        self.assertEqual((second_path / "evidence.sqlite").stat().st_nlink, 1)

    def test_changed_text_reuses_unchanged_chunk_identity(self) -> None:
        library_root = self.root / "changed-text-library"
        library = FixedLibrary(library_root)
        source = self.root / "C.md"
        _write_markdown(
            source,
            "# Stable\n\nstable exact text.\n\n## Changed\n\nold changing text.\n",
        )
        first = library.build([source])
        profile = offline_fake_profile(dimensions=4)
        first_fake = OfflineDeterministicFakeEmbedder()
        first_vectors = build_vectors(
            library_root, first["snapshot_id"], profile, first_fake
        )
        first_artifact = _vector_manifest(library_root, first_vectors)
        document_id = _manifest(_snapshot_path(library_root, first))["sources"][0][
            "document_id"
        ]

        _write_markdown(
            source,
            "# Stable\n\nstable exact text.\n\n## Changed\n\nnew changing text.\n",
        )
        second = library.build(
            [],
            base_snapshot_id=first["snapshot_id"],
            replacements={document_id: source},
        )
        second_fake = OfflineDeterministicFakeEmbedder()
        second_vectors = build_vectors(
            library_root, second["snapshot_id"], profile, second_fake
        )
        second_artifact = _vector_manifest(library_root, second_vectors)

        first_ids = {item["vector_object_id"] for item in first_artifact["objects"]}
        second_ids = {item["vector_object_id"] for item in second_artifact["objects"]}
        self.assertEqual(len(first_ids), 2)
        self.assertEqual(len(second_ids), 2)
        self.assertEqual(len(first_ids & second_ids), 1)
        self.assertEqual(second_vectors["statistics"]["new_objects"], 1)
        self.assertEqual(second_vectors["statistics"]["reused_objects"], 1)
        self.assertEqual(len(second_fake.calls), 1)
        self.assertEqual(len(second_fake.calls[0]), 1)
        self.assertIn("new changing text", second_fake.calls[0][0])

    def test_every_profile_identity_field_invalidates_reuse(self) -> None:
        library_root = self.root / "profile-library"
        source = self.root / "profile.md"
        _write_markdown(source, "# Profile\n\nprofile identity evidence.\n")
        snapshot = FixedLibrary(library_root).build([source])
        base = offline_fake_profile(dimensions=4)
        base_result = build_vectors(
            library_root,
            snapshot["snapshot_id"],
            base,
            OfflineDeterministicFakeEmbedder(),
        )
        profile_ids = {base_result["profile_id"]}
        object_ids = {
            item["vector_object_id"]
            for item in _vector_manifest(library_root, base_result)["objects"]
        }

        variants: list[dict[str, object]] = []
        for field, value in (
            ("provider", "offline-deterministic-fake-b"),
            ("model_id", "sha256-seeded-float64-b"),
            ("model_revision", "2"),
            ("dimensions", 5),
            ("input_role", "passage"),
            ("instruction", "A different complete offline instruction."),
        ):
            variant = copy.deepcopy(base)
            variant[field] = value
            variants.append(variant)
        implementation = copy.deepcopy(base)
        implementation["preprocessing"]["implementation"] = "exact-text-noop-b"
        variants.append(implementation)
        version = copy.deepcopy(base)
        version["preprocessing"]["version"] = 2
        variants.append(version)
        config = copy.deepcopy(base)
        config["preprocessing"]["config"]["test_variant"] = "different"
        variants.append(config)

        for variant in variants:
            fake = OfflineDeterministicFakeEmbedder()
            result = build_vectors(
                library_root, snapshot["snapshot_id"], variant, fake
            )
            self.assertEqual(result["statistics"]["new_objects"], 1)
            self.assertEqual(result["statistics"]["reused_objects"], 0)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(len(fake.calls[0]), 1)
            self.assertNotIn(result["profile_id"], profile_ids)
            profile_ids.add(result["profile_id"])
            variant_object_ids = {
                item["vector_object_id"]
                for item in _vector_manifest(library_root, result)["objects"]
            }
            self.assertTrue(object_ids.isdisjoint(variant_object_ids))
            object_ids.update(variant_object_ids)
        self.assertEqual(len(profile_ids), 10)
        self.assertEqual(len(object_ids), 10)

        invalid_profiles = []
        for revision in ("", "latest"):
            invalid = copy.deepcopy(base)
            invalid["model_revision"] = revision
            invalid_profiles.append(invalid)
        extra_credential = {**copy.deepcopy(base), "api_key": "not-allowed"}
        invalid_profiles.append(extra_credential)
        nested_credential = copy.deepcopy(base)
        nested_credential["preprocessing"]["config"]["access_token"] = "not-allowed"
        invalid_profiles.append(nested_credential)
        real_provider_label = copy.deepcopy(base)
        real_provider_label["provider"] = "real-provider-name"
        invalid_profiles.append(real_provider_label)
        for invalid in invalid_profiles:
            fake = OfflineDeterministicFakeEmbedder()
            with self.assertRaises(VectorError):
                build_vectors(library_root, snapshot["snapshot_id"], invalid, fake)
            self.assertEqual(fake.calls, [])

    def test_duplicate_text_reuses_one_object_with_complete_mapping(self) -> None:
        library_root = self.root / "duplicate-library"
        first = self.root / "first" / "one.md"
        second = self.root / "second" / "two.md"
        payload = "# Shared\n\nidentical legal duplicate text.\n"
        _write_markdown(first, payload)
        _write_markdown(second, payload)
        snapshot = FixedLibrary(library_root).build([first, second])
        fake = OfflineDeterministicFakeEmbedder()
        result = build_vectors(
            library_root, snapshot["snapshot_id"], offline_fake_profile(), fake
        )
        loaded = load_verified_vectors(
            library_root, snapshot["snapshot_id"], result["profile_id"]
        )

        self.assertEqual(result["statistics"]["vector_references"], 2)
        self.assertEqual(result["statistics"]["unique_inputs"], 1)
        self.assertEqual(result["statistics"]["new_objects"], 1)
        self.assertEqual(result["statistics"]["deduplicated_vector_bytes"], 64)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(fake.calls[0]), 1)
        self.assertEqual(len(loaded["vectors"]), 2)
        self.assertEqual(loaded["vectors"][0]["vector"], loaded["vectors"][1]["vector"])

    def test_identical_vectors_are_physically_isolated_by_library(self) -> None:
        profile = offline_fake_profile(dimensions=4)
        results = []
        for name in ("first", "second"):
            library_root = self.root / f"{name}-library"
            source = self.root / name / "same.md"
            _write_markdown(source, "# Same\n\nidentical cross-library evidence.\n")
            snapshot = FixedLibrary(library_root).build([source])
            fake = OfflineDeterministicFakeEmbedder()
            result = build_vectors(
                library_root, snapshot["snapshot_id"], profile, fake
            )
            self.assertEqual(len(fake.calls), 1)
            record = _vector_manifest(library_root, result)["objects"][0]
            results.append((library_root, result, record))

        first_root, first_result, first_record = results[0]
        second_root, second_result, second_record = results[1]
        self.assertEqual(first_result["profile_id"], second_result["profile_id"])
        self.assertEqual(first_record["vector_object_id"], second_record["vector_object_id"])
        self.assertEqual(first_record["path"], second_record["path"])
        first_path = first_root / first_record["path"]
        second_path = second_root / second_record["path"]
        self.assertFalse(first_path.samefile(second_path))
        self.assertEqual(first_path.stat().st_nlink, 1)
        self.assertEqual(second_path.stat().st_nlink, 1)
        self.assertTrue(first_path.resolve().is_relative_to(first_root.resolve()))
        self.assertTrue(second_path.resolve().is_relative_to(second_root.resolve()))

    def test_bad_embedder_outputs_fail_before_any_vector_write(self) -> None:
        cases = {
            "too_few": [],
            "too_many": [[0.0] * 4, [0.0] * 4],
            "short_dimension": [[0.0] * 3],
            "long_dimension": [[0.0] * 5],
            "bool": [[False, 0.0, 0.0, 0.0]],
            "nan": [[float("nan"), 0.0, 0.0, 0.0]],
            "inf": [[float("inf"), 0.0, 0.0, 0.0]],
        }
        for name, output in cases.items():
            with self.subTest(name=name):
                library_root = self.root / f"bad-{name}"
                source = self.root / f"bad-{name}.md"
                _write_markdown(source, f"# Bad\n\n{name} evidence.\n")
                snapshot = FixedLibrary(library_root).build([source])
                snapshot_path = _snapshot_path(library_root, snapshot)
                snapshot_before = _tree_hashes(snapshot_path)
                catalog_before = (library_root / "snapshot-catalog.json").read_bytes()
                fake = _FixedOutputEmbedder(output)

                with self.assertRaises(VectorError):
                    build_vectors(
                        library_root,
                        snapshot["snapshot_id"],
                        offline_fake_profile(dimensions=4),
                        fake,
                    )
                self.assertEqual(len(fake.calls), 1)
                self.assertFalse((library_root / "derived").exists())
                self.assertEqual(snapshot_before, _tree_hashes(snapshot_path))
                self.assertEqual(
                    catalog_before,
                    (library_root / "snapshot-catalog.json").read_bytes(),
                )

    def test_registered_missing_or_tampered_data_never_calls_embedder_to_repair(self) -> None:
        for damage in (
            "object_missing",
            "object_tampered",
            "artifact_missing",
            "artifact_tampered",
            "artifact_dimension_tampered",
        ):
            with self.subTest(damage=damage):
                library_root = self.root / damage
                source = self.root / f"{damage}.md"
                _write_markdown(source, f"# Damage\n\n{damage} evidence.\n")
                snapshot = FixedLibrary(library_root).build([source])
                profile = offline_fake_profile(dimensions=4)
                result = build_vectors(
                    library_root,
                    snapshot["snapshot_id"],
                    profile,
                    OfflineDeterministicFakeEmbedder(),
                )
                artifact_path = library_root / result["artifact_path"]
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                object_path = library_root / artifact["objects"][0]["path"]
                vector_catalog = library_root / "derived" / "vectors" / "catalog.json"
                catalog_before = vector_catalog.read_bytes()
                if damage == "object_missing":
                    object_path.unlink()
                elif damage == "object_tampered":
                    object_path.write_bytes(object_path.read_bytes() + b"tampered")
                elif damage == "artifact_missing":
                    shutil.rmtree(artifact_path.parent)
                elif damage == "artifact_tampered":
                    artifact_path.write_bytes(artifact_path.read_bytes() + b" ")
                else:
                    artifact["profile"]["dimensions"] = 5
                    artifact_path.write_bytes(vectors_module._canonical_json(artifact))
                damaged_state = _tree_hashes(library_root / "derived")

                with self.assertRaises(VectorError):
                    verify_vectors(
                        library_root, snapshot["snapshot_id"], result["profile_id"]
                    )
                fake = OfflineDeterministicFakeEmbedder()
                with self.assertRaises(VectorError):
                    build_vectors(
                        library_root, snapshot["snapshot_id"], profile, fake
                    )
                self.assertEqual(fake.calls, [])
                self.assertEqual(damaged_state, _tree_hashes(library_root / "derived"))
                self.assertEqual(catalog_before, vector_catalog.read_bytes())

    def test_catalog_failure_hides_artifact_and_retry_reuses_orphans(self) -> None:
        library_root = self.root / "atomic-library"
        source = self.root / "atomic.md"
        _write_markdown(source, "# Atomic\n\natomic vector evidence.\n")
        snapshot = FixedLibrary(library_root).build([source])
        snapshot_path = _snapshot_path(library_root, snapshot)
        snapshot_before = _tree_hashes(snapshot_path)
        catalog_before = (library_root / "snapshot-catalog.json").read_bytes()
        objects_before = _legacy_object_hashes(library_root)
        profile = offline_fake_profile(dimensions=4)
        selected_profile_id = profile_id(profile)
        fake = OfflineDeterministicFakeEmbedder()

        with mock.patch.object(
            vectors_module,
            "_write_vector_catalog",
            side_effect=VectorError("forced vector catalog failure"),
        ):
            with self.assertRaisesRegex(VectorError, "forced vector catalog failure"):
                build_vectors(library_root, snapshot["snapshot_id"], profile, fake)
        self.assertEqual(len(fake.calls), 1)
        artifact_directory = (
            library_root
            / "derived"
            / "vectors"
            / "artifacts"
            / snapshot["snapshot_id"]
            / selected_profile_id
        )
        self.assertFalse(artifact_directory.exists())
        self.assertFalse(
            (library_root / "derived" / "vectors" / "catalog.json").exists()
        )
        orphan_payloads = list(
            (library_root / "derived" / "vectors" / "objects").glob("*/*/payload")
        )
        self.assertEqual(len(orphan_payloads), 1)
        self.assertEqual(orphan_payloads[0].stat().st_nlink, 1)
        self.assertEqual(snapshot_before, _tree_hashes(snapshot_path))
        self.assertEqual(catalog_before, (library_root / "snapshot-catalog.json").read_bytes())
        self.assertEqual(objects_before, _legacy_object_hashes(library_root))

        retry_fake = OfflineDeterministicFakeEmbedder()
        retry = build_vectors(
            library_root, snapshot["snapshot_id"], profile, retry_fake
        )
        self.assertEqual(retry_fake.calls, [])
        self.assertEqual(retry["submitted_unique_inputs"], 0)
        self.assertEqual(retry["statistics"]["new_objects"], 0)
        self.assertEqual(retry["statistics"]["reused_objects"], 1)
        self.assertTrue(
            verify_vectors(
                library_root, snapshot["snapshot_id"], retry["profile_id"]
            )["verified"]
        )
        self.assertFalse(
            any(path.name.startswith(".building-") for path in library_root.rglob("*"))
        )

    def test_cli_is_offline_simulated_and_mcp_remains_exactly_eight(self) -> None:
        library_root = self.root / "cli-library"
        source = self.root / "cli.md"
        _write_markdown(source, "# CLI\n\nCLI offline vector evidence.\n")
        snapshot = FixedLibrary(library_root).build([source])

        code, built, stderr = _run_cli(
            [
                "build-vectors",
                "--library",
                str(library_root),
                "--snapshot-id",
                snapshot["snapshot_id"],
                "--dimensions",
                "4",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertIs(built["offline"], True)
        self.assertIs(built["simulated"], True)
        self.assertEqual(built["profile"]["provider"], "offline-deterministic-fake")
        self.assertEqual(built["profile"]["model_revision"], "1")
        self.assertEqual(built["generation"]["mode"], "offline_simulated")
        self.assertEqual(built["embedder_calls"], 1)

        code, repeated, stderr = _run_cli(
            [
                "build-vectors",
                "--library",
                str(library_root),
                "--snapshot-id",
                snapshot["snapshot_id"],
                "--dimensions",
                "4",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertIs(repeated["already_exists"], True)
        self.assertEqual(repeated["embedder_calls"], 0)

        code, verified, stderr = _run_cli(
            [
                "verify-vectors",
                "--library",
                str(library_root),
                "--snapshot-id",
                snapshot["snapshot_id"],
                "--profile-id",
                built["profile_id"],
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertIs(verified["verified"], True)
        self.assertIs(verified["offline"], True)
        self.assertIs(verified["simulated"], True)

        self.assertEqual(
            [tool.name for tool in TOOLS],
            [
                "search_documents",
                "get_excerpt",
                "get_multiple_excerpts",
                "get_document_metadata",
                "get_document_toc",
                "read_document_section",
                "find_in_document",
                "retrieval_status",
            ],
        )


if __name__ == "__main__":
    unittest.main()
