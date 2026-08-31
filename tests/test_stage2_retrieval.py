from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literature_evidence_mcp import build_snapshot, search_snapshot
from literature_evidence_mcp.retrieval import _fts_expression


_SYNTHETIC_MARKDOWN = {
    "entropy.md": (
        "# Information Measures\n\n"
        "## Entropy\n\n"
        "Shannon entropy quantifies uncertainty in a probability distribution.\n"
    ),
    "grid.md": (
        "# 配电网证据\n\n"
        "## 谐波监测\n\n"
        "配电网 谐波 监测 使用 电压 频谱 识别 非线性 负载。\n"
    ),
    "identifier.md": (
        "# Calibration Record\n\n"
        "## Reference\n\n"
        "The observation was found during review under identifier GRID-CAL-2048.\n"
    ),
    "distant-terms.md": (
        "# Distant Terms\n\n"
        "NEBULA appears in an astronomy note. The code was NOT assigned. "
        "A sample was FOUND much later under row 999.\n"
    ),
    "tie-a.md": (
        "# Equal Rank\n\n"
        "calibrationmarker balanced evidence sentence\n"
    ),
    "tie-b.md": (
        "# Equal Rank\n\n"
        "calibrationmarker balanced evidence sentence\n"
    ),
}


class StageTwoSyntheticRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        sources: list[Path] = []
        for name, text in _SYNTHETIC_MARKDOWN.items():
            source = root / name
            source.write_text(text, encoding="utf-8")
            sources.append(source)
        built = build_snapshot(root / "library", sources)
        self.snapshot = Path(built["snapshot_path"])

    def assert_real_empty(self, result: dict[str, object]) -> None:
        self.assertFalse(result["found"])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["retrieval_mode"], "bm25")
        self.assertIn("不代表", result["message"])

    def test_synthetic_queries_and_markdown_anchors(self) -> None:
        english_question = search_snapshot(
            self.snapshot, "What is Shannon entropy?"
        )
        self.assertTrue(english_question["found"])
        self.assertEqual(len(english_question["results"]), 1)
        english_hit = english_question["results"][0]
        self.assertEqual(english_hit["source_name"], "entropy.md")
        self.assertEqual(
            english_hit["heading_path"], ["Information Measures", "Entropy"]
        )
        self.assertEqual(english_hit["source_line_start"], 3)
        self.assertEqual(english_hit["source_line_end"], 5)
        self.assertEqual(
            english_hit["anchor_label"],
            "Markdown lines 3-5 · Information Measures / Entropy",
        )
        self.assertIsNone(english_hit["pdf_page_start"])
        self.assertIsNone(english_hit["pdf_page_end"])
        self.assertFalse(english_hit["fulltext_verified"])
        self.assertFalse(english_hit["formula_verified"])

        spaced_chinese = search_snapshot(self.snapshot, "配电网 谐波 监测")
        self.assertTrue(spaced_chinese["found"])
        self.assertEqual(len(spaced_chinese["results"]), 1)
        chinese_hit = spaced_chinese["results"][0]
        self.assertEqual(chinese_hit["source_name"], "grid.md")
        self.assertEqual(chinese_hit["heading_path"], ["配电网证据", "谐波监测"])
        self.assertEqual(
            chinese_hit["anchor_label"],
            "Markdown lines 3-5 · 配电网证据 / 谐波监测",
        )
        self.assertFalse(chinese_hit["fulltext_verified"])
        self.assertFalse(chinese_hit["formula_verified"])

        punctuation_noise = search_snapshot(
            self.snapshot, "Shannon,,, entropy!!!"
        )
        self.assertTrue(punctuation_noise["found"])
        self.assertEqual(
            punctuation_noise["results"], english_question["results"]
        )

    def test_continuous_chinese_and_absent_term_are_real_empty_results(self) -> None:
        continuous_chinese = search_snapshot(self.snapshot, "配电网谐波监测")
        self.assert_real_empty(continuous_chinese)

        absent_term = search_snapshot(
            self.snapshot, "quasarxylophoneunlisted"
        )
        self.assert_real_empty(absent_term)

    def test_hyphenated_identifiers_require_the_complete_identifier(self) -> None:
        fragment_only = search_snapshot(
            self.snapshot, "PHANTOM-ALPHA-FOUND-777"
        )
        self.assert_real_empty(fragment_only)

        distant_terms = search_snapshot(
            self.snapshot, "NEBULA-NOT-FOUND-999"
        )
        self.assert_real_empty(distant_terms)

        existing_identifier = search_snapshot(self.snapshot, "GRID-CAL-2048")
        self.assertTrue(existing_identifier["found"])
        self.assertEqual(len(existing_identifier["results"]), 1)
        self.assertEqual(
            existing_identifier["results"][0]["source_name"], "identifier.md"
        )
        self.assertIn(
            "GRID-CAL-2048", existing_identifier["results"][0]["excerpt"]
        )

        natural_question = search_snapshot(
            self.snapshot,
            "How does Shannon entropy relate to energy systems?",
        )
        self.assertTrue(natural_question["found"])
        self.assertEqual(len(natural_question["results"]), 1)
        self.assertEqual(
            natural_question["results"][0]["source_name"], "entropy.md"
        )

    def test_equal_score_ties_are_ordered_by_chunk_id_and_repeatable(self) -> None:
        first = search_snapshot(
            self.snapshot, "calibrationmarker", top_k=10
        )
        second = search_snapshot(
            self.snapshot, "calibrationmarker", top_k=10
        )

        self.assertTrue(first["found"])
        self.assertEqual(first, second)
        self.assertEqual(len(first["results"]), 2)
        self.assertEqual(
            {result["source_name"] for result in first["results"]},
            {"tie-a.md", "tie-b.md"},
        )
        self.assertEqual(
            first["results"][0]["score"], first["results"][1]["score"]
        )
        chunk_ids = [result["chunk_id"] for result in first["results"]]
        self.assertEqual(chunk_ids, sorted(chunk_ids))


class StageTwoQueryExpressionTests(unittest.TestCase):
    def test_hyphenated_identifier_is_one_exact_phrase(self) -> None:
        self.assertEqual(
            _fts_expression("NEBULA-NOT-FOUND-999"),
            '"NEBULA NOT FOUND 999"',
        )
        self.assertEqual(
            _fts_expression("GRID-CAL-2048"),
            '"GRID CAL 2048"',
        )

    def test_natural_language_keeps_existing_soft_or_expression(self) -> None:
        self.assertEqual(
            _fts_expression(
                "How does Shannon entropy relate to energy systems?"
            ),
            '"does" OR "Shannon" OR "entropy" OR "relate" OR "energy" OR "systems"',
        )


if __name__ == "__main__":
    unittest.main()
