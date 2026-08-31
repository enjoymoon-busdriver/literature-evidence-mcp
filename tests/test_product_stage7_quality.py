from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

        markdown = self._case("TRACE-MD-ENHANCED")["observed"]["results"][0]
        self.assertTrue(markdown["anchor_label"])
        self.assertIsInstance(markdown["source_line_start"], int)
        pdf = self._case("TRACE-PDF-ENHANCED")["observed"]["results"][0]
        self.assertEqual(pdf["pdf_page_start"], 2)

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


if __name__ == "__main__":
    unittest.main()
