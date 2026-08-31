from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import literature_evidence_mcp.quality_eval as quality_eval
from literature_evidence_mcp.errors import SnapshotError
from literature_evidence_mcp.library import FixedLibrary
from literature_evidence_mcp.mcp_server import TOOLS
from literature_evidence_mcp.quality_eval import main, run_evaluation


def _business_tree_identity(root: Path) -> dict[str, str]:
    identity: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".git"
        ):
            continue
        identity[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return identity


class ProductStageSevenQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_root = Path(__file__).resolve().parents[1]
        cls.before_tree = _business_tree_identity(cls.repository_root)
        with mock.patch(
            "literature_evidence_mcp.registry.default_application_root",
            side_effect=AssertionError("evaluation must not use the user application root"),
        ), mock.patch(
            "socket.socket",
            side_effect=AssertionError("evaluation must not open a network socket"),
        ):
            cls.report = run_evaluation()
        cls.after_tree = _business_tree_identity(cls.repository_root)

    def _case(self, case_id: str) -> dict[str, object]:
        return next(
            case for case in self.report["cases"] if case["case_id"] == case_id
        )

    def test_report_boundary_dataset_and_repository_are_stable(self) -> None:
        report = self.report
        self.assertTrue(report["passed"])
        self.assertEqual(report["evidence_level"], "offline_simulated")
        self.assertEqual(report["real_model_calls"], 0)
        self.assertEqual(report["network_calls"], 0)
        self.assertIn("不能推出真实模型质量提升", report["claim_boundary"])
        self.assertIn("不等于预期文档命中", report["user_explanation"]["case_pass_hint_zh"])
        self.assertEqual(report["dataset"]["library_count"], 2)
        self.assertEqual(report["dataset"]["snapshot_count"], 3)
        self.assertEqual(report["dataset"]["logical_question_count"], 12)
        self.assertEqual(report["dataset"]["case_count"], 24)
        self.assertEqual(report["dataset"]["top_k"], 5)
        self.assertEqual(
            {case["mode"] for case in report["cases"]}, {"bm25", "enhanced"}
        )
        self.assertEqual(self.before_tree, self.after_tree)

        serialized = json.dumps(report, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("lemcp-stage7-offline-", serialized)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("library_root", serialized)
        self.assertNotIn("chunk_text", serialized)
        for case in report["cases"]:
            self.assertTrue(case["q0"])
            self.assertTrue(case["selection"]["library_id"])
            self.assertTrue(case["selection"]["snapshot_id"])
            self.assertIn("empty", case["expected"])
            self.assertIsInstance(case["observed"]["results"], list)
            self.assertEqual(case["observed"]["status"], "ok")
            self.assertEqual(
                case["observed"]["library_id"], case["selection"]["library_id"]
            )
            self.assertEqual(
                case["observed"]["snapshot_id"], case["selection"]["snapshot_id"]
            )

    def test_modes_calls_metrics_and_repeatability(self) -> None:
        bm25 = [case for case in self.report["cases"] if case["mode"] == "bm25"]
        enhanced = [
            case for case in self.report["cases"] if case["mode"] == "enhanced"
        ]
        self.assertEqual(len(bm25), 12)
        self.assertEqual(len(enhanced), 12)
        self.assertTrue(
            all(
                case["call_count"] == 0 and case["simulated"] is False
                for case in bm25
            )
        )
        self.assertTrue(
            all(
                case["call_count"] == 3
                and case["simulated"] is True
                and case["transport_order_ok"] is True
                for case in enhanced
            )
        )

        bm25_metrics = self.report["metrics"]["bm25"]
        enhanced_metrics = self.report["metrics"]["enhanced"]
        self.assertEqual(bm25_metrics["positive_hit_at_k"], 0.7)
        self.assertEqual(enhanced_metrics["positive_hit_at_k"], 1.0)
        for metrics in (bm25_metrics, enhanced_metrics):
            self.assertEqual(metrics["contract_passed_case_count"], 12)
            self.assertEqual(metrics["true_empty_accuracy"], 1.0)
            self.assertEqual(metrics["library_isolation_violations"], 0)
            self.assertEqual(metrics["snapshot_isolation_violations"], 0)
            self.assertEqual(metrics["traceability_coverage"], 1.0)

        again = run_evaluation()
        self.assertEqual(self.report["metrics"], again["metrics"])
        self.assertEqual(
            [
                (
                    case["case_id"],
                    case["route"],
                    case["metric_hit"],
                    case["case_pass"],
                )
                for case in self.report["cases"]
            ],
            [
                (
                    case["case_id"],
                    case["route"],
                    case["metric_hit"],
                    case["case_pass"],
                )
                for case in again["cases"]
            ],
        )

    def test_negative_isolation_cross_language_and_traceability_cases(self) -> None:
        for prefix in ("SNAPSHOT-EMPTY", "TRUE-EMPTY"):
            for mode in ("BM25", "ENHANCED"):
                case = self._case(f"{prefix}-{mode}")
                self.assertTrue(case["expected"]["empty"])
                self.assertFalse(case["observed"]["found"])
                self.assertEqual(case["observed"]["results"], [])

        for prefix in ("LIB-A", "LIB-B", "SNAPSHOT-S2"):
            for mode in ("BM25", "ENHANCED"):
                case = self._case(f"{prefix}-{mode}")
                self.assertEqual(case["library_isolation_violations"], 0)
                self.assertEqual(case["snapshot_isolation_violations"], 0)

        for prefix in ("CROSS-ZH-EN", "CROSS-EN-ZH", "LEXICAL-HARD"):
            bm25 = self._case(f"{prefix}-BM25")
            enhanced = self._case(f"{prefix}-ENHANCED")
            self.assertEqual(
                bm25["acceptance"], "observational_known_bm25_limitation"
            )
            self.assertIsNotNone(bm25["known_limitation"])
            self.assertFalse(bm25["metric_hit"])
            self.assertTrue(enhanced["metric_hit"])

        for prefix in ("TRACE-MD", "TRACE-PDF"):
            for mode in ("BM25", "ENHANCED"):
                case = self._case(f"{prefix}-{mode}")
                expected = case["expected"]
                result = next(
                    item
                    for item in case["observed"]["results"]
                    if item["chunk_id"] == expected["chunk_id"]
                )
                self.assertEqual(
                    {key: result[key] for key in expected["trace"]},
                    expected["trace"],
                )

    def test_wrong_route_identity_fails_even_for_shared_or_empty_results(self) -> None:
        original_bm25 = quality_eval._bm25_search
        original_search = FixedLibrary.search

        def other_library(snapshot: object) -> Path:
            return next(
                path
                for path in snapshot.library_root.parent.iterdir()
                if path.is_dir() and path.name != snapshot.library_id
            )

        def misroute_bm25(snapshot: object, query: str) -> tuple[dict, str]:
            if query == "alpha_library_marker shared calibration":
                library = FixedLibrary(snapshot.library_root)
                requested = next(
                    item
                    for item in library.list_snapshots()
                    if item["snapshot_id"] == snapshot.snapshot_id
                )
                wrong_snapshot_id = requested["base_snapshot_id"]
                self.assertIsNotNone(wrong_snapshot_id)
            elif query == "no_such_evidence_7f3a9d":
                library = FixedLibrary(other_library(snapshot))
                wrong_snapshot_id = library.list_snapshots()[0]["snapshot_id"]
            else:
                return original_bm25(snapshot, query)
            return (
                original_search(
                    library,
                    wrong_snapshot_id,
                    query,
                    top_k=5,
                    excerpt_chars=240,
                ),
                library._root.name,
            )

        def misroute_enhanced(
            library: FixedLibrary, snapshot_id: str, query: str, **kwargs: object
        ) -> dict:
            if kwargs.get("mode") != "enhanced":
                return original_search(library, snapshot_id, query, **kwargs)
            if query == "alpha_library_marker shared calibration":
                requested = next(
                    item
                    for item in library.list_snapshots()
                    if item["snapshot_id"] == snapshot_id
                )
                return original_search(
                    library, requested["base_snapshot_id"], query, **kwargs
                )
            elif query == "no_such_evidence_7f3a9d":
                root = next(
                    path
                    for path in library._root.parent.iterdir()
                    if path.is_dir() and path.name != library._root.name
                )
                wrong = FixedLibrary(
                    root,
                    library_id=root.name,
                    enhanced_search=library._enhanced_search,
                )
                wrong_snapshot_id = wrong.list_snapshots()[0]["snapshot_id"]
                return original_search(wrong, wrong_snapshot_id, query, **kwargs)
            return original_search(library, snapshot_id, query, **kwargs)

        with mock.patch.object(
            quality_eval, "_bm25_search", side_effect=misroute_bm25
        ), mock.patch.object(
            FixedLibrary, "search", autospec=True, side_effect=misroute_enhanced
        ):
            report = run_evaluation()

        self.assertFalse(report["passed"])
        for case_id in ("LIB-A-BM25", "LIB-A-ENHANCED"):
            case = next(case for case in report["cases"] if case["case_id"] == case_id)
            self.assertGreater(case["snapshot_isolation_violations"], 0)
            self.assertFalse(case["case_pass"])
        for case_id in ("TRUE-EMPTY-BM25", "TRUE-EMPTY-ENHANCED"):
            case = next(case for case in report["cases"] if case["case_id"] == case_id)
            self.assertFalse(case["observed"]["found"])
            self.assertEqual(case["observed"]["results"], [])
            self.assertGreater(case["library_isolation_violations"], 0)
            self.assertGreater(case["snapshot_isolation_violations"], 0)
            self.assertFalse(case["case_pass"])

    def test_exact_trace_tampering_fails(self) -> None:
        original_search = FixedLibrary.search

        def tamper_trace(
            library: FixedLibrary, snapshot_id: str, query: str, **kwargs: object
        ) -> dict:
            result = copy.deepcopy(
                original_search(library, snapshot_id, query, **kwargs)
            )
            if query == "markdown_anchor_marker provenance chain":
                for item in result["results"]:
                    item["source_line_start"] = 1
                    item["source_line_end"] = 1
            elif query == "pdf_page_marker traceable page":
                for item in result["results"]:
                    item["pdf_page_start"] = 9999
                    item["pdf_page_end"] = 9999
            return result

        with mock.patch.object(
            FixedLibrary, "search", autospec=True, side_effect=tamper_trace
        ):
            report = run_evaluation()

        self.assertFalse(report["passed"])
        for prefix in ("TRACE-MD", "TRACE-PDF"):
            for mode in ("BM25", "ENHANCED"):
                case = next(
                    case
                    for case in report["cases"]
                    if case["case_id"] == f"{prefix}-{mode}"
                )
                self.assertFalse(case["traceability_pass"])
                self.assertFalse(case["case_pass"])

    def test_snapshot_errors_are_not_counted_as_true_empty(self) -> None:
        original_search = FixedLibrary.search

        def fail_negatives(
            library: FixedLibrary, snapshot_id: str, query: str, **kwargs: object
        ) -> dict:
            is_old_snapshot = False
            if query == "snapshot_new_marker":
                requested = next(
                    item
                    for item in library.list_snapshots()
                    if item["snapshot_id"] == snapshot_id
                )
                is_old_snapshot = requested["base_snapshot_id"] is None
            if query == "no_such_evidence_7f3a9d" or is_old_snapshot:
                raise SnapshotError("synthetic injected snapshot failure")
            return original_search(library, snapshot_id, query, **kwargs)

        with mock.patch.object(
            FixedLibrary, "search", autospec=True, side_effect=fail_negatives
        ):
            report = run_evaluation()

        self.assertFalse(report["passed"])
        for mode in ("bm25", "enhanced"):
            self.assertLess(report["metrics"][mode]["true_empty_accuracy"], 1.0)
        for prefix in ("SNAPSHOT-EMPTY", "TRUE-EMPTY"):
            for mode in ("BM25", "ENHANCED"):
                case = next(
                    case
                    for case in report["cases"]
                    if case["case_id"] == f"{prefix}-{mode}"
                )
                self.assertEqual(case["observed"]["status"], "error")
                self.assertIsNone(case["observed"]["found"])
                self.assertFalse(case["case_pass"])

    def test_cli_exit_codes_and_mcp_tool_count(self) -> None:
        self.assertEqual(len(TOOLS), 8)
        stdout = io.StringIO()
        with mock.patch(
            "literature_evidence_mcp.quality_eval.run_evaluation",
            return_value=self.report,
        ), contextlib.redirect_stdout(stdout):
            self.assertEqual(main([]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["evidence_level"], "offline_simulated")

        failed = dict(self.report, passed=False)
        with mock.patch(
            "literature_evidence_mcp.quality_eval.run_evaluation",
            return_value=failed,
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 1)

        stderr = io.StringIO()
        with mock.patch(
            "literature_evidence_mcp.quality_eval.run_evaluation",
            side_effect=RuntimeError("secret /tmp/private-path"),
        ), contextlib.redirect_stderr(stderr):
            self.assertEqual(main([]), 2)
        self.assertNotIn("secret", stderr.getvalue())
        self.assertNotIn("/tmp", stderr.getvalue())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(
                main(
                    [
                        "--api-key",
                        "SYNTHETIC_SECRET",
                        "/private/tmp/synthetic-secret-path",
                    ]
                ),
                2,
            )
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("SYNTHETIC_SECRET", stderr.getvalue())
        self.assertNotIn("/private/tmp/synthetic-secret-path", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
