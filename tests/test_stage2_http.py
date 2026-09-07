"""HTTP boundary tests migrated from the historical fixed-library web entry.

The browser service now owns one application root. Every content request uses a
strict library_id and snapshot_id; no implicit single-library compatibility route
is retained.
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import uvicorn
from starlette.testclient import TestClient

from literature_evidence_mcp import snapshot as snapshot_module
from literature_evidence_mcp.cli import main as cli_main
from literature_evidence_mcp.errors import LibraryRegistryError, SnapshotError
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.web import (
    BUILD_INTENT,
    SESSION_COOKIE,
    UploadLimits,
    create_app,
    serve_local,
)
from tests.test_stage1 import _pdf_bytes, _tree_hashes


PORT = 18765
AUTHORITY = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{AUTHORITY}"


def _sidecars(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.name.endswith(("-journal", "-wal", "-shm"))
    ]


class StageTwoHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lemcp-http-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.application_root = self.root / "application"
        self.app = create_app(self.application_root, port=PORT)
        self.client = TestClient(
            self.app,
            base_url=ORIGIN,
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def bootstrap(self, client: TestClient | None = None) -> str:
        response = (client or self.client).get("/api/status")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf_token"]

    @staticmethod
    def post_headers(token: str, intent: str) -> dict[str, str]:
        return {
            "Origin": ORIGIN,
            "X-CSRF-Token": token,
            "X-Action-Intent": intent,
        }

    def create_library(
        self,
        token: str,
        *,
        client: TestClient | None = None,
        name: str = "测试资料库",
    ) -> str:
        response = (client or self.client).post(
            "/api/libraries",
            headers={
                **self.post_headers(token, "create-library"),
                "Content-Type": "application/json",
            },
            content=json.dumps({"name": name, "description": "合成 HTTP 测试"}),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["library"]["library_id"]

    def build_files(
        self,
        token: str,
        library_id: str,
        files: list[tuple[str, tuple[str, bytes, str]]],
        *,
        client: TestClient | None = None,
        mode: str = "blank",
        base_snapshot_id: str | None = None,
        intent: str | None = BUILD_INTENT,
    ):
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": token,
        }
        if intent is not None:
            headers["X-Action-Intent"] = intent
        data = {"build_mode": mode}
        if base_snapshot_id is not None:
            data["base_snapshot_id"] = base_snapshot_id
        return (client or self.client).post(
            f"/api/libraries/{library_id}/build",
            headers=headers,
            data=data,
            files=files,
        )

    def test_status_and_lists_are_readonly_and_static_ui_is_package_local(self) -> None:
        self.assertFalse(self.application_root.exists())
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["binding"], "127.0.0.1")
        self.assertTrue(payload["local_only"])
        self.assertEqual(payload["offline_default"], "bm25")
        self.assertEqual(payload["library_count"], 0)
        self.assertEqual(
            payload["readonly_actions"], ["status", "list", "verify", "search"]
        )
        self.assertEqual(
            payload["write_actions"], ["create", "select", "build", "activate"]
        )
        self.assertFalse(self.application_root.exists())
        listed = self.client.get("/api/libraries")
        self.assertEqual(listed.json(), {"libraries": []})
        self.assertFalse(self.application_root.exists())

        cookie = response.headers["set-cookie"]
        self.assertIn(f"{SESSION_COOKIE}_{PORT}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Path=/", cookie)

        page = self.client.get("/")
        script = self.client.get("/static/app.js")
        styles = self.client.get("/static/styles.css")
        self.assertEqual(
            (page.status_code, script.status_code, styles.status_code),
            (200, 200, 200),
        )
        self.assertIn("<title>FolioHook · 寻章 — 本地知识库</title>", page.text)
        self.assertIn("<h1>寻章 · 本地知识库</h1>", page.text)
        self.assertIn("创建或切换资料库", page.text)
        self.assertIn("拖到这里", page.text)
        self.assertIn('/static/app.js', page.text)
        self.assertIn('/static/styles.css', page.text)
        self.assertIn(BUILD_INTENT, script.text)
        self.assertIn("anchor_label", script.text)
        self.assertIn("全文未核验", script.text)
        self.assertNotIn("https://", page.text + script.text)
        self.assertIn("default-src 'self'", page.headers["content-security-policy"])
        self.assertIn("form-action 'none'", page.headers["content-security-policy"])
        for item in (response, listed, page, script, styles):
            self.assertEqual(item.headers["cache-control"], "no-store")
            self.assertNotIn("access-control-allow-origin", item.headers)

    def test_host_origin_session_csrf_and_action_intent_are_enforced(self) -> None:
        self.assertEqual(self.client.get("/api/libraries").status_code, 403)
        token = self.bootstrap()
        for host in ("evil.example", "127.0.0.1:9999", "localhost:18765", ""):
            with self.subTest(host=host):
                rejected = self.client.get("/api/status", headers={"Host": host})
                self.assertEqual(rejected.status_code, 400)
                self.assertIn("Host", rejected.json()["error"])
        duplicate_host = self.client.get(
            "/api/status",
            headers=[("Host", AUTHORITY), ("Host", "evil.example")],
        )
        self.assertEqual(duplicate_host.status_code, 400)
        for origin in (
            "https://127.0.0.1:18765",
            "http://evil.example",
            "null",
        ):
            with self.subTest(origin=origin):
                rejected = self.client.get("/api/status", headers={"Origin": origin})
                self.assertEqual(rejected.status_code, 403)
                self.assertNotIn("access-control-allow-origin", rejected.headers)
        body = '{"name":"拒绝"}'
        self.assertEqual(
            self.client.post(
                "/api/libraries",
                headers={
                    "X-CSRF-Token": token,
                    "X-Action-Intent": "create-library",
                    "Content-Type": "application/json",
                },
                content=body,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/libraries",
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": "0" * 64,
                    "X-Action-Intent": "create-library",
                    "Content-Type": "application/json",
                },
                content=body,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/libraries",
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": token,
                    "Content-Type": "application/json",
                },
                content=body,
            ).status_code,
            400,
        )

        search_body = '{"snapshot_id":"invalid","query":"evidence"}'
        self.assertEqual(
            self.client.post(
                "/api/libraries/invalid/search",
                headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
                content=search_body,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/libraries/invalid/search",
                headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                content=search_body,
            ).status_code,
            403,
        )
        with TestClient(
            self.app,
            base_url=ORIGIN,
            raise_server_exceptions=False,
        ) as second:
            second_token = self.bootstrap(second)
            self.assertNotEqual(token, second_token)
            crossed = second.post(
                "/api/libraries/invalid/search",
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": token,
                    "Content-Type": "application/json",
                },
                content=search_body,
            )
            self.assertEqual(crossed.status_code, 403)

        preflight = self.client.options(
            "/api/libraries/invalid/build",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(preflight.status_code, 403)
        self.assertNotIn("access-control-allow-origin", preflight.headers)
        self.assertFalse(self.application_root.exists())

    def test_two_ports_keep_independent_browser_sessions(self) -> None:
        first_port = PORT + 1
        second_port = PORT + 2
        first_origin = f"http://127.0.0.1:{first_port}"
        second_origin = f"http://127.0.0.1:{second_port}"
        with TestClient(
            create_app(self.root / "first", port=first_port),
            base_url=first_origin,
            raise_server_exceptions=False,
        ) as first, TestClient(
            create_app(self.root / "second", port=second_port),
            base_url=second_origin,
            raise_server_exceptions=False,
        ) as second:
            first.get("/api/status")
            second.get("/api/status")
            first_name = f"{SESSION_COOKIE}_{first_port}"
            second_name = f"{SESSION_COOKIE}_{second_port}"
            self.assertIn(first_name, dict(first.cookies))
            self.assertIn(second_name, dict(second.cookies))
            self.assertNotEqual(first_name, second_name)
            second_value = second.cookies.get(f"{SESSION_COOKIE}_{second_port}")
            rejected = first.get(
                "/api/libraries",
                headers={
                    "Cookie": f"{SESSION_COOKIE}_{first_port}={second_value}"
                },
            )
            self.assertEqual(rejected.status_code, 403)

    def test_status_and_library_list_count_without_verifying_snapshots(self) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        built = self.build_files(
            token,
            library_id,
            [("files", ("safe.md", b"# Safe\n\nstatus evidence\n", "text/markdown"))],
        )
        self.assertEqual(built.status_code, 201, built.text)

        with mock.patch(
            "literature_evidence_mcp.library.verify_snapshot",
            side_effect=AssertionError("status and library list must stay cheap"),
        ):
            with TestClient(
                self.app,
                base_url=ORIGIN,
                raise_server_exceptions=False,
            ) as anonymous:
                status = anonymous.get("/api/status")
                listed = anonymous.get("/api/libraries")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["library_count"], 1)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["libraries"][0]["snapshot_count"], 1)

    def test_build_list_verify_search_and_readonly_hashes_use_two_ids(self) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        built = self.build_files(
            token,
            library_id,
            [
                ("files", ("one.md", b"# One\n\nalpha evidence\n", "text/markdown")),
                (
                    "files",
                    (
                        "two.pdf",
                        _pdf_bytes(
                            [
                                "First page voltage evidence.",
                                "Second page harmonicpageanchor evidence.",
                            ]
                        ),
                        "application/pdf",
                    ),
                ),
            ],
        )
        self.assertEqual(built.status_code, 201, built.text)
        payload = built.json()
        snapshot_id = payload["snapshot_id"]
        self.assertEqual(payload["library_id"], library_id)
        self.assertEqual(payload["difference"]["counts"]["added"], 2)
        library_root = self.application_root / "libraries" / library_id
        before = _tree_hashes(library_root)

        listed = self.client.get(f"/api/libraries/{library_id}/snapshots")
        verified = self.client.get(
            f"/api/libraries/{library_id}/snapshots/{snapshot_id}/verify"
        )
        searched = self.client.post(
            f"/api/libraries/{library_id}/search",
            headers={
                **self.post_headers(token, "search"),
                "Content-Type": "application/json",
            },
            content=json.dumps(
                {"snapshot_id": snapshot_id, "query": "harmonicpageanchor"}
            ),
        )
        self.assertEqual((listed.status_code, verified.status_code, searched.status_code), (200, 200, 200))
        self.assertTrue(verified.json()["verified"])
        self.assertTrue(searched.json()["found"])
        self.assertEqual(searched.json()["retrieval_mode"], "bm25")
        hit = searched.json()["results"][0]
        self.assertEqual(hit["source_name"], "two.pdf")
        self.assertEqual(hit["anchor_label"], "PDF page 2")
        self.assertFalse(hit["fulltext_verified"])
        self.assertFalse(hit["formula_verified"])
        self.assertEqual(before, _tree_hashes(library_root))
        combined = built.text + listed.text + verified.text + searched.text
        self.assertNotIn("snapshot_path", built.text)
        self.assertNotIn(str(self.root), combined)
        self.assertEqual(self.client.get("/api/snapshots").status_code, 404)

    def test_repeat_build_never_overwrites_first_snapshot(self) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        files = [
            (
                "files",
                ("stable.md", b"# Stable\n\nrepeatable evidence\n", "text/markdown"),
            )
        ]
        first = self.build_files(token, library_id, files)
        self.assertEqual(first.status_code, 201, first.text)
        first_payload = first.json()
        library_root = self.application_root / "libraries" / library_id
        first_path = library_root / "snapshots" / first_payload["snapshot_id"]
        first_hashes = _tree_hashes(first_path)

        second = self.build_files(token, library_id, files)
        self.assertEqual(second.status_code, 201, second.text)
        second_payload = second.json()
        self.assertNotEqual(first_payload["snapshot_id"], second_payload["snapshot_id"])
        self.assertEqual(first_payload["corpus_sha256"], second_payload["corpus_sha256"])
        self.assertEqual(
            first_payload["database_sha256"], second_payload["database_sha256"]
        )
        self.assertEqual(first_hashes, _tree_hashes(first_path))
        listed = self.client.get(
            f"/api/libraries/{library_id}/snapshots"
        ).json()["snapshots"]
        self.assertEqual(len(listed), 2)

    def test_snapshot_list_derives_top_level_pointers_from_the_same_rows(
        self,
    ) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        first = self.build_files(
            token,
            library_id,
            [("files", ("first.md", b"# First\n\nalpha\n", "text/markdown"))],
        )
        second = self.build_files(
            token,
            library_id,
            [("files", ("second.md", b"# Second\n\nbeta\n", "text/markdown"))],
        )
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        second_id = second.json()["snapshot_id"]
        activated = self.client.post(
            f"/api/libraries/{library_id}/snapshots/{second_id}/activate",
            headers=self.post_headers(token, "activate-snapshot"),
        )
        self.assertEqual(activated.status_code, 200, activated.text)

        with mock.patch.object(
            FixedLibrary,
            "catalog_status",
            side_effect=AssertionError("snapshot listing must use one catalog read"),
        ):
            listed = self.client.get(f"/api/libraries/{library_id}/snapshots")
        self.assertEqual(listed.status_code, 200, listed.text)
        payload = listed.json()
        current_rows = [
            item["snapshot_id"] for item in payload["snapshots"] if item["current"]
        ]
        last_rows = [
            item["snapshot_id"]
            for item in payload["snapshots"]
            if item["last_successful"]
        ]
        self.assertEqual(current_rows, [payload["current_snapshot_id"]])
        self.assertEqual(last_rows, [payload["last_successful_snapshot_id"]])

    def test_build_mode_upload_names_types_and_limits_are_strict(self) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        missing_mode = self.client.post(
            f"/api/libraries/{library_id}/build",
            headers=self.post_headers(token, BUILD_INTENT),
            files=[("files", ("ok.md", b"# OK\n\ntext\n", "text/markdown"))],
        )
        self.assertEqual(missing_mode.status_code, 400)
        blank_with_base = self.build_files(
            token,
            library_id,
            [("files", ("ok.md", b"# OK\n\ntext\n", "text/markdown"))],
            base_snapshot_id="20260101T000000000000Z-000000000000-00000000",
        )
        self.assertEqual(blank_with_base.status_code, 400)
        inherit_without_base = self.build_files(
            token,
            library_id,
            [("files", ("ok.md", b"# OK\n\ntext\n", "text/markdown"))],
            mode="inherit",
        )
        self.assertEqual(inherit_without_base.status_code, 400)
        for filename in (
            "../escape.md",
            "folder\\escape.md",
            "unsupported.txt",
        ):
            with self.subTest(filename=filename):
                response = self.build_files(
                    token,
                    library_id,
                    [("files", (filename, b"# Bad\n\ntext\n", "text/plain"))],
                )
                self.assertEqual(response.status_code, 400, response.text)
        duplicate = self.build_files(
            token,
            library_id,
            [
                ("files", ("same.md", b"# One\n\nfirst\n", "text/markdown")),
                ("files", ("SAME.md", b"# Two\n\nsecond\n", "text/markdown")),
            ],
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(
            [item["state"] for item in duplicate.json()["files"]],
            ["failed", "failed"],
        )

        small_root = self.root / "small"
        limits = UploadLimits(
            max_files=2,
            max_file_bytes=32,
            max_total_bytes=48,
            max_request_bytes=2048,
            max_filename_bytes=20,
            max_json_bytes=512,
        )
        with TestClient(
            create_app(small_root, port=PORT, upload_limits=limits),
            base_url=ORIGIN,
            raise_server_exceptions=False,
        ) as small:
            small_token = small.get("/api/status").json()["csrf_token"]
            created = small.post(
                "/api/libraries",
                headers={
                    **self.post_headers(small_token, "create-library"),
                    "Content-Type": "application/json",
                },
                content='{"name":"small"}',
            )
            small_id = created.json()["library"]["library_id"]
            long_name = small.post(
                f"/api/libraries/{small_id}/build",
                headers=self.post_headers(small_token, BUILD_INTENT),
                data={"build_mode": "blank"},
                files=[
                    (
                        "files",
                        (
                            "name-with-too-many-bytes.md",
                            b"# Bad\n\ntext\n",
                            "text/markdown",
                        ),
                    )
                ],
            )
            self.assertEqual(long_name.status_code, 400, long_name.text)
            too_large = small.post(
                f"/api/libraries/{small_id}/build",
                headers=self.post_headers(small_token, BUILD_INTENT),
                data={"build_mode": "blank"},
                files=[
                    ("files", ("good.md", b"# good\n", "text/markdown")),
                    ("files", ("large.md", b"x" * 33, "text/markdown")),
                ],
            )
            self.assertEqual(too_large.status_code, 413, too_large.text)
            self.assertEqual(
                [item["state"] for item in too_large.json()["files"]],
                ["not_published", "failed"],
            )

            exact_file = small.post(
                f"/api/libraries/{small_id}/build",
                headers=self.post_headers(small_token, BUILD_INTENT),
                data={"build_mode": "blank"},
                files=[
                    (
                        "files",
                        ("exact.md", b"# A\n\n" + b"x" * 27, "text/markdown"),
                    )
                ],
            )
            self.assertEqual(exact_file.status_code, 201, exact_file.text)
            exact_total = small.post(
                f"/api/libraries/{small_id}/build",
                headers=self.post_headers(small_token, BUILD_INTENT),
                data={"build_mode": "blank"},
                files=[
                    ("files", ("one.md", b"# A\n\n" + b"x" * 19, "text/markdown")),
                    ("files", ("two.md", b"# B\n\n" + b"y" * 19, "text/markdown")),
                ],
            )
            self.assertEqual(exact_total.status_code, 201, exact_total.text)

            oversized_json = small.post(
                f"/api/libraries/{small_id}/search",
                headers={
                    **self.post_headers(small_token, "search"),
                    "Content-Type": "application/json",
                },
                content=json.dumps(
                    {
                        "snapshot_id": exact_total.json()["snapshot_id"],
                        "query": "x" * 600,
                    }
                ),
            )
            self.assertEqual(oversized_json.status_code, 413)

            total_over = small.post(
                f"/api/libraries/{small_id}/build",
                headers=self.post_headers(small_token, BUILD_INTENT),
                data={"build_mode": "blank"},
                files=[
                    ("files", ("one.md", b"# A\n\n" + b"x" * 20, "text/markdown")),
                    ("files", ("two.md", b"# B\n\n" + b"y" * 20, "text/markdown")),
                ],
            )
            self.assertEqual(total_over.status_code, 413, total_over.text)

            too_many = small.post(
                f"/api/libraries/{small_id}/build",
                headers=self.post_headers(small_token, BUILD_INTENT),
                data={"build_mode": "blank"},
                files=[
                    ("files", ("a.md", b"text", "text/markdown")),
                    ("files", ("b.md", b"text", "text/markdown")),
                    ("files", ("c.md", b"text", "text/markdown")),
                ],
            )
            self.assertEqual(too_many.status_code, 413, too_many.text)

            request_too_large = small.post(
                f"/api/libraries/{small_id}/build",
                headers={
                    **self.post_headers(small_token, BUILD_INTENT),
                    "Content-Type": "multipart/form-data; boundary=x",
                    "Content-Length": "1",
                },
                content=b"x" * 3000,
            )
            self.assertEqual(request_too_large.status_code, 413)
            self.assertIn("x-frame-options", request_too_large.headers)
            snapshots = small.get(
                f"/api/libraries/{small_id}/snapshots"
            ).json()["snapshots"]
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(_sidecars(small_root), [])

    def test_build_rejects_wrong_method_intent_media_and_malformed_multipart(
        self,
    ) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        route = f"/api/libraries/{library_id}/build"
        self.assertEqual(self.client.get(route).status_code, 405)

        no_intent = self.build_files(
            token,
            library_id,
            [("files", ("note.md", b"local text", "text/markdown"))],
            intent=None,
        )
        self.assertEqual(no_intent.status_code, 400)

        path_json = self.client.post(
            route,
            headers={
                **self.post_headers(token, BUILD_INTENT),
                "Content-Type": "application/json",
            },
            content='{"paths":["/etc/passwd"]}',
        )
        self.assertEqual(path_json.status_code, 415)

        with mock.patch(
            "literature_evidence_mcp.web.tempfile.mkdtemp"
        ) as make_temporary_directory:
            rejected_before_parse = self.client.post(
                route,
                headers={
                    "Host": "evil.example",
                    **self.post_headers(token, BUILD_INTENT),
                },
                data={"build_mode": "blank"},
                files=[("files", ("note.md", b"text", "text/markdown"))],
            )
        self.assertEqual(rejected_before_parse.status_code, 400)
        make_temporary_directory.assert_not_called()

        malformed = self.client.post(
            route,
            headers={
                **self.post_headers(token, BUILD_INTENT),
                "Content-Type": "multipart/form-data; boundary=expected",
            },
            content=b"--different\r\ninvalid multipart\r\n--different--\r\n",
        )
        self.assertEqual(malformed.status_code, 400, malformed.text)
        self.assertIn("multipart", malformed.json()["error"])
        self.assertNotIn("Traceback", malformed.text)
        library_root = self.application_root / "libraries" / library_id
        self.assertFalse((library_root / "snapshots").exists())

    def test_snapshot_routes_reject_unsafe_ids_paths_and_symlink_roots(self) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        built = self.build_files(
            token,
            library_id,
            [("files", ("safe.md", b"# Safe\n\nlisted evidence\n", "text/markdown"))],
        )
        self.assertEqual(built.status_code, 201, built.text)
        safe_id = built.json()["snapshot_id"]
        library_root = self.application_root / "libraries" / library_id
        snapshots = library_root / "snapshots"
        (snapshots / ".building-leftover").mkdir()
        (snapshots / "invalid-name").mkdir()
        unicode_digits_id = "٠" * 8 + "T" + "٠" * 12 + "Z-aaaaaaaaaaaa-00000000"
        (snapshots / unicode_digits_id).mkdir()
        regular_id = "20260101T000000000000Z-aaaaaaaaaaaa-11111111"
        (snapshots / regular_id).write_text("not a directory", encoding="utf-8")
        external_candidate = self.root / "external-candidate"
        external_candidate.mkdir()
        symlink_id = "20260101T000000000000Z-bbbbbbbbbbbb-22222222"
        (snapshots / symlink_id).symlink_to(
            external_candidate,
            target_is_directory=True,
        )

        listed = self.client.get(f"/api/libraries/{library_id}/snapshots")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [item["snapshot_id"] for item in listed.json()["snapshots"]],
            [safe_id],
        )
        invalid = self.client.get(
            f"/api/libraries/{library_id}/snapshots/not-a-snapshot/verify"
        )
        self.assertEqual(invalid.status_code, 400)
        nonexistent_id = "20260101T000000000000Z-cccccccccccc-33333333"
        missing = self.client.get(
            f"/api/libraries/{library_id}/snapshots/{nonexistent_id}/verify"
        )
        self.assertEqual(missing.status_code, 404)
        traversal = self.client.get(
            f"/api/libraries/{library_id}/snapshots/%2E%2E/verify"
        )
        self.assertIn(traversal.status_code, {400, 404})
        extra_path = self.client.post(
            f"/api/libraries/{library_id}/search",
            headers={
                **self.post_headers(token, "search"),
                "Content-Type": "application/json",
            },
            content=json.dumps(
                {
                    "snapshot_id": safe_id,
                    "query": "evidence",
                    "snapshot_path": "/etc",
                }
            ),
        )
        self.assertEqual(extra_path.status_code, 400)

        linked_library_id = self.create_library(token, name="快照链接拒绝")
        linked_root = self.application_root / "libraries" / linked_library_id
        outside_snapshots = self.root / "outside-snapshots"
        outside_snapshots.mkdir()
        (linked_root / "snapshots").symlink_to(
            outside_snapshots,
            target_is_directory=True,
        )
        rejected = self.build_files(
            token,
            linked_library_id,
            [("files", ("blocked.md", b"# Blocked\n\ntext\n", "text/markdown"))],
        )
        self.assertIn(rejected.status_code, {400, 422})
        self.assertFalse(rejected.json()["published"])
        self.assertEqual(list(outside_snapshots.iterdir()), [])

    def test_search_parameter_boundaries_and_continuous_chinese_hint(self) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        built = self.build_files(
            token,
            library_id,
            [
                (
                    "files",
                    (
                        "grid.md",
                        "# 配电网\n\n配电网 谐波 监测 使用 电压 频谱。\n".encode(),
                        "text/markdown",
                    ),
                )
            ],
        )
        self.assertEqual(built.status_code, 201, built.text)
        snapshot_id = built.json()["snapshot_id"]

        def search(payload: dict[str, object]):
            return self.client.post(
                f"/api/libraries/{library_id}/search",
                headers={
                    **self.post_headers(token, "search"),
                    "Content-Type": "application/json",
                },
                content=json.dumps({"snapshot_id": snapshot_id, **payload}),
            )

        for payload in (
            {"query": "x", "top_k": 1, "excerpt_chars": 1},
            {"query": "x" * 400, "top_k": 10, "excerpt_chars": 1200},
        ):
            with self.subTest(valid=payload):
                self.assertEqual(search(payload).status_code, 200)
        for payload in (
            {"query": ""},
            {"query": "x" * 401},
            {"query": "x\u0000"},
            {"query": 1},
            {"query": "x", "top_k": 0},
            {"query": "x", "top_k": 11},
            {"query": "x", "top_k": True},
            {"query": "x", "top_k": 1.5},
            {"query": "x", "excerpt_chars": 0},
            {"query": "x", "excerpt_chars": 1201},
            {"query": "x", "excerpt_chars": False},
        ):
            with self.subTest(invalid=payload):
                self.assertEqual(search(payload).status_code, 400)

        continuous = search({"query": "配电网谐波监测"})
        self.assertEqual(continuous.status_code, 200)
        self.assertFalse(continuous.json()["found"])
        self.assertEqual(continuous.json()["results"], [])
        self.assertIn("加空格", continuous.json()["input_hint"])

    def test_failed_build_cleans_upload_and_building_directories_without_leaks(
        self,
    ) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        first = self.build_files(
            token,
            library_id,
            [("files", ("kept.md", b"# Kept\n\nold evidence\n", "text/markdown"))],
        )
        self.assertEqual(first.status_code, 201, first.text)
        library_root = self.application_root / "libraries" / library_id
        before_catalog = (library_root / "snapshot-catalog.json").read_bytes()
        before_snapshots = _tree_hashes(library_root / "snapshots")
        upload_parent = self.root / "upload-parent"
        upload_parent.mkdir()

        with mock.patch.object(tempfile, "tempdir", str(upload_parent)):
            with mock.patch.object(
                snapshot_module,
                "_verify_snapshot",
                side_effect=SnapshotError("forced /private/secret traceback marker"),
            ):
                failed = self.build_files(
                    token,
                    library_id,
                    [
                        (
                            "files",
                            ("failing.md", b"# Fail\n\nnew evidence\n", "text/markdown"),
                        )
                    ],
                )
        self.assertEqual(failed.status_code, 400, failed.text)
        self.assertFalse(failed.json()["published"])
        self.assertNotIn("/private/secret", failed.text)
        self.assertNotIn("Traceback", failed.text)
        self.assertEqual(
            before_catalog,
            (library_root / "snapshot-catalog.json").read_bytes(),
        )
        self.assertEqual(before_snapshots, _tree_hashes(library_root / "snapshots"))
        self.assertEqual(list(upload_parent.iterdir()), [])
        self.assertFalse(
            any(
                path.name.startswith(".building-")
                for path in (library_root / "snapshots").iterdir()
            )
        )
        self.assertEqual(_sidecars(library_root), [])

    def test_cleanup_failure_after_publish_keeps_truthful_success_response(
        self,
    ) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        upload_parent = self.root / "cleanup-warning-uploads"
        upload_parent.mkdir()
        original_build = FixedLibrary.build

        def counted_build(instance, controlled_sources, **kwargs):
            return original_build(instance, controlled_sources, **kwargs)

        with mock.patch.object(tempfile, "tempdir", str(upload_parent)):
            with mock.patch.object(
                FixedLibrary,
                "build",
                autospec=True,
                side_effect=counted_build,
            ) as build_mock:
                with mock.patch(
                    "literature_evidence_mcp.web.shutil.rmtree",
                    side_effect=OSError("forced /private/cleanup path"),
                ) as cleanup_mock:
                    response = self.build_files(
                        token,
                        library_id,
                        [
                            (
                                "files",
                                (
                                    "published.md",
                                    b"# Published\n\ntruthful success\n",
                                    "text/markdown",
                                ),
                            )
                        ],
                    )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertTrue(payload["published"])
        self.assertIn("已成功发布", payload["cleanup_warning"])
        self.assertIn("请勿重复构建", payload["cleanup_warning"])
        self.assertNotIn("/private", response.text)
        self.assertEqual(build_mock.call_count, 1)
        self.assertEqual(cleanup_mock.call_count, 1)
        snapshot_id = payload["snapshot_id"]
        library_root = self.application_root / "libraries" / library_id
        self.assertTrue((library_root / "snapshots" / snapshot_id).is_dir())
        listed = self.client.get(f"/api/libraries/{library_id}/snapshots")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["current_snapshot_id"], snapshot_id)
        self.assertEqual(listed.json()["last_successful_snapshot_id"], snapshot_id)

    def test_concurrent_build_is_rejected_without_retry(self) -> None:
        token = self.bootstrap()
        library_id = self.create_library(token)
        entered = threading.Event()
        release = threading.Event()
        original_build = FixedLibrary.build
        result: dict[str, object] = {}

        def blocking_build(instance, controlled_sources, **kwargs):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("test build lock was not released")
            return original_build(instance, controlled_sources, **kwargs)

        def run_first() -> None:
            response = self.build_files(
                token,
                library_id,
                [("files", ("first.md", b"# First\n\nlocked build\n", "text/markdown"))],
            )
            result["status"] = response.status_code

        with TestClient(
            self.app,
            base_url=ORIGIN,
            raise_server_exceptions=False,
        ) as second:
            second_token = self.bootstrap(second)
            with mock.patch.object(FixedLibrary, "build", blocking_build):
                thread = threading.Thread(target=run_first)
                thread.start()
                self.assertTrue(entered.wait(timeout=2))
                rejected = self.build_files(
                    second_token,
                    library_id,
                    [("files", ("second.md", b"# Second\n\nother\n", "text/markdown"))],
                    client=second,
                )
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertIn("未重试", rejected.text)
                release.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(result["status"], 201)
        snapshots = self.client.get(
            f"/api/libraries/{library_id}/snapshots"
        ).json()["snapshots"]
        self.assertEqual(len(snapshots), 1)

    def test_application_root_symlink_is_rejected(self) -> None:
        target = self.root / "target"
        target.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(target, target_is_directory=True)
        with self.assertRaises(LibraryRegistryError):
            create_app(linked, port=PORT)


class StageTwoPortLifecycleTests(unittest.TestCase):
    def test_occupied_port_reports_chinese_before_claiming_page(self) -> None:
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        output = io.StringIO()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            try:
                with redirect_stdout(output), redirect_stderr(errors):
                    status = cli_main(
                        [
                            "serve",
                            "--application-root",
                            str(Path(raw) / "application"),
                            "--port",
                            str(port),
                        ]
                    )
            finally:
                occupied.close()
        self.assertEqual(status, 2)
        self.assertNotIn("本机管理页", output.getvalue())
        self.assertIn("端口可能已被占用", errors.getvalue())

    def test_serve_ctrl_c_exits_without_traceback(self) -> None:
        with mock.patch(
            "literature_evidence_mcp.web.serve_local", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(
                cli_main(["serve", "--application-root", "/tmp/fixed-application"]),
                130,
            )

    def test_real_uvicorn_start_stop_releases_port(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            app = create_app(Path(raw) / "application", port=port)
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                access_log=False,
                proxy_headers=False,
                server_header=False,
                log_level="critical",
                lifespan="off",
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(server.started)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            server.should_exit = True
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                rebound.bind(("127.0.0.1", port))
            finally:
                rebound.close()

    def test_serve_entry_forces_loopback_and_safe_uvicorn_options(self) -> None:
        captured: dict[str, object] = {}
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        class FakeServer:
            def __init__(self, config: uvicorn.Config) -> None:
                captured["config"] = config

            def run(self, sockets=None) -> None:
                captured["ran"] = True
                captured["listener"] = sockets[0].getsockname()

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("uvicorn.Server", FakeServer):
                with redirect_stdout(io.StringIO()):
                    serve_local(Path(raw) / "application", port=port)
        config = captured["config"]
        self.assertIsInstance(config, uvicorn.Config)
        assert isinstance(config, uvicorn.Config)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, port)
        self.assertFalse(config.access_log)
        self.assertFalse(config.proxy_headers)
        self.assertFalse(config.server_header)
        self.assertEqual(config.workers, 1)
        self.assertTrue(captured["ran"])
        self.assertEqual(captured["listener"], ("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
