from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from literature_evidence_mcp import build_snapshot, search_snapshot, verify_snapshot
from literature_evidence_mcp import snapshot as snapshot_module
from literature_evidence_mcp.cli import main as cli_main
from literature_evidence_mcp.errors import ImportPolicyError, SnapshotError


def _pdf_bytes(pages: list[str]) -> bytes:
    """Create a tiny text-layer PDF without a test-only PDF dependency."""
    objects: dict[int, bytes] = {}
    page_object_numbers = [4 + index * 2 for index in range(len(pages))]
    content_object_numbers = [number + 1 for number in page_object_numbers]
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for page_number, (page_object, content_object, text) in enumerate(
        zip(page_object_numbers, content_object_numbers, pages), 1
    ):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects[content_object] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"\nendstream"
        )
        objects[page_object] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> "
            + f"/Contents {content_object} 0 R >>".encode()
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number in range(1, max(objects) + 1):
        offsets.append(len(output))
        output.extend(f"{object_number} 0 obj\n".encode())
        output.extend(objects[object_number])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(output)


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _object_path(snapshot: Path, source: dict[str, object], kind: str) -> Path:
    record = source["objects"][kind]
    return snapshot.parent.parent / record["path"]


def _copy_snapshot_fixture(baseline: Path, case_root: Path) -> Path:
    library = case_root / "library"
    snapshots = library / "snapshots"
    snapshots.mkdir(parents=True)
    shutil.copytree(baseline.parent.parent / "objects", library / "objects")
    snapshot = snapshots / baseline.name
    shutil.copytree(baseline, snapshot)
    return snapshot


def _refresh_database_manifest(snapshot: Path) -> None:
    database_path = snapshot / "evidence.sqlite"
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = database_path.read_bytes()
    manifest["database"]["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest["database"]["byte_size"] = len(payload)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _mutate_database(
    snapshot: Path,
    statement: str,
    parameters: tuple[object, ...],
    *,
    rebuild_fts: bool = False,
) -> None:
    database = sqlite3.connect(snapshot / "evidence.sqlite")
    extracted_char_counts: dict[str, int] | None = None
    try:
        database.execute(statement, parameters)
        if rebuild_fts:
            database.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')")
            database.execute(
                """UPDATE asset SET extracted_char_count=(
                    SELECT coalesce(sum(length(chunk_text)),0) FROM chunk
                    WHERE chunk.asset_id=asset.asset_id
                )"""
            )
            extracted_char_counts = dict(
                database.execute(
                    "SELECT asset_id,extracted_char_count FROM asset"
                ).fetchall()
            )
        database.commit()
    finally:
        database.close()
    if extracted_char_counts is not None:
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for source in manifest["sources"]:
            source["extracted_char_count"] = extracted_char_counts[source["asset_id"]]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    _refresh_database_manifest(snapshot)


