from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from literature_evidence_mcp import build_snapshot, search_snapshot
from literature_evidence_mcp.errors import SearchInputError
from literature_evidence_mcp.mcp_tools import ReadOnlyEvidenceTools
from literature_evidence_mcp.registry import LibraryRegistry
from tests.test_stage1 import _pdf_bytes


TOOL_NAMES = [
    "search_documents",
    "get_excerpt",
    "get_multiple_excerpts",
    "get_document_metadata",
    "get_document_toc",
    "read_document_section",
    "find_in_document",
    "retrieval_status",
]
EXPECTED_REQUIRED = {
    "search_documents": {"library_id", "snapshot_id", "query"},
    "get_excerpt": {"library_id", "snapshot_id", "document_id", "chunk_id"},
    "get_multiple_excerpts": {
        "library_id",
        "snapshot_id",
        "document_id",
        "chunk_ids",
    },
    "get_document_metadata": {"library_id", "snapshot_id", "document_id"},
    "get_document_toc": {"library_id", "snapshot_id", "document_id"},
    "read_document_section": {
        "library_id",
        "snapshot_id",
        "document_id",
        "section_id",
    },
    "find_in_document": {"library_id", "snapshot_id", "document_id", "query"},
    "retrieval_status": set(),
}
EXPECTED_PROPERTIES = {
    "search_documents": {
        "library_id",
        "snapshot_id",
        "query",
        "top_k",
        "excerpt_chars",
        "mode",
    },
    "get_excerpt": {
        "library_id",
        "snapshot_id",
        "document_id",
        "chunk_id",
        "max_chars",
    },
    "get_multiple_excerpts": {
        "library_id",
        "snapshot_id",
        "document_id",
        "chunk_ids",
        "per_item_chars",
    },
    "get_document_metadata": {"library_id", "snapshot_id", "document_id"},
    "get_document_toc": {
        "library_id",
        "snapshot_id",
        "document_id",
        "max_items",
    },
    "read_document_section": {
        "library_id",
        "snapshot_id",
        "document_id",
        "section_id",
        "max_chars",
    },
    "find_in_document": {
        "library_id",
        "snapshot_id",
        "document_id",
        "query",
        "top_k",
        "excerpt_chars",
    },
    "retrieval_status": {"library_id", "snapshot_id"},
}
EXPECTED_DEFAULTS = {
    "search_documents": {"top_k": 5, "excerpt_chars": 1000, "mode": "bm25"},
    "get_excerpt": {"max_chars": 600},
    "get_multiple_excerpts": {"per_item_chars": 600},
    "get_document_metadata": {},
    "get_document_toc": {"max_items": 100},
    "read_document_section": {"max_chars": 1200},
    "find_in_document": {"top_k": 5, "excerpt_chars": 600},
    "retrieval_status": {},
}
FORBIDDEN_OUTPUT_KEYS = {
    "snapshot_path",
    "stored_path",
    "database_path",
    "library_path",
    "source_sha256",
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


def _assert_no_private_keys(test: unittest.TestCase, value: Any) -> None:
    if isinstance(value, dict):
        test.assertTrue(FORBIDDEN_OUTPUT_KEYS.isdisjoint(value))
        for item in value.values():
            _assert_no_private_keys(test, item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_private_keys(test, item)


def _protocol_payload(result: Any) -> dict[str, Any]:
    if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
        raise AssertionError("tool result must contain one JSON TextContent")
    payload = json.loads(result.content[0].text)
    if not isinstance(payload, dict):
        raise AssertionError("tool result JSON must be an object")
    return payload


class _SyntheticLibraryMixin:
    def make_library(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lemcp-stage3-")
        self.root = Path(self.temporary.name)
        self.application_root = self.root / "application"
        registry = LibraryRegistry(self.application_root)
        library_record = registry.create("Protocol library")
        self.library_id = library_record["library_id"]
        self.library = Path(library_record["library_root"])
        primary = self.root / "protocol-evidence.md"
        primary.write_text(
            "# Protocol Evidence\n\n"
            "## Route Alpha\n\n"
            "calibrationmarker alpha evidence anchor.\n\n"
            "## Route Bravo\n\n"
            "calibrationmarker bravo evidence anchor.\n",
            encoding="utf-8",
        )
        other = self.root / "other-document.md"
        other.write_text(
            "# Other Document\n\nseparate material for mismatch checks.\n",
            encoding="utf-8",
        )
        foreign = self.root / "foreign-snapshot.md"
        foreign.write_text(
            "# Foreign Snapshot\n\ncontent existing only in the second snapshot.\n",
            encoding="utf-8",
        )
        pdf = self.root / "page-evidence.pdf"
        pdf.write_bytes(
            _pdf_bytes(
                [
                    "First page contains firstpageunique protocol evidence.",
                    "Second page contains secondpageunique protocol evidence.",
                ]
            )
        )
        self.built = build_snapshot(self.library, [primary, other, pdf])
        self.foreign_built = build_snapshot(self.library, [foreign])
        self.snapshot = Path(self.built["snapshot_path"])
        self.snapshot_id = self.built["snapshot_id"]
        self.foreign_snapshot_id = self.foreign_built["snapshot_id"]
        search = search_snapshot(self.snapshot, "calibrationmarker", top_k=10)
        self.document_id = search["results"][0]["document_id"]
        self.chunk_ids = [item["chunk_id"] for item in search["results"]]
        other_search = search_snapshot(self.snapshot, "mismatch checks")
        self.other_document_id = other_search["results"][0]["document_id"]
        self.other_chunk_id = other_search["results"][0]["chunk_id"]
        pdf_search = search_snapshot(self.snapshot, "secondpageunique")
        self.pdf_document_id = pdf_search["results"][0]["document_id"]
        self.pdf_chunk_id = pdf_search["results"][0]["chunk_id"]
        self.service = ReadOnlyEvidenceTools(self.application_root)
        self.section_id = self.service.get_document_toc(
            self.library_id, self.snapshot_id, self.document_id
        )["items"][0]["section_id"]

    def remove_library(self) -> None:
        self.temporary.cleanup()


class StageThreeToolTests(_SyntheticLibraryMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.make_library()

    def tearDown(self) -> None:
        self.remove_library()

    def test_all_eight_readonly_operations_preserve_traceability_and_tree(self) -> None:
        before = _tree_identity(self.application_root)
        search = self.service.search_documents(
            self.library_id,
            self.snapshot_id,
            "calibrationmarker",
            top_k=10,
            excerpt_chars=1200,
        )
        excerpt = self.service.get_excerpt(
            self.library_id,
            self.snapshot_id,
            self.document_id,
            self.chunk_ids[0],
            max_chars=1200,
        )
        multiple = self.service.get_multiple_excerpts(
            self.library_id,
            self.snapshot_id,
            self.document_id,
            list(reversed(self.chunk_ids[:2])),
            per_item_chars=1200,
        )
        metadata = self.service.get_document_metadata(
            self.library_id, self.snapshot_id, self.document_id
        )
        toc = self.service.get_document_toc(
            self.library_id, self.snapshot_id, self.document_id
        )
        section = self.service.read_document_section(
            self.library_id,
            self.snapshot_id,
            self.document_id,
            self.section_id,
            max_chars=1200,
        )
        found = self.service.find_in_document(
            self.library_id,
            self.snapshot_id,
            self.document_id,
            "calibrationmarker",
            top_k=10,
            excerpt_chars=1200,
        )
        status = self.service.retrieval_status(self.library_id, self.snapshot_id)
        pdf_metadata = self.service.get_document_metadata(
            self.library_id, self.snapshot_id, self.pdf_document_id
        )
        pdf_toc = self.service.get_document_toc(
            self.library_id, self.snapshot_id, self.pdf_document_id
        )
        pdf_excerpt = self.service.get_excerpt(
            self.library_id,
            self.snapshot_id,
            self.pdf_document_id,
            self.pdf_chunk_id,
        )
        pdf_section = self.service.read_document_section(
            self.library_id,
            self.snapshot_id,
            self.pdf_document_id,
            pdf_toc["items"][1]["section_id"],
        )

        self.assertEqual(before, _tree_identity(self.application_root))
        self.assertEqual(search, self.service.search_documents(
            self.library_id,
            self.snapshot_id,
            "calibrationmarker",
            top_k=10,
            excerpt_chars=1200,
        ))
        for item in search["results"]:
            self.assertTrue(
                {
                    "document_id",
                    "asset_id",
                    "chunk_id",
                    "title",
                    "source_name",
                    "heading_path",
                    "anchor_label",
                    "fulltext_verified",
                    "formula_verified",
                    "excerpt",
                }.issubset(item)
            )
            self.assertLessEqual(len(item["excerpt"]), 1200)
        self.assertEqual(excerpt["result"]["chunk_id"], self.chunk_ids[0])
        self.assertEqual(
            [item["chunk_id"] for item in multiple["results"]],
            list(reversed(self.chunk_ids[:2])),
        )
        self.assertTrue(all("truncated" in item for item in multiple["results"]))
        self.assertEqual(metadata["document"]["document_id"], self.document_id)
        self.assertTrue(metadata["document"]["assets"][0]["asset_id"].startswith("asset_"))
        self.assertTrue(toc["items"])
        self.assertEqual(section["section"]["section_id"], self.section_id)
        self.assertLessEqual(
            sum(len(item["excerpt"]) for item in section["results"]), 1200
        )
        self.assertEqual(found["retrieval_mode"], "bm25")
        self.assertTrue(status["closed_world"])
        self.assertTrue(status["readonly"])
        self.assertEqual(status["transport"], "stdio")
        self.assertEqual(status["library_id"], self.library_id)
        self.assertEqual(pdf_metadata["document"]["assets"][0]["page_count"], 2)
        self.assertEqual(
            [item["section_kind"] for item in pdf_toc["items"]],
            ["pdf_page", "pdf_page"],
        )
        self.assertEqual(
            [item["pdf_page_start"] for item in pdf_toc["items"]], [1, 2]
        )
        self.assertEqual(pdf_excerpt["result"]["pdf_page_start"], 2)
        self.assertEqual(pdf_excerpt["result"]["anchor_label"], "PDF page 2")
        self.assertEqual(pdf_section["section"]["pdf_page_start"], 2)
        self.assertEqual(pdf_section["results"][0]["anchor_label"], "PDF page 2")
        for payload in (
            search,
            excerpt,
            multiple,
            metadata,
            toc,
            section,
            found,
            status,
            pdf_metadata,
            pdf_toc,
            pdf_excerpt,
            pdf_section,
        ):
            _assert_no_private_keys(self, payload)
            self.assertNotIn(str(self.root), json.dumps(payload, ensure_ascii=False))

    def test_empty_results_boundaries_mismatches_and_deterministic_order(self) -> None:
        for result in (
            self.service.search_documents(
                self.library_id, self.snapshot_id, "termabsentfromsnapshot"
            ),
            self.service.find_in_document(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                "termabsentfromsnapshot",
            ),
        ):
            self.assertFalse(result["found"])
            self.assertEqual(result["results"], [])

        first = self.service.find_in_document(
            self.library_id,
            self.snapshot_id,
            self.document_id,
            "calibrationmarker",
            top_k=10,
        )
        second = self.service.find_in_document(
            self.library_id,
            self.snapshot_id,
            self.document_id,
            "calibrationmarker",
            top_k=10,
        )
        self.assertEqual(first, second)
        scores_and_ids = [
            (item["score"], item["chunk_id"]) for item in first["results"]
        ]
        self.assertEqual(scores_and_ids, sorted(scores_and_ids))
        self.assertEqual(len(scores_and_ids), 2)
        self.assertEqual(scores_and_ids[0][0], scores_and_ids[1][0])
        self.assertLess(scores_and_ids[0][1], scores_and_ids[1][1])

        invalid_calls = (
            lambda: self.service.search_documents(
                self.library_id, self.snapshot_id, "x" * 401
            ),
            lambda: self.service.search_documents(
                self.library_id, self.snapshot_id, "x" + " " * 400
            ),
            lambda: self.service.search_documents(
                self.library_id, self.snapshot_id, 1
            ),
            lambda: self.service.search_documents(
                self.library_id, self.snapshot_id, "evidence", top_k=0
            ),
            lambda: self.service.search_documents(
                self.library_id, self.snapshot_id, "evidence", top_k=11
            ),
            lambda: self.service.search_documents(
                self.library_id,
                self.snapshot_id,
                "evidence",
                excerpt_chars=1201,
            ),
            lambda: self.service.search_documents(
                self.library_id, self.snapshot_id, "evidence", top_k=True
            ),
            lambda: self.service.get_excerpt(
                self.library_id,
                self.snapshot_id,
                self.other_document_id,
                self.chunk_ids[0],
            ),
            lambda: self.service.get_excerpt(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                self.chunk_ids[0],
                max_chars=0,
            ),
            lambda: self.service.get_excerpt(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                "chunk_" + "0" * 25,
            ),
            lambda: self.service.get_multiple_excerpts(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                [self.chunk_ids[0], self.chunk_ids[0]],
            ),
            lambda: self.service.get_multiple_excerpts(
                self.library_id, self.snapshot_id, self.document_id, []
            ),
            lambda: self.service.get_multiple_excerpts(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                ["chunk_" + f"{index:024x}" for index in range(6)],
            ),
            lambda: self.service.get_multiple_excerpts(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                [self.chunk_ids[0], self.other_chunk_id],
            ),
            lambda: self.service.get_document_metadata(
                self.library_id, self.foreign_snapshot_id, self.document_id
            ),
            lambda: self.service.get_document_metadata(
                self.library_id, self.snapshot_id, "doc_" + "0" * 25
            ),
            lambda: self.service.get_document_toc(
                self.library_id, self.snapshot_id, self.document_id, max_items=0
            ),
            lambda: self.service.get_document_toc(
                self.library_id, self.snapshot_id, self.document_id, max_items=101
            ),
            lambda: self.service.read_document_section(
                self.library_id,
                self.snapshot_id,
                self.other_document_id,
                self.section_id,
            ),
            lambda: self.service.read_document_section(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                self.section_id,
                max_chars=1201,
            ),
            lambda: self.service.read_document_section(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                "sec_" + "0" * 25,
            ),
            lambda: self.service.find_in_document(
                self.library_id,
                self.snapshot_id,
                "doc_" + "0" * 24,
                "evidence",
            ),
            lambda: self.service.find_in_document(
                self.library_id, self.snapshot_id, self.document_id, 1
            ),
            lambda: self.service.find_in_document(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                "evidence",
                top_k=0,
            ),
            lambda: self.service.find_in_document(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                "evidence",
                top_k=11,
            ),
            lambda: self.service.find_in_document(
                self.library_id,
                self.snapshot_id,
                self.document_id,
                "evidence",
                excerpt_chars=1201,
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(SearchInputError):
                call()


class StageThreeProtocolTests(_SyntheticLibraryMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.make_library()

    def tearDown(self) -> None:
        self.remove_library()

    async def test_installed_stdio_protocol_exact_tools_calls_errors_and_no_writes(
        self,
    ) -> None:
        command = Path(sys.executable).with_name("literature-evidence-mcp")
        self.assertTrue(command.is_file(), command)
        before = _tree_identity(self.application_root)
        params = StdioServerParameters(
            command=str(command),
            args=["--application-root", str(self.application_root)],
            cwd=self.root,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
            async with asyncio.timeout(30):
                async with Client(stdio_client(params, errlog=errlog)) as client:
                    listed = await client.list_tools()
                    self.assertEqual(
                        [tool.name for tool in listed.tools], TOOL_NAMES
                    )
                    for tool in listed.tools:
                        schema = tool.input_schema
                        self.assertEqual(schema["type"], "object")
                        self.assertIs(schema["additionalProperties"], False)
                        self.assertEqual(
                            set(schema["required"]), EXPECTED_REQUIRED[tool.name]
                        )
                        self.assertEqual(
                            set(schema["properties"]), EXPECTED_PROPERTIES[tool.name]
                        )
                        properties = schema["properties"]
                        self.assertEqual(
                            properties["library_id"]["pattern"],
                            "^lib_[0-9a-f]{32}$",
                        )
                        self.assertEqual(properties["library_id"]["type"], "string")
                        self.assertEqual(properties["library_id"]["minLength"], 36)
                        self.assertEqual(properties["library_id"]["maxLength"], 36)
                        self.assertEqual(
                            properties["snapshot_id"]["pattern"],
                            "^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}-[0-9a-f]{8}$",
                        )
                        self.assertEqual(properties["snapshot_id"]["type"], "string")
                        self.assertEqual(properties["snapshot_id"]["minLength"], 44)
                        self.assertEqual(properties["snapshot_id"]["maxLength"], 44)
                        if "document_id" in properties:
                            self.assertEqual(properties["document_id"]["type"], "string")
                            self.assertEqual(
                                properties["document_id"]["pattern"],
                                "^doc_[0-9a-f]{24}$",
                            )
                            self.assertEqual(properties["document_id"]["minLength"], 28)
                            self.assertEqual(properties["document_id"]["maxLength"], 28)
                        if "chunk_id" in properties:
                            self.assertEqual(properties["chunk_id"]["type"], "string")
                            self.assertEqual(
                                properties["chunk_id"]["pattern"],
                                "^chunk_[0-9a-f]{24}$",
                            )
                            self.assertEqual(properties["chunk_id"]["minLength"], 30)
                            self.assertEqual(properties["chunk_id"]["maxLength"], 30)
                        if "section_id" in properties:
                            self.assertEqual(properties["section_id"]["type"], "string")
                            self.assertEqual(
                                properties["section_id"]["pattern"],
                                "^sec_[0-9a-f]{24}$",
                            )
                            self.assertEqual(properties["section_id"]["minLength"], 28)
                            self.assertEqual(properties["section_id"]["maxLength"], 28)
                        if "query" in properties:
                            self.assertEqual(properties["query"]["type"], "string")
                            self.assertEqual(properties["query"]["minLength"], 1)
                            self.assertEqual(properties["query"]["maxLength"], 400)
                        if "top_k" in properties:
                            self.assertEqual(properties["top_k"]["type"], "integer")
                            self.assertEqual(properties["top_k"]["minimum"], 1)
                            self.assertEqual(properties["top_k"]["maximum"], 10)
                            self.assertEqual(properties["top_k"]["default"], 5)
                        for character_bound in (
                            "excerpt_chars",
                            "max_chars",
                            "per_item_chars",
                        ):
                            if character_bound in properties:
                                self.assertEqual(
                                    properties[character_bound]["type"], "integer"
                                )
                                self.assertEqual(
                                    properties[character_bound]["minimum"], 1
                                )
                                self.assertEqual(
                                    properties[character_bound]["maximum"], 1200
                                )
                        if "chunk_ids" in properties:
                            self.assertEqual(properties["chunk_ids"]["type"], "array")
                            self.assertEqual(properties["chunk_ids"]["minItems"], 1)
                            self.assertEqual(properties["chunk_ids"]["maxItems"], 5)
                            self.assertIs(properties["chunk_ids"]["uniqueItems"], True)
                            self.assertEqual(
                                properties["chunk_ids"]["items"]["pattern"],
                                "^chunk_[0-9a-f]{24}$",
                            )
                            self.assertEqual(
                                properties["chunk_ids"]["items"]["type"], "string"
                            )
                        if "max_items" in properties:
                            self.assertEqual(properties["max_items"]["type"], "integer")
                            self.assertEqual(properties["max_items"]["minimum"], 1)
                            self.assertEqual(properties["max_items"]["maximum"], 100)
                            self.assertEqual(properties["max_items"]["default"], 100)
                        if "mode" in properties:
                            self.assertEqual(properties["mode"]["type"], "string")
                            self.assertEqual(
                                properties["mode"]["enum"], ["bm25", "enhanced"]
                            )
                        self.assertEqual(
                            {
                                name: definition["default"]
                                for name, definition in properties.items()
                                if "default" in definition
                            },
                            EXPECTED_DEFAULTS[tool.name],
                        )
                        self.assertIsNotNone(tool.annotations)
                        self.assertIs(tool.annotations.read_only_hint, True)
                        self.assertIs(tool.annotations.destructive_hint, False)
                        self.assertIs(
                            tool.annotations.open_world_hint,
                            tool.name == "search_documents",
                        )

                    search = await client.call_tool(
                        "search_documents",
                        {
                            "library_id": self.library_id,
                            "snapshot_id": self.snapshot_id,
                            "query": "calibrationmarker",
                            "top_k": 10,
                            "excerpt_chars": 1200,
                        },
                    )
                    self.assertFalse(search.is_error)
                    search_payload = _protocol_payload(search)
                    self.assertEqual(
                        search_payload,
                        self.service.search_documents(
                            self.library_id,
                            self.snapshot_id,
                            "calibrationmarker",
                            top_k=10,
                            excerpt_chars=1200,
                        ),
                    )
                    explicit_bm25 = await client.call_tool(
                        "search_documents",
                        {
                            "library_id": self.library_id,
                            "snapshot_id": self.snapshot_id,
                            "query": "calibrationmarker",
                            "top_k": 10,
                            "excerpt_chars": 1200,
                            "mode": "bm25",
                        },
                    )
                    self.assertFalse(explicit_bm25.is_error)
                    self.assertEqual(search_payload, _protocol_payload(explicit_bm25))

                    enhanced = await client.call_tool(
                        "search_documents",
                        {
                            "library_id": self.library_id,
                            "snapshot_id": self.snapshot_id,
                            "query": "calibrationmarker",
                            "mode": "enhanced",
                        },
                    )
                    self.assertTrue(enhanced.is_error)
                    enhanced_payload = _protocol_payload(enhanced)
                    self.assertEqual(
                        enhanced_payload["error"]["code"],
                        "LEMCP_E_ENHANCED_UNAVAILABLE",
                    )
                    self.assertEqual(
                        enhanced_payload["audit"],
                        {"simulated": None, "call_count": 0, "calls": []},
                    )
                    _assert_no_private_keys(self, enhanced_payload)
                    self.assertNotIn(
                        str(self.root), json.dumps(enhanced_payload, ensure_ascii=False)
                    )

                    invalid_mode = await client.call_tool(
                        "search_documents",
                        {
                            "library_id": self.library_id,
                            "snapshot_id": self.snapshot_id,
                            "query": "calibrationmarker",
                            "mode": "hybrid",
                        },
                    )
                    self.assertTrue(invalid_mode.is_error)
                    self.assertEqual(
                        _protocol_payload(invalid_mode)["error"]["code"],
                        "LEMCP_E_INVALID_INPUT",
                    )
                    self.assertEqual(
                        before, _tree_identity(self.application_root)
                    )

                    async def valid_call(
                        name: str, arguments: dict[str, Any]
                    ) -> dict[str, Any]:
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(result.is_error, name)
                        payload = _protocol_payload(result)
                        _assert_no_private_keys(self, payload)
                        self.assertNotIn(
                            str(self.root), json.dumps(payload, ensure_ascii=False)
                        )
                        self.assertEqual(
                            before, _tree_identity(self.application_root)
                        )
                        return payload

                    excerpt_arguments = {
                        "library_id": self.library_id,
                        "snapshot_id": self.snapshot_id,
                        "document_id": self.pdf_document_id,
                        "chunk_id": self.pdf_chunk_id,
                    }
                    excerpt_payload = await valid_call(
                        "get_excerpt", excerpt_arguments
                    )
                    self.assertEqual(
                        excerpt_payload["result"]["anchor_label"], "PDF page 2"
                    )
                    self.assertEqual(
                        excerpt_payload["result"]["pdf_page_start"], 2
                    )

                    multiple_arguments = {
                        "library_id": self.library_id,
                        "snapshot_id": self.snapshot_id,
                        "document_id": self.document_id,
                        "chunk_ids": list(reversed(self.chunk_ids[:2])),
                    }
                    multiple_payload = await valid_call(
                        "get_multiple_excerpts", multiple_arguments
                    )
                    self.assertEqual(
                        [item["chunk_id"] for item in multiple_payload["results"]],
                        multiple_arguments["chunk_ids"],
                    )
                    self.assertTrue(
                        all("truncated" in item for item in multiple_payload["results"])
                    )

                    metadata_arguments = {
                        "library_id": self.library_id,
                        "snapshot_id": self.snapshot_id,
                        "document_id": self.pdf_document_id,
                    }
                    metadata_payload = await valid_call(
                        "get_document_metadata", metadata_arguments
                    )
                    self.assertEqual(
                        metadata_payload["document"]["assets"][0]["page_count"], 2
                    )

                    toc_arguments = {
                        "library_id": self.library_id,
                        "snapshot_id": self.snapshot_id,
                        "document_id": self.pdf_document_id,
                    }
                    toc_payload = await valid_call("get_document_toc", toc_arguments)
                    self.assertEqual(
                        [item["section_kind"] for item in toc_payload["items"]],
                        ["pdf_page", "pdf_page"],
                    )
                    self.assertEqual(
                        [item["pdf_page_start"] for item in toc_payload["items"]],
                        [1, 2],
                    )
                    protocol_section_id = toc_payload["items"][1]["section_id"]

                    section_arguments = {
                        "library_id": self.library_id,
                        "snapshot_id": self.snapshot_id,
                        "document_id": self.pdf_document_id,
                        "section_id": protocol_section_id,
                    }
                    section_payload = await valid_call(
                        "read_document_section", section_arguments
                    )
                    self.assertEqual(
                        section_payload["section"]["section_id"],
                        protocol_section_id,
                    )
                    self.assertEqual(
                        section_payload["results"][0]["anchor_label"], "PDF page 2"
                    )
                    self.assertLessEqual(section_payload["returned_chars"], 1200)

                    find_arguments = {
                        "library_id": self.library_id,
                        "snapshot_id": self.snapshot_id,
                        "document_id": self.document_id,
                        "query": "calibrationmarker",
                    }
                    find_payload = await valid_call(
                        "find_in_document", find_arguments
                    )
                    self.assertTrue(find_payload["found"])
                    self.assertEqual(len(find_payload["results"]), 2)
                    self.assertEqual(
                        find_payload["results"][0]["score"],
                        find_payload["results"][1]["score"],
                    )
                    self.assertLess(
                        find_payload["results"][0]["chunk_id"],
                        find_payload["results"][1]["chunk_id"],
                    )

                    status_arguments = {
                        "library_id": self.library_id,
                        "snapshot_id": self.snapshot_id,
                    }
                    status_payload = await valid_call(
                        "retrieval_status", status_arguments
                    )
                    self.assertTrue(status_payload["closed_world"])
                    self.assertTrue(status_payload["readonly"])
                    self.assertEqual(status_payload["transport"], "stdio")
                    libraries_payload = await valid_call("retrieval_status", {})
                    self.assertEqual(libraries_payload["scope"], "libraries")
                    self.assertEqual(
                        [item["library_id"] for item in libraries_payload["libraries"]],
                        [self.library_id],
                    )
                    snapshots_payload = await valid_call(
                        "retrieval_status", {"library_id": self.library_id}
                    )
                    self.assertEqual(snapshots_payload["scope"], "snapshots")
                    self.assertEqual(
                        snapshots_payload["current_snapshot_id"], self.snapshot_id
                    )
                    self.assertEqual(
                        snapshots_payload["last_successful_snapshot_id"],
                        self.foreign_snapshot_id,
                    )

                    valid_arguments_by_tool = {
                        "search_documents": {
                            "library_id": self.library_id,
                            "snapshot_id": self.snapshot_id,
                            "query": "calibrationmarker",
                        },
                        "get_excerpt": excerpt_arguments,
                        "get_multiple_excerpts": multiple_arguments,
                        "get_document_metadata": metadata_arguments,
                        "get_document_toc": toc_arguments,
                        "read_document_section": section_arguments,
                        "find_in_document": find_arguments,
                        "retrieval_status": {},
                    }
                    for name, arguments in valid_arguments_by_tool.items():
                        result = await client.call_tool(
                            name, {**arguments, "unexpected": 1}
                        )
                        self.assertTrue(result.is_error, name)
                        error_payload = _protocol_payload(result)
                        rendered_error = json.dumps(
                            error_payload, ensure_ascii=False
                        )
                        self.assertEqual(
                            error_payload["error"]["code"],
                            "LEMCP_E_INVALID_INPUT",
                        )
                        self.assertNotIn(str(self.root), rendered_error)
                        self.assertNotIn("Traceback", rendered_error)
                        self.assertEqual(
                            before, _tree_identity(self.application_root)
                        )

                    for name, arguments in valid_arguments_by_tool.items():
                        if name == "retrieval_status":
                            continue
                        for missing in ("library_id", "snapshot_id"):
                            incomplete = dict(arguments)
                            incomplete.pop(missing)
                            result = await client.call_tool(name, incomplete)
                            self.assertTrue(result.is_error, (name, missing))
                            self.assertEqual(
                                _protocol_payload(result)["error"]["code"],
                                "LEMCP_E_INVALID_INPUT",
                            )
                            self.assertEqual(
                                before, _tree_identity(self.application_root)
                            )

                    for name, arguments in (
                        (
                            "search_documents",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "query": "termabsentfromsnapshot",
                            },
                        ),
                        (
                            "find_in_document",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "document_id": self.document_id,
                                "query": "termabsentfromsnapshot",
                            },
                        ),
                    ):
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(result.is_error)
                        payload = _protocol_payload(result)
                        self.assertFalse(payload["found"])
                        self.assertEqual(payload["results"], [])
                        self.assertEqual(
                            before, _tree_identity(self.application_root)
                        )

                    invalid_calls = [
                        (
                            "search_documents",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "query": "evidence",
                                "unexpected": 1,
                            },
                        ),
                        (
                            "search_documents",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "query": "evidence",
                                "top_k": True,
                            },
                        ),
                        (
                            "search_documents",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "query": "x" * 401,
                            },
                        ),
                        (
                            "search_documents",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "query": "x" + " " * 400,
                            },
                        ),
                        (
                            "get_excerpt",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "document_id": self.other_document_id,
                                "chunk_id": self.chunk_ids[0],
                            },
                        ),
                        (
                            "get_multiple_excerpts",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "document_id": self.document_id,
                                "chunk_ids": [self.chunk_ids[0], self.chunk_ids[0]],
                            },
                        ),
                        (
                            "get_document_metadata",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.foreign_snapshot_id,
                                "document_id": self.document_id,
                            },
                        ),
                        (
                            "read_document_section",
                            {
                                "library_id": self.library_id,
                                "snapshot_id": self.snapshot_id,
                                "document_id": self.other_document_id,
                                "section_id": self.section_id,
                            },
                        ),
                        (
                            "retrieval_status",
                            {"snapshot_id": "/tmp/not-a-snapshot"},
                        ),
                    ]
                    for name, arguments in invalid_calls:
                        result = await client.call_tool(name, arguments)
                        self.assertTrue(result.is_error, (name, arguments))
                        payload = _protocol_payload(result)
                        self.assertIn("error", payload)
                        rendered_error = json.dumps(payload, ensure_ascii=False)
                        self.assertRegex(
                            payload["error"]["code"], r"^LEMCP_E_[A-Z_]+$"
                        )
                        self.assertNotIn(str(self.root), rendered_error)
                        self.assertNotIn("Traceback", rendered_error)
                        self.assertEqual(
                            before, _tree_identity(self.application_root)
                        )
            errlog.flush()
            errlog.seek(0)
            stderr = errlog.read()

        self.assertEqual(before, _tree_identity(self.application_root))
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn(str(self.root), stderr)


if __name__ == "__main__":
    unittest.main()
