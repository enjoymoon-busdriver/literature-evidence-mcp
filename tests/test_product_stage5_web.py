from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from literature_evidence_mcp.mcp_server import TOOLS
from literature_evidence_mcp.web import create_app


PORT = 19125
ORIGIN = f"http://127.0.0.1:{PORT}"
_LIBRARY_ID = re.compile(r"\Alib_[0-9a-f]{32}\Z")


class ProductStageFiveWebTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lemcp-product-stage5-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.application_root = self.root / "application"
        self.client = TestClient(
            create_app(self.application_root, port=PORT),
            base_url=ORIGIN,
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        status = self.client.get("/api/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.csrf = status.json()["csrf_token"]

    def _headers(self, intent: str) -> dict[str, str]:
        return {
            "Origin": ORIGIN,
            "X-CSRF-Token": self.csrf,
            "X-Action-Intent": intent,
        }

    def _create(self, name: str, description: str) -> dict[str, object]:
        response = self.client.post(
            "/api/libraries",
            headers={**self._headers("create-library"), "Content-Type": "application/json"},
            content=json.dumps({"name": name, "description": description}),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["library"]

    def _build(
        self,
        library_id: str,
        files: list[tuple[str, bytes]],
        *,
        mode: str,
        base_snapshot_id: str | None = None,
    ):
        data = {"build_mode": mode}
        if base_snapshot_id is not None:
            data["base_snapshot_id"] = base_snapshot_id
        return self.client.post(
            f"/api/libraries/{library_id}/build",
            headers=self._headers("build-snapshot"),
            data=data,
            files=[
                ("files", (name, payload, "text/markdown"))
                for name, payload in files
            ],
        )

    def _snapshots(self, library_id: str) -> dict[str, object]:
        response = self.client.get(f"/api/libraries/{library_id}/snapshots")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _search(self, library_id: str, snapshot_id: str, query: str):
        return self.client.post(
            f"/api/libraries/{library_id}/search",
            headers={**self._headers("search"), "Content-Type": "application/json"},
            content=json.dumps(
                {"snapshot_id": snapshot_id, "query": query, "top_k": 5}
            ),
        )

    def test_blank_to_two_libraries_incremental_failure_activation_and_ui_contract(
        self,
    ) -> None:
        self.assertFalse(self.application_root.exists())
        listed_empty = self.client.get("/api/libraries")
        self.assertEqual(listed_empty.json(), {"libraries": []})
        self.assertFalse(self.application_root.exists())

        no_intent = self.client.post(
            "/api/libraries",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": self.csrf,
                "Content-Type": "application/json",
            },
            content='{"name":"拒绝"}',
        )
        self.assertEqual(no_intent.status_code, 400)
        no_origin = self.client.post(
            "/api/libraries",
            headers={
                "X-CSRF-Token": self.csrf,
                "X-Action-Intent": "create-library",
                "Content-Type": "application/json",
            },
            content='{"name":"拒绝"}',
        )
        self.assertEqual(no_origin.status_code, 403)

        first_library = self._create("热力学资料", "第一套合成证据")
        second_library = self._create("隔离资料", "第二套同名文件测试")
        first_id = str(first_library["library_id"])
        second_id = str(second_library["library_id"])
        self.assertRegex(first_id, _LIBRARY_ID)
        self.assertRegex(second_id, _LIBRARY_ID)
        self.assertNotEqual(first_id, second_id)

        libraries = self.client.get("/api/libraries").json()["libraries"]
        self.assertEqual([item["name"] for item in libraries], ["热力学资料", "隔离资料"])
        self.assertEqual(
            [item["description"] for item in libraries],
            ["第一套合成证据", "第二套同名文件测试"],
        )
        serialized = json.dumps(libraries, ensure_ascii=False)
        self.assertNotIn("library_root", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertTrue(libraries[0]["selected"])

        switched = self.client.post(
            f"/api/libraries/{second_id}/select",
            headers=self._headers("select-library"),
        )
        self.assertEqual(switched.status_code, 200, switched.text)
        selected = self.client.get("/api/libraries").json()["libraries"]
        self.assertEqual(
            [item["library_id"] for item in selected if item["selected"]],
            [second_id],
        )

        a = b"# Alpha\n\nalpha_marker local evidence.\n"
        b = b"# Beta\n\nbeta_marker local evidence.\n"
        c = b"# Gamma\n\ngamma_marker local evidence.\n"

        missing_mode = self.client.post(
            f"/api/libraries/{first_id}/build",
            headers=self._headers("build-snapshot"),
            files=[("files", ("A.md", a, "text/markdown"))],
        )
        self.assertEqual(missing_mode.status_code, 400)
        self.assertFalse(missing_mode.json().get("published", False))
        self.assertEqual(
            missing_mode.json()["files"][0]["state"],
            "not_published",
        )

        early_failure = self._build(
            first_id,
            [
                ("good-before-bad.md", b"# Good\n\ngood before bad.\n"),
                ("bad.txt", b"unsupported extension"),
            ],
            mode="blank",
        )
        self.assertEqual(early_failure.status_code, 400, early_failure.text)
        early_files = early_failure.json()["files"]
        self.assertEqual(
            [(item["name"], item["state"]) for item in early_files],
            [
                ("good-before-bad.md", "not_published"),
                ("bad.txt", "failed"),
            ],
        )
        self.assertEqual(self._snapshots(first_id)["snapshots"], [])

        first = self._build(
            first_id,
            [("A.md", a), ("B.md", b)],
            mode="blank",
        )
        self.assertEqual(first.status_code, 201, first.text)
        first_payload = first.json()
        first_snapshot = first_payload["snapshot_id"]
        self.assertTrue(first_payload["published"])
        self.assertEqual(first_payload["library_id"], first_id)
        self.assertEqual(
            [item["stage"] for item in first_payload["files"]],
            ["已进入成功快照", "已进入成功快照"],
        )
        self.assertEqual(
            first_payload["difference"]["counts"],
            {"added": 2, "inherited": 0, "replaced": 0, "removed": 0},
        )
        self.assertEqual(
            {item["source_name"] for item in first_payload["members"]},
            {"A.md", "B.md"},
        )
        self.assertEqual(
            {
                key: first_payload["storage"][key]
                for key in ("object_references", "new_objects", "reused_objects")
            },
            {"object_references": 6, "new_objects": 6, "reused_objects": 0},
        )

        second = self._build(
            first_id,
            [("C.md", c)],
            mode="inherit",
            base_snapshot_id=first_snapshot,
        )
        self.assertEqual(second.status_code, 201, second.text)
        second_payload = second.json()
        second_snapshot = second_payload["snapshot_id"]
        self.assertEqual(
            second_payload["difference"]["counts"],
            {"added": 1, "inherited": 2, "replaced": 0, "removed": 0},
        )
        self.assertEqual(
            {item["source_name"] for item in second_payload["members"]},
            {"A.md", "B.md", "C.md"},
        )
        self.assertEqual(
            {
                key: second_payload["storage"][key]
                for key in ("object_references", "new_objects", "reused_objects")
            },
            {"object_references": 9, "new_objects": 3, "reused_objects": 6},
        )
        self.assertEqual(
            second_payload["storage"]["logical_object_bytes"],
            second_payload["storage"]["new_object_bytes"]
            + second_payload["storage"]["reused_object_bytes"],
        )

        first_state = self._snapshots(first_id)
        self.assertEqual(first_state["current_snapshot_id"], first_snapshot)
        self.assertEqual(first_state["last_successful_snapshot_id"], second_snapshot)
        self.assertEqual(len(first_state["snapshots"]), 2)
        for marker in ("alpha_marker", "beta_marker", "gamma_marker"):
            searched = self._search(first_id, second_snapshot, marker)
            self.assertEqual(searched.status_code, 200, searched.text)
            self.assertTrue(searched.json()["found"])
            self.assertEqual(searched.json()["retrieval_mode"], "bm25")
        enhanced = self.client.post(
            f"/api/libraries/{first_id}/search",
            headers={**self._headers("search"), "Content-Type": "application/json"},
            content=json.dumps(
                {
                    "snapshot_id": second_snapshot,
                    "query": "alpha_marker",
                    "mode": "enhanced",
                }
            ),
        )
        self.assertEqual(enhanced.status_code, 503, enhanced.text)
        enhanced_payload = enhanced.json()
        self.assertIn("尚未配置", enhanced_payload["error"])
        self.assertEqual(enhanced_payload["mode"], "enhanced")
        self.assertEqual(
            enhanced_payload["audit"],
            {"simulated": None, "call_count": 0, "calls": []},
        )
        status = self.client.get("/api/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertIs(status.json()["enhanced_available"], False)

        isolated = self._build(second_id, [("A.md", a)], mode="blank")
        self.assertEqual(isolated.status_code, 201, isolated.text)
        isolated_payload = isolated.json()
        self.assertEqual(isolated_payload["storage"]["new_objects"], 3)
        self.assertEqual(isolated_payload["storage"]["reused_objects"], 0)
        no_gamma = self._search(second_id, isolated_payload["snapshot_id"], "gamma_marker")
        self.assertEqual(no_gamma.status_code, 200, no_gamma.text)
        self.assertFalse(no_gamma.json()["found"])
        cross_library = self.client.get(
            f"/api/libraries/{second_id}/snapshots/{second_snapshot}/verify"
        )
        self.assertEqual(cross_library.status_code, 404)
        self.assertEqual(self.client.get(f"/api/libraries/{first_id}/snapshots").status_code, 200)
        self.assertEqual(self.client.get("/api/snapshots").status_code, 404)

        before_failure = self._snapshots(first_id)
        failure = self._build(
            first_id,
            [("good.md", b"# Good\n\ngood_marker evidence.\n"), ("bad.md", b"\xff\xfe")],
            mode="inherit",
            base_snapshot_id=second_snapshot,
        )
        self.assertEqual(failure.status_code, 422, failure.text)
        failure_payload = failure.json()
        self.assertFalse(failure_payload["published"])
        self.assertNotIn("snapshot_id", failure_payload)
        failure_files = {item["name"]: item for item in failure_payload["files"]}
        self.assertEqual(failure_files["bad.md"]["stage"], "失败、未发布")
        self.assertIn("UTF-8", failure_files["bad.md"]["error"])
        self.assertIn("未发布", failure_files["good.md"]["stage"])
        self.assertNotEqual(failure_files["good.md"]["state"], "published")
        after_failure = self._snapshots(first_id)
        self.assertEqual(
            (
                after_failure["current_snapshot_id"],
                after_failure["last_successful_snapshot_id"],
                [item["snapshot_id"] for item in after_failure["snapshots"]],
            ),
            (
                before_failure["current_snapshot_id"],
                before_failure["last_successful_snapshot_id"],
                [item["snapshot_id"] for item in before_failure["snapshots"]],
            ),
        )
        self.assertTrue(self._search(first_id, first_snapshot, "alpha_marker").json()["found"])

        no_activate_intent = self.client.post(
            f"/api/libraries/{first_id}/snapshots/{second_snapshot}/activate",
            headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(no_activate_intent.status_code, 400)
        activated = self.client.post(
            f"/api/libraries/{first_id}/snapshots/{second_snapshot}/activate",
            headers=self._headers("activate-snapshot"),
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(activated.json()["library_id"], first_id)
        activated_state = self._snapshots(first_id)
        self.assertEqual(activated_state["current_snapshot_id"], second_snapshot)
        self.assertEqual(activated_state["last_successful_snapshot_id"], second_snapshot)
        self.assertTrue(all(item["verified"] for item in activated_state["snapshots"]))

        page = self.client.get("/")
        script = self.client.get("/static/app.js")
        self.assertIn('id="drop-zone"', page.text)
        self.assertIn('type="file"', page.text)
        self.assertIn('accept=".md,.markdown,.pdf,text/markdown,application/pdf"', page.text)
        for label in (
            "待提交",
            "本机接收 / 构建中",
            "已进入成功快照",
            "失败、未发布",
            "current",
            "last-successful",
        ):
            self.assertIn(label, page.text + script.text)
        self.assertIn("textContent", script.text)
        self.assertIn("createElement", script.text)
        self.assertIn("resetLibraryContext", script.text)
        self.assertIn("libraryContextIsCurrent", script.text)
        self.assertIn("requestedLibraryId", script.text)
        self.assertIn("requestedEpoch", script.text)
        self.assertIn('elements.files.value = ""', script.text)
        self.assertIn("cleanup_warning", script.text)
        load_libraries = script.text.split(
            'async function loadLibraries(candidateId = "") {',
            1,
        )[1].split("async function createLibrary()", 1)[0]
        self.assertIn("const requestId = ++state.libraryRequestId;", load_libraries)
        stale_guard = "if (requestId !== state.libraryRequestId)"
        self.assertIn(stale_guard, load_libraries)
        self.assertLess(
            load_libraries.index(stale_guard),
            load_libraries.index("state.libraries = payload.libraries;"),
        )
        switch_library = script.text.split(
            "async function switchLibrary() {",
            1,
        )[1].split("function badge(", 1)[0]
        self.assertEqual(switch_library.count("loadLibraries(libraryId)"), 1)
        self.assertIn("if (!(await loadLibraries(libraryId)))", switch_library)
        load_snapshots = script.text.split(
            'async function loadSnapshots(preferredId = "") {',
            1,
        )[1].split("async function verifySnapshot(", 1)[0]
        self.assertIn("const requestId = ++state.snapshotRequestId;", load_snapshots)
        self.assertIn(
            "const previousSnapshotId = elements.snapshotSelect.value;",
            load_snapshots,
        )
        snapshot_guard = "requestId !== state.snapshotRequestId"
        self.assertIn(snapshot_guard, load_snapshots)
        self.assertLess(
            load_snapshots.index(snapshot_guard),
            load_snapshots.index("state.snapshots = payload.snapshots;"),
        )
        snapshot_change = (
            "if (elements.snapshotSelect.value !== previousSnapshotId)"
        )
        self.assertIn(snapshot_change, load_snapshots)
        self.assertLess(
            load_snapshots.index("elements.snapshotSelect.value = preferredId;"),
            load_snapshots.index(snapshot_change),
        )
        self.assertIn("invalidateSearch();", load_snapshots)
        choose_files = script.text.split("function chooseFiles(fileList) {", 1)[1].split(
            "function renderSelectedFiles()",
            1,
        )[0]
        self.assertLess(
            choose_files.index('elements.files.value = "";'),
            choose_files.index("state.files = files;"),
        )
        search_function = script.text.split("async function search() {", 1)[1].split(
            "async function refreshAll()",
            1,
        )[0]
        self.assertIn("const requestId = ++state.searchRequestId;", search_function)
        self.assertEqual(search_function.count("searchRequestIsCurrent("), 3)
        self.assertIn(
            'elements.query.addEventListener("input", invalidateSearch);',
            script.text,
        )
        self.assertIn(
            'elements.snapshotSelect.addEventListener("change", invalidateSearch);',
            script.text,
        )
        self.assertNotIn("innerHTML", script.text)
        self.assertNotIn("insertAdjacentHTML", script.text)
        self.assertNotIn("https://", page.text + script.text)
        self.assertIn('id="search-mode"', page.text)
        self.assertIn("BM25（离线、零密钥、零模型调用）", page.text)
        self.assertIn("增强搜索（当前不可用）", page.text)
        self.assertIn("第 1 次发送原问题做改写", page.text)
        self.assertIn("第 2 次发送改写问题取得查询向量", page.text)
        self.assertIn(
            "第 3 次只发送所选快照中本地向量召回的有界候选 ID",
            page.text,
        )
        self.assertIn("增强搜索尚未配置或当前不可用", page.text + script.text)

        self.assertEqual(len(TOOLS), 8)
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
        for tool in TOOLS:
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertEqual(
                tool.annotations.open_world_hint,
                tool.name == "search_documents",
            )


if __name__ == "__main__":
    unittest.main()