def _remove_all_chunks_from_one_asset(snapshot: Path) -> None:
    database = sqlite3.connect(snapshot / "evidence.sqlite")
    try:
        asset_id = database.execute(
            "SELECT asset_id FROM chunk WHERE source_line_start IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        database.execute("DELETE FROM chunk WHERE asset_id=?", (asset_id,))
        database.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')")
        database.commit()
        counts = {
            "documents": database.execute("SELECT count(*) FROM document").fetchone()[0],
            "assets": database.execute("SELECT count(*) FROM asset").fetchone()[0],
            "chunks": database.execute("SELECT count(*) FROM chunk").fetchone()[0],
            "fts_rows": database.execute("SELECT count(*) FROM chunk_fts").fetchone()[0],
        }
        chunk_counts = dict(
            database.execute(
                "SELECT asset_id,count(*) FROM chunk GROUP BY asset_id"
            ).fetchall()
        )
    finally:
        database.close()

    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"] = counts
    for source in manifest["sources"]:
        source["chunk_count"] = chunk_counts.get(source["asset_id"], 0)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _refresh_database_manifest(snapshot)


class StageOneSnapshotTests(unittest.TestCase):
    def test_supported_runtime_has_sqlite_image_api(self) -> None:
        database = sqlite3.connect(":memory:")
        try:
            self.assertTrue(hasattr(database, "serialize"))
            self.assertTrue(hasattr(database, "deserialize"))
        finally:
            database.close()

    def test_markdown_and_pdf_build_verify_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            markdown = root / "audit-note.md"
            markdown.write_text(
                "# Audit Note\n\n## Entropy\n\nShannon entropy measures uncertainty in a probability distribution.\n",
                encoding="utf-8",
            )
            pdf = root / "power-evidence.pdf"
            pdf.write_bytes(
                _pdf_bytes(
                    [
                        "First page discusses voltage quality and measurement.",
                        "Second page explains harmonic distortion evidence.",
                    ]
                )
            )

            built = build_snapshot(root / "library", [markdown, pdf])
            snapshot = Path(built["snapshot_path"])
            status = verify_snapshot(snapshot)
            before_readonly_actions = _tree_hashes(snapshot)

            self.assertTrue(status["verified"])
            self.assertEqual(status["counts"]["documents"], 2)
            self.assertEqual(status["counts"]["assets"], 2)
            self.assertEqual(status["counts"]["chunks"], status["counts"]["fts_rows"])

            markdown_result = search_snapshot(snapshot, "Shannon entropy")
            self.assertTrue(markdown_result["found"])
            markdown_hit = markdown_result["results"][0]
            self.assertEqual(markdown_hit["heading_path"], ["Audit Note", "Entropy"])
            self.assertIsNotNone(markdown_hit["source_line_start"])
            self.assertIn("Markdown lines", markdown_hit["anchor_label"])
            self.assertFalse(markdown_hit["fulltext_verified"])
            self.assertFalse(markdown_hit["formula_verified"])

            natural_question = search_snapshot(snapshot, "What is Shannon entropy?")
            self.assertTrue(natural_question["found"])
            self.assertEqual(
                natural_question["results"][0]["document_id"],
                markdown_hit["document_id"],
            )

            pdf_result = search_snapshot(snapshot, "harmonic distortion")
            self.assertTrue(pdf_result["found"])
            pdf_hit = pdf_result["results"][0]
            self.assertEqual(pdf_hit["pdf_page_start"], 2)
            self.assertEqual(pdf_hit["pdf_page_end"], 2)
            self.assertEqual(pdf_hit["anchor_label"], "PDF page 2")

            missing = search_snapshot(snapshot, "termthatcannotpossiblyexist")
            self.assertFalse(missing["found"])
            self.assertEqual(missing["results"], [])
            self.assertIn("不代表", missing["message"])
            for suffix in ("-journal", "-wal", "-shm"):
                self.assertFalse(Path(str(snapshot / "evidence.sqlite") + suffix).exists())
            self.assertEqual(before_readonly_actions, _tree_hashes(snapshot))

    def test_every_build_is_new_but_database_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "stable.md"
            source.write_text("# Stable\n\nrepeatable corpus evidence\n", encoding="utf-8")
            library = root / "library"

            first = build_snapshot(library, [source])
            first_path = Path(first["snapshot_path"])
            before = _tree_hashes(first_path)
            second = build_snapshot(library, [source])

            self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
            self.assertNotEqual(first["snapshot_path"], second["snapshot_path"])
            self.assertEqual(first["corpus_sha256"], second["corpus_sha256"])
            self.assertEqual(first["database_sha256"], second["database_sha256"])
            self.assertEqual(before, _tree_hashes(first_path))

    def test_manifest_records_real_hashes_without_external_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "private-location-name.md"
            source.write_text("portable snapshot text without a heading\n", encoding="utf-8")
            built = build_snapshot(root / "library", [source])
            snapshot = Path(built["snapshot_path"])
            manifest_bytes = (snapshot / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)

            self.assertNotIn(str(root), manifest_bytes.decode("utf-8"))
            self.assertEqual(manifest["schema_version"], 2)
            self.assertRegex(manifest["schema"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                manifest["database"]["sha256"],
                hashlib.sha256((snapshot / "evidence.sqlite").read_bytes()).hexdigest(),
            )
            frozen = _object_path(snapshot, manifest["sources"][0], "source")
            self.assertEqual(
                manifest["sources"][0]["source_sha256"],
                hashlib.sha256(frozen.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                manifest["sources"][0]["fulltext_verification"], "unverified"
            )
            self.assertEqual(
                manifest["sources"][0]["formula_verification"], "unverified"
            )
            self.assertEqual(manifest["sources"][0]["title"], "private-location-name")

    def test_tampered_frozen_source_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.md"
            source.write_text("# Evidence\n\noriginal content\n", encoding="utf-8")
            built = build_snapshot(root / "library", [source])
            snapshot = Path(built["snapshot_path"])
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            frozen = _object_path(snapshot, manifest["sources"][0], "source")
            frozen.write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(SnapshotError, "SHA-256"):
                verify_snapshot(snapshot)

    def test_semantically_malformed_database_is_rejected_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            markdown = root / "anchors.md"
            markdown.write_text(
                "# Anchors\n\nsemantic validation evidence\n", encoding="utf-8"
            )
            pdf = root / "pages.pdf"
            pdf.write_bytes(_pdf_bytes(["PDF page anchor validation evidence."]))
            built = build_snapshot(root / "library", [markdown, pdf])
            baseline = Path(built["snapshot_path"])
            deeply_nested_json = "[" * 20000 + "]" * 20000
            with self.assertRaises(RecursionError):
                json.loads(deeply_nested_json)
            database = sqlite3.connect(baseline / "evidence.sqlite")
            try:
                document_id, ordinal, original_chunk_text, anchor_label = database.execute(
                    """SELECT document_id,ordinal,chunk_text,anchor_label FROM chunk
                    WHERE source_line_start IS NOT NULL"""
                ).fetchone()
            finally:
                database.close()
            wrong_anchor = "wrong anchor"
            empty_chunk_text = ""
            oversized_chunk_text = "x" * (snapshot_module.MAX_CHUNK_CHARS + 1)

            cases = [
                (
                    "identifiers-invalid-json",
                    "UPDATE document SET identifiers=?",
                    ("{",),
                    "identifiers",
                ),
                (
                    "identifiers-wrong-shape",
                    "UPDATE document SET identifiers=?",
                    ("[]",),
                    "identifiers",
                ),
                (
                    "identifiers-too-deep",
                    "UPDATE document SET identifiers=?",
                    (deeply_nested_json,),
                    "identifiers",
                ),
                (
                    "topics-wrong-shape",
                    "UPDATE document SET topics=?",
                    ('["valid", 1]',),
                    "topics",
                ),
                (
                    "source-type",
                    "UPDATE document SET source_type=?",
                    ("remote",),
                    "source_type",
                ),
                (
                    "material-type",
                    "UPDATE document SET material_type=?",
                    ("other",),
                    "material_type",
                ),
                (
                    "heading-path-wrong-shape",
                    "UPDATE chunk SET heading_path=? WHERE source_line_start IS NOT NULL",
                    ('["Anchors", 1]',),
                    "heading_path",
                ),
                (
                    "markdown-line-range",
                    "UPDATE chunk SET source_line_start=? WHERE source_line_start IS NOT NULL",
                    (0,),
                    "行号范围",
                ),
                (
                    "markdown-pdf-anchor",
                    "UPDATE chunk SET pdf_page_start=?,pdf_page_end=? "
                    "WHERE source_line_start IS NOT NULL",
                    (1, 1),
                    "Markdown",
                ),
                (
                    "pdf-page-range",
                    "UPDATE chunk SET pdf_page_start=?,pdf_page_end=? "
                    "WHERE pdf_page_start IS NOT NULL",
                    (2, 2),
                    "页码范围",
                ),
                (
                    "anchor-label-mismatch",
                    "UPDATE chunk SET anchor_label=?,chunk_id=? "
                    "WHERE source_line_start IS NOT NULL",
                    (
                        wrong_anchor,
                        snapshot_module._chunk_id(
                            document_id, ordinal, original_chunk_text, wrong_anchor
                        ),
                    ),
                    "anchor_label",
                ),
                (
                    "empty-chunk-text",
                    "UPDATE chunk SET chunk_text=?,chunk_id=? "
                    "WHERE source_line_start IS NOT NULL",
                    (
                        empty_chunk_text,
                        snapshot_module._chunk_id(
                            document_id, ordinal, empty_chunk_text, anchor_label
                        ),
                    ),
                    "chunk_text 不得为空",
                ),
                (
                    "oversized-chunk-text",
                    "UPDATE chunk SET chunk_text=?,chunk_id=? "
                    "WHERE source_line_start IS NOT NULL",
                    (
                        oversized_chunk_text,
                        snapshot_module._chunk_id(
                            document_id, ordinal, oversized_chunk_text, anchor_label
                        ),
                    ),
                    "chunk_text 不得超过",
                ),
                (
                    "derived-chunk-id",
                    "UPDATE chunk SET chunk_id=? WHERE source_line_start IS NOT NULL",
                    ("chunk_000000000000000000000000",),
                    "chunk_id",
                ),
            ]

            for name, statement, parameters, message in cases:
                with self.subTest(name=name):
                    snapshot = _copy_snapshot_fixture(baseline, root / name)
                    rebuild_fts = name in {
                        "empty-chunk-text",
                        "oversized-chunk-text",
                    }
                    _mutate_database(
                        snapshot,
                        statement,
                        parameters,
                        rebuild_fts=rebuild_fts,
                    )

                    with self.assertRaisesRegex(SnapshotError, message):
                        verify_snapshot(snapshot)
                    if name == "identifiers-invalid-json":
                        with self.assertRaises(SnapshotError):
                            search_snapshot(snapshot, "validation evidence")

                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        exit_code = cli_main(["verify", str(snapshot)])
                    self.assertEqual(exit_code, 2)
                    self.assertIn("错误：", stderr.getvalue())
                    for leaked in (
                        "Traceback",
                        "JSONDecodeError",
                        "TypeError",
                        "RecursionError",
                        "sqlite3.",
                    ):
                        self.assertNotIn(leaked, stderr.getvalue())

            deep_manifest = _copy_snapshot_fixture(baseline, root / "deep-manifest")
            manifest_path = deep_manifest / "manifest.json"
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest_path.write_text(
                '{"deep":' + deeply_nested_json + "," + manifest_text[1:],
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SnapshotError, "manifest.json"):
                verify_snapshot(deep_manifest)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = cli_main(["verify", str(deep_manifest)])
            self.assertEqual(exit_code, 2)
            self.assertNotIn("RecursionError", stderr.getvalue())

            zero_chunks = _copy_snapshot_fixture(baseline, root / "zero-chunk-asset")
            _remove_all_chunks_from_one_asset(zero_chunks)

            with self.assertRaisesRegex(SnapshotError, "至少包含一个 chunk"):
                verify_snapshot(zero_chunks)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = cli_main(["verify", str(zero_chunks)])
            self.assertEqual(exit_code, 2)
            self.assertNotIn("Traceback", stderr.getvalue())

            corrupted = _copy_snapshot_fixture(baseline, root / "corrupted-sqlite")
            (corrupted / "evidence.sqlite").write_bytes(b"not a sqlite database")
            _refresh_database_manifest(corrupted)

            with self.assertRaises(SnapshotError):
                verify_snapshot(corrupted)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = cli_main(["verify", str(corrupted)])
            self.assertEqual(exit_code, 2)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("sqlite3.", stderr.getvalue())

    def test_manifest_cannot_redirect_verified_database_or_promote_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.md"
            source.write_text("# Evidence\n\nverified database target\n", encoding="utf-8")
            built = build_snapshot(root / "library", [source])
            snapshot = Path(built["snapshot_path"])
            manifest_path = snapshot / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            manifest["database"]["path"] = "other.sqlite"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SnapshotError, "database.path"):
                verify_snapshot(snapshot)

            manifest["database"]["path"] = "evidence.sqlite"
            manifest["sources"][0]["fulltext_verification"] = "verified"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SnapshotError, "核验状态"):
                verify_snapshot(snapshot)

    def test_pdf_without_text_layer_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            blank_pdf = root / "image-only.pdf"
            blank_pdf.write_bytes(_pdf_bytes([""]))
            library = root / "library"

            with self.assertRaisesRegex(ImportPolicyError, "文本层"):
                build_snapshot(library, [blank_pdf])
            self.assertFalse(library.exists())

    def test_prepublication_verification_failure_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.md"
            source.write_text("# Evidence\n\nprepublication gate\n", encoding="utf-8")
            library = root / "library"

            with mock.patch.object(
                snapshot_module,
                "_verify_snapshot",
                side_effect=SnapshotError("forced prepublication failure"),
            ):
                with self.assertRaisesRegex(SnapshotError, "prepublication"):
                    build_snapshot(library, [source])
            snapshots = library / "snapshots"
            self.assertTrue(snapshots.is_dir())
            self.assertEqual(list(snapshots.iterdir()), [])

    def test_no_replace_publish_preserves_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / ".building-source"
            target = root / "existing-snapshot"
            source.mkdir()
            target.mkdir()
            (source / "new.txt").write_text("new", encoding="utf-8")
            (target / "keep.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(SnapshotError, "已存在"):
                snapshot_module._publish_directory_no_replace(source, target)
            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")
            self.assertTrue((source / "new.txt").is_file())

    def test_invalid_markdown_and_duplicate_input_publish_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            invalid = root / "invalid.md"
            invalid.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ImportPolicyError, "UTF-8"):
                build_snapshot(root / "bad-library", [invalid])
            self.assertFalse((root / "bad-library").exists())

            valid = root / "valid.md"
            valid.write_text("# Valid\n\nunique evidence\n", encoding="utf-8")
            with self.assertRaisesRegex(ImportPolicyError, "重复"):
                build_snapshot(root / "duplicate-library", [valid, valid])
            self.assertFalse((root / "duplicate-library").exists())


if __name__ == "__main__":
    unittest.main()
