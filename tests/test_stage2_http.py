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
from literature_evidence_mcp.errors import ImportPolicyError, SnapshotError
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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "library"
        self.app = create_app(self.library, port=PORT)
        self.client = TestClient(
            self.app,
            base_url=ORIGIN,
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def bootstrap(self, client: TestClient | None = None) -> str:
        active = client or self.client
        response = active.get("/api/status")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf_token"]

    def post_headers(self, token: str) -> dict[str, str]:
        return {"Origin": ORIGIN, "X-CSRF-Token": token}

    def build_files(
        self,
        token: str,
        files: list[tuple[str, tuple[str, bytes, str]]],
        *,
        client: TestClient | None = None,
        intent: str | None = BUILD_INTENT,
    ):
        active = client or self.client
        headers = self.post_headers(token)
        if intent is not None:
            headers["X-Build-Intent"] = intent
        return active.post("/api/build", headers=headers, files=files)

    def test_status_is_readonly_and_static_ui_is_package_local(self) -> None:
        self.assertFalse(self.library.exists())
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["binding"], "127.0.0.1")
        self.assertFalse(payload["library"]["available"])
        self.assertEqual(payload["library"]["snapshot_candidates"], 0)
        self.assertEqual(payload["write_actions"], ["build"])
        self.assertEqual(
            payload["readonly_actions"], ["status", "list", "verify", "search"]
        )
        self.assertFalse(self.library.exists())
        listed = self.client.get("/api/snapshots")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["snapshots"], [])
        self.assertFalse(self.library.exists())

        cookie = response.headers["set-cookie"]
        self.assertIn(f"{SESSION_COOKIE}_{PORT}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Secure", cookie)

        page = self.client.get("/")
        script = self.client.get("/static/app.js")
        styles = self.client.get("/static/styles.css")
        self.assertEqual((page.status_code, script.status_code, styles.status_code), (200, 200, 200))
        self.assertIn("明确构建一个全新快照", page.text)
        self.assertIn("关键词之间加入空格", page.text)
        self.assertIn('/static/app.js', page.text)
        self.assertIn('/static/styles.css', page.text)
        self.assertNotIn("https://", page.text)
        self.assertIn("anchor_label", script.text)
        self.assertIn("全文未核验", script.text)
        self.assertIn(BUILD_INTENT, script.text)
        self.assertIn("default-src 'self'", page.headers["content-security-policy"])
        self.assertIn("form-action 'none'", page.headers["content-security-policy"])
        for item in (response, page, script, styles):
            self.assertEqual(item.headers["cache-control"], "no-store")
            self.assertNotIn("access-control-allow-origin", item.headers)

    def test_host_origin_session_and_csrf_rejections(self) -> None:
        no_session = self.client.get("/api/snapshots")
        self.assertEqual(no_session.status_code, 403)

        bad_hosts = ["evil.example", "127.0.0.1:9999", "localhost:18765", ""]
        for host in bad_hosts:
            with self.subTest(host=host):
                response = self.client.get("/api/status", headers={"Host": host})
                self.assertEqual(response.status_code, 400)
                self.assertIn("Host", response.json()["error"])
        duplicate = self.client.get(
            "/api/status",
            headers=[("Host", AUTHORITY), ("Host", "evil.example")],
        )
        self.assertEqual(duplicate.status_code, 400)

        for origin in ("https://127.0.0.1:18765", "http://evil.example", "null"):
            with self.subTest(origin=origin):
                response = self.client.get(
                    "/api/status", headers={"Origin": origin}
                )
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("access-control-allow-origin", response.headers)

        token = self.bootstrap()
        search_body = {"snapshot_id": "invalid", "query": "evidence"}
        missing_origin = self.client.post(
            "/api/search",
            headers={"X-CSRF-Token": token},
            json=search_body,
        )
        self.assertEqual(missing_origin.status_code, 403)
        missing_csrf = self.client.post(
            "/api/search",
            headers={"Origin": ORIGIN},
            json=search_body,
        )
        self.assertEqual(missing_csrf.status_code, 403)
        wrong_csrf = self.client.post(
            "/api/search",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "0" * 64},
            json=search_body,
        )
        self.assertEqual(wrong_csrf.status_code, 403)

        with TestClient(
            self.app,
            base_url=ORIGIN,
            raise_server_exceptions=False,
        ) as second:
            second_token = self.bootstrap(second)
            self.assertNotEqual(token, second_token)
            crossed = second.post(
                "/api/search",
                headers={"Origin": ORIGIN, "X-CSRF-Token": token},
                json=search_body,
            )
            self.assertEqual(crossed.status_code, 403)

        preflight = self.client.options(
            "/api/build",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(preflight.status_code, 403)
        self.assertNotIn("access-control-allow-origin", preflight.headers)

    def test_two_ports_keep_independent_browser_sessions(self) -> None:
        first_port = 18801
        second_port = 18802
        first_origin = f"http://127.0.0.1:{first_port}"
        second_origin = f"http://127.0.0.1:{second_port}"
        with (
            TestClient(
                create_app(self.root / "first-library", port=first_port),
                base_url=first_origin,
                raise_server_exceptions=False,
            ) as first,
            TestClient(
                create_app(self.root / "second-library", port=second_port),
                base_url=second_origin,
                raise_server_exceptions=False,
            ) as second,
        ):
            self.assertEqual(first.get("/api/status").status_code, 200)
            self.assertEqual(second.get("/api/status").status_code, 200)
            first_cookies = dict(first.cookies)
            second_cookies = dict(second.cookies)
            first_name = f"{SESSION_COOKIE}_{first_port}"
            second_name = f"{SESSION_COOKIE}_{second_port}"
            self.assertIn(first_name, first_cookies)
            self.assertIn(second_name, second_cookies)
            merged = (
                f"{first_name}={first_cookies[first_name]}; "
                f"{second_name}={second_cookies[second_name]}"
            )
            self.assertEqual(
                first.get("/api/snapshots", headers={"Cookie": merged}).status_code,
                200,
            )
            self.assertEqual(
                second.get("/api/snapshots", headers={"Cookie": merged}).status_code,
                200,
            )

    def test_anonymous_status_only_counts_candidates_without_verifying(self) -> None:
        token = self.bootstrap()
        built = self.build_files(
            token,
            [("files", ("safe.md", b"# Safe\n\nstatus evidence\n", "text/markdown"))],
        )
        self.assertEqual(built.status_code, 201, built.text)

        with mock.patch(
            "literature_evidence_mcp.library.verify_snapshot",
            side_effect=AssertionError("status must not verify snapshot contents"),
        ):
            with TestClient(
                self.app,
                base_url=ORIGIN,
                raise_server_exceptions=False,
            ) as anonymous:
                response = anonymous.get("/api/status")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["library"]["available"])
        self.assertEqual(response.json()["library"]["snapshot_candidates"], 1)
        self.assertIsNone(response.json()["library"]["error"])

    def test_explicit_multi_file_build_list_verify_search_and_readonly_hashes(self) -> None:
        token = self.bootstrap()
        markdown = (
            b"# Audit Note\n\n## Entropy\n\n"
            b"Shannon entropy measures local uncertainty.\n"
        )
        pdf = _pdf_bytes(
            [
                "First page voltage evidence.",
                "Second page harmonicpageanchor evidence.",
            ]
        )
        response = self.build_files(
            token,
            [
                ("files", ("original-note.md", markdown, "text/markdown")),
                ("files", ("original-pages.pdf", pdf, "application/pdf")),
            ],
        )
        self.assertEqual(response.status_code, 201, response.text)
        built = response.json()
        self.assertNotIn("snapshot_path", built)
        snapshot_id = built["snapshot_id"]
        snapshot = self.library / "snapshots" / snapshot_id
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {source["source_name"] for source in manifest["sources"]},
            {"original-note.md", "original-pages.pdf"},
        )

        before = _tree_hashes(self.library)
        status = self.client.get("/api/status")
        listed = self.client.get("/api/snapshots")
        verified = self.client.get(f"/api/snapshots/{snapshot_id}/verify")
        searched = self.client.post(
            "/api/search",
            headers=self.post_headers(token),
            json={
                "snapshot_id": snapshot_id,
                "query": "harmonicpageanchor",
                "top_k": 5,
                "excerpt_chars": 1000,
            },
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(searched.status_code, 200)
        self.assertNotIn("snapshot_path", verified.json())
        self.assertNotIn("snapshot_path", listed.text)
        hit = searched.json()["results"][0]
        self.assertEqual(hit["source_name"], "original-pages.pdf")
        self.assertEqual(hit["anchor_label"], "PDF page 2")
        self.assertFalse(hit["fulltext_verified"])
        self.assertFalse(hit["formula_verified"])
        self.assertEqual(before, _tree_hashes(self.library))
        self.assertEqual(_sidecars(self.library), [])

    def test_repeat_build_never_overwrites_first_snapshot(self) -> None:
        token = self.bootstrap()
        files = [("files", ("stable.md", b"# Stable\n\nrepeatable evidence\n", "text/markdown"))]
        first = self.build_files(token, files)
        self.assertEqual(first.status_code, 201, first.text)
        first_payload = first.json()
        first_path = self.library / "snapshots" / first_payload["snapshot_id"]
        first_hashes = _tree_hashes(first_path)

        second = self.build_files(token, files)
        self.assertEqual(second.status_code, 201, second.text)
        second_payload = second.json()
        self.assertNotEqual(first_payload["snapshot_id"], second_payload["snapshot_id"])
        self.assertEqual(first_payload["corpus_sha256"], second_payload["corpus_sha256"])
        self.assertEqual(first_payload["database_sha256"], second_payload["database_sha256"])
        self.assertEqual(first_hashes, _tree_hashes(first_path))
        snapshots = self.client.get("/api/snapshots").json()["snapshots"]
        self.assertEqual(len(snapshots), 2)

    def test_concurrent_build_is_rejected_before_second_upload_is_parsed(self) -> None:
        first_token = self.bootstrap()
        entered = threading.Event()
        release = threading.Event()
        original_build = FixedLibrary.build
        first_result: dict[str, object] = {}

        def blocking_build(instance, controlled_sources):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("test build lock was not released")
            return original_build(instance, controlled_sources)

        def run_first() -> None:
            response = self.build_files(
                first_token,
                [("files", ("first.md", b"# First\n\nlocked build\n", "text/markdown"))],
            )
            first_result["status"] = response.status_code

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
                    [
                        (
                            "files",
                            ("second.md", b"# Second\n\nother build\n", "text/markdown"),
                        )
                    ],
                    client=second,
                )
                self.assertEqual(rejected.status_code, 409, rejected.text)
                release.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(first_result["status"], 201)
        self.assertEqual(len(list((self.library / "snapshots").iterdir())), 1)

    def test_build_requires_post_multipart_csrf_and_explicit_intent(self) -> None:
        token = self.bootstrap()
        get_build = self.client.get("/api/build")
        self.assertEqual(get_build.status_code, 405)

        no_intent = self.build_files(
            token,
            [("files", ("note.md", b"local text", "text/markdown"))],
            intent=None,
        )
        self.assertEqual(no_intent.status_code, 400)
        self.assertIn("明确点击", no_intent.json()["error"])

        path_json = self.client.post(
            "/api/build",
            headers={**self.post_headers(token), "X-Build-Intent": BUILD_INTENT},
            json={"paths": ["/etc/passwd"]},
        )
        self.assertEqual(path_json.status_code, 415)
        self.assertFalse(self.library.exists())

        with mock.patch(
            "literature_evidence_mcp.web.tempfile.TemporaryDirectory"
        ) as temporary_directory:
            rejected_before_parse = self.client.post(
                "/api/build",
                headers={
                    "Host": "evil.example",
                    "Origin": ORIGIN,
                    "X-CSRF-Token": token,
                    "X-Build-Intent": BUILD_INTENT,
                },
                files=[("files", ("note.md", b"text", "text/markdown"))],
            )
            self.assertEqual(rejected_before_parse.status_code, 400)
            temporary_directory.assert_not_called()

        with self.assertLogs("python_multipart.multipart", level="WARNING"):
            malformed = self.client.post(
                "/api/build",
                headers={
                    **self.post_headers(token),
                    "X-Build-Intent": BUILD_INTENT,
                    "Content-Type": "multipart/form-data; boundary=expected",
                },
                content=b"--different\r\ninvalid multipart\r\n--different--\r\n",
            )
        self.assertEqual(malformed.status_code, 400, malformed.text)
        self.assertIn("multipart", malformed.json()["error"])
        self.assertNotIn("Traceback", malformed.text)
        self.assertFalse(self.library.exists())

    def test_unsafe_filenames_types_and_small_resource_limits_are_rejected(self) -> None:
        limits = UploadLimits(
            max_files=2,
            max_file_bytes=8,
            max_total_bytes=12,
            max_request_bytes=2048,
            max_filename_bytes=20,
            max_json_bytes=128,
        )
        small_library = self.root / "small-library"
        with TestClient(
            create_app(small_library, port=PORT, upload_limits=limits),
            base_url=ORIGIN,
            raise_server_exceptions=False,
        ) as client:
            token = self.bootstrap(client)

            invalid_cases = [
                ("../escape.md", b"text", "text/markdown"),
                ("folder\\escape.md", b"text", "text/markdown"),
                ("unsupported.txt", b"text", "text/plain"),
                ("name-with-too-many-bytes.md", b"text", "text/markdown"),
            ]
            for filename, payload, media_type in invalid_cases:
                with self.subTest(filename=filename):
                    response = self.build_files(
                        token,
                        [("files", (filename, payload, media_type))],
                        client=client,
                    )
                    self.assertEqual(response.status_code, 400, response.text)

            duplicate = self.build_files(
                token,
                [
                    ("files", ("A.md", b"text", "text/markdown")),
                    ("files", ("a.md", b"text", "text/markdown")),
                ],
                client=client,
            )
            self.assertEqual(duplicate.status_code, 400)

            per_file = self.build_files(
                token,
                [("files", ("large.md", b"123456789", "text/markdown"))],
                client=client,
            )
            self.assertEqual(per_file.status_code, 413)

            exact_file = self.build_files(
                token,
                [("files", ("exact.md", b"# A\n\nxy\n", "text/markdown"))],
                client=client,
            )
            self.assertEqual(exact_file.status_code, 201, exact_file.text)

            exact_total = self.build_files(
                token,
                [
                    ("files", ("one.md", b"# A\nx\n", "text/markdown")),
                    ("files", ("two.md", b"# B\ny\n", "text/markdown")),
                ],
                client=client,
            )
            self.assertEqual(exact_total.status_code, 201, exact_total.text)
            count_before = len(list((small_library / "snapshots").iterdir()))

            oversized_json = client.post(
                "/api/search",
                headers=self.post_headers(token),
                json={
                    "snapshot_id": exact_total.json()["snapshot_id"],
                    "query": "x" * 200,
                },
            )
            self.assertEqual(oversized_json.status_code, 413)

            total_over = self.build_files(
                token,
                [
                    ("files", ("one.md", b"123456", "text/markdown")),
                    ("files", ("two.md", b"1234567", "text/markdown")),
                ],
                client=client,
            )
            self.assertEqual(total_over.status_code, 413)

            too_many = self.build_files(
                token,
                [
                    ("files", ("a.md", b"text", "text/markdown")),
                    ("files", ("b.md", b"text", "text/markdown")),
                    ("files", ("c.md", b"text", "text/markdown")),
                ],
                client=client,
            )
            self.assertEqual(too_many.status_code, 413, too_many.text)
            self.assertEqual(
                len(list((small_library / "snapshots").iterdir())), count_before
            )
            self.assertEqual(_sidecars(small_library), [])

            understated = client.post(
                "/api/build",
                headers={
                    **self.post_headers(token),
                    "X-Build-Intent": BUILD_INTENT,
                    "Content-Type": "multipart/form-data; boundary=x",
                    "Content-Length": "1",
                },
                content=b"x" * 3000,
            )
            self.assertEqual(understated.status_code, 413, understated.text)
            self.assertIn("x-frame-options", understated.headers)

    def test_failed_build_cleans_upload_building_and_sqlite_sidecars(self) -> None:
        token = self.bootstrap()
        first = self.build_files(
            token,
            [("files", ("kept.md", b"# Kept\n\nold evidence\n", "text/markdown"))],
        )
        self.assertEqual(first.status_code, 201)
        before = _tree_hashes(self.library)
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
                    [
                        (
                            "files",
                            ("failing.md", b"# Fail\n\nnew evidence\n", "text/markdown"),
                        )
                    ],
                )
        self.assertEqual(failed.status_code, 400, failed.text)
        self.assertNotIn("/private/secret", failed.text)
        self.assertNotIn("Traceback", failed.text)
        self.assertEqual(before, _tree_hashes(self.library))
        self.assertEqual(list(upload_parent.iterdir()), [])
        snapshots = self.library / "snapshots"
        self.assertEqual(
            [path for path in snapshots.iterdir() if path.name.startswith(".building-")],
            [],
        )
        self.assertEqual(_sidecars(self.library), [])

    def test_fixed_library_listing_rejects_paths_and_skips_unsafe_children(self) -> None:
        token = self.bootstrap()
        built = self.build_files(
            token,
            [("files", ("safe.md", b"# Safe\n\nlisted evidence\n", "text/markdown"))],
        )
        self.assertEqual(built.status_code, 201)
        safe_id = built.json()["snapshot_id"]
        snapshots = self.library / "snapshots"
        (snapshots / ".building-leftover").mkdir()
        (snapshots / "invalid-name").mkdir()
        unicode_digits_id = "٠" * 8 + "T" + "٠" * 12 + "Z-aaaaaaaaaaaa-00000000"
        (snapshots / unicode_digits_id).mkdir()
        regular_id = "20260101T000000000000Z-aaaaaaaaaaaa-11111111"
        (snapshots / regular_id).write_text("not a directory", encoding="utf-8")
        external = self.root / "external"
        external.mkdir()
        symlink_id = "20260101T000000000000Z-bbbbbbbbbbbb-22222222"
        (snapshots / symlink_id).symlink_to(external, target_is_directory=True)

        listed = self.client.get("/api/snapshots")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [item["snapshot_id"] for item in listed.json()["snapshots"]],
            [safe_id],
        )

        invalid = self.client.get("/api/snapshots/not-a-snapshot/verify")
        self.assertEqual(invalid.status_code, 400)
        nonexistent_id = "20260101T000000000000Z-cccccccccccc-33333333"
        missing = self.client.get(f"/api/snapshots/{nonexistent_id}/verify")
        self.assertEqual(missing.status_code, 404)
        traversal = self.client.get("/api/snapshots/%2E%2E/verify")
        self.assertIn(traversal.status_code, {400, 404})

        extra_path = self.client.post(
            "/api/search",
            headers=self.post_headers(token),
            json={
                "snapshot_id": safe_id,
                "query": "evidence",
                "snapshot_path": "/etc",
            },
        )
        self.assertEqual(extra_path.status_code, 400)

    def test_symlink_library_and_snapshots_root_are_never_followed(self) -> None:
        real_root = self.root / "real-root"
        real_root.mkdir()
        linked_root = self.root / "linked-root"
        linked_root.symlink_to(real_root, target_is_directory=True)
        with self.assertRaisesRegex(ImportPolicyError, "符号链接"):
            create_app(linked_root, port=PORT)

        bad_library = self.root / "bad-library"
        bad_library.mkdir()
        external = self.root / "outside-snapshots"
        external.mkdir()
        (bad_library / "snapshots").symlink_to(external, target_is_directory=True)
        with TestClient(
            create_app(bad_library, port=PORT),
            base_url=ORIGIN,
            raise_server_exceptions=False,
        ) as client:
            self.bootstrap(client)
            response = client.get("/api/snapshots")
            self.assertEqual(response.status_code, 400)
            self.assertIn("符号链接", response.json()["error"])
        self.assertEqual(list(external.iterdir()), [])

    def test_search_parameter_boundaries_and_continuous_chinese_hint(self) -> None:
        token = self.bootstrap()
        built = self.build_files(
            token,
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
        self.assertEqual(built.status_code, 201)
        snapshot_id = built.json()["snapshot_id"]

        def search(payload: dict[str, object]):
            return self.client.post(
                "/api/search",
                headers=self.post_headers(token),
                json={"snapshot_id": snapshot_id, **payload},
            )

        valid_edges = [
            {"query": "x", "top_k": 1, "excerpt_chars": 1},
            {"query": "x" * 400, "top_k": 10, "excerpt_chars": 1200},
        ]
        for payload in valid_edges:
            with self.subTest(valid=payload):
                self.assertEqual(search(payload).status_code, 200)

        invalid_payloads = [
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
        ]
        for payload in invalid_payloads:
            with self.subTest(invalid=payload):
                self.assertEqual(search(payload).status_code, 400)

        continuous = search({"query": "配电网谐波监测"})
        self.assertEqual(continuous.status_code, 200)
        self.assertFalse(continuous.json()["found"])
        self.assertEqual(continuous.json()["results"], [])
        self.assertIn("加空格", continuous.json()["input_hint"])


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
                            "--library",
                            str(Path(raw) / "library"),
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
                cli_main(["serve", "--library", "/tmp/fixed-library"]), 130
            )

    def test_real_uvicorn_start_stop_releases_port(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            self.assertGreaterEqual(port, 1024)

            app = create_app(Path(raw) / "library", port=port)
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
                    serve_local(Path(raw) / "library", port=port)
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
