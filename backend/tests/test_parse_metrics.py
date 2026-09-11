"""解析四维指标的单元测试 — 覆盖理想输入、退化输入与中文 tokenize。"""

from __future__ import annotations

import pytest

from evals.parse_eval.parse_metrics import (
    _edit_dist,
    _norm_formula,
    _tokenize,
    char_error_rate,
    formula_metrics,
    table_structure_pr,
    text_extraction_pr,
)
from evals.parse_eval.schema import CellSpan, TableTruth


class TestTokenize:
    def test_cjk_chars_split_to_tokens(self) -> None:
        toks = _tokenize("智能客服的准确率")
        assert "的" in toks and "准" in toks and "客服" not in toks

    def test_ascii_words(self) -> None:
        assert "revenue" in _tokenize("revenue grew 7.2%")


class TestTextExtractionPR:
    def test_identical(self) -> None:
        r = text_extraction_pr("Revenue grew 12%", "Revenue grew 12%")
        assert r["precision"] == pytest.approx(1.0)
        assert r["recall"] == pytest.approx(1.0)
        assert r["f1"] == pytest.approx(1.0)

    def test_subset_recall_low_precision_high(self) -> None:
        # 预测只有参考里的一半 token
        r = text_extraction_pr("Revenue 12% extra noise", "Revenue grew 12%")
        assert r["recall"] < 1.0
        assert r["precision"] < 1.0
        assert 0.0 < r["f1"] < 1.0

    def test_empty_ref(self) -> None:
        assert text_extraction_pr("any", "")["f1"] == 0.0


class TestTableStructure:
    def _table_html(self) -> str:
        return (
            "<table border=\"1\"><thead>"
            "<tr><th rowspan=\"2\">Item</th><th colspan=\"3\">Amount</th></tr>"
            "<tr><th>Revenue</th><th>Cost</th><th>Profit</th></tr></thead>"
            "<tbody>"
            "<tr><td>First quarter</td><td>2,910</td><td>1,680</td><td>1,230</td></tr>"
            "<tr><td>Second quarter</td><td>3,120</td><td>1,890</td><td>1,230</td></tr>"
            "</tbody></table>"
        )

    def _truth(self) -> TableTruth:
        return TableTruth(
            rows=4,
            cols=4,
            cells=[
                ["Item", "Amount", "", ""],
                ["", "Revenue", "Cost", "Profit"],
                ["First quarter", "2,910", "1,680", "1,230"],
                ["Second quarter", "3,120", "1,890", "1,230"],
            ],
            spans=[
                CellSpan(row=0, col=0, row_span=2, col_span=1),
                CellSpan(row=0, col=1, row_span=1, col_span=3),
            ],
            header_rows=2,
        )

    def test_perfect_extraction_scores_high(self) -> None:
        r = table_structure_pr(self._table_html(), self._truth())
        # 所有真值文本应被召回
        assert r["recall"] == pytest.approx(1.0)
        assert r["span_recovered"] is True
        assert r["header_recall"] == pytest.approx(1.0)

    def test_no_table_returns_zero(self) -> None:
        r = table_structure_pr("<p>观察文本</p>", self._truth())
        assert r["f1"] == 0.0
        assert r["span_recovered"] is False

    def test_missing_cells_reduces_recall(self) -> None:
        partial = "<table><tr><td>Item</td><td>Revenue</td></tr></table>"
        r = table_structure_pr(partial, self._truth())
        assert r["recall"] < 1.0
        assert r["span_recovered"] is False


class TestFormula:
    def test_perfect_hit(self) -> None:
        r = formula_metrics(
            "T = h \\cdot T_{cache} + (1 - h) \\cdot T_{miss}",
            ["T = h \\cdot T_{cache} + (1 - h) \\cdot T_{miss}"],
        )
        assert r["block_hit_rate"] == 1.0

    def test_no_overlap_low_sim(self) -> None:
        r = formula_metrics("a b c d", ["T = h \\cdot T_{cache}"])
        assert r["block_hit_rate"] == 0.0
        assert r["avg_string_sim"] < 0.3

    def test_empty_expected(self) -> None:
        assert formula_metrics("anything", [])["block_hit_rate"] == 1.0

    def test_norm_removes_quote_whitespace(self) -> None:
        assert _norm_formula("a  b") == "ab"


class TestCER:
    def test_identical(self) -> None:
        assert char_error_rate("你好 世界", "你好 世界") == pytest.approx(0.0)

    def test_partial_substitution_positively(self) -> None:
        cer = char_error_rate("你好世", "你好世界")
        assert 0 < cer < 1.0

    def test_disjoint(self) -> None:
        assert char_error_rate("abc", "你好世界") == pytest.approx(1.0)

    def test_empty_ref(self) -> None:
        assert char_error_rate("xxx", "") == pytest.approx(1.0)
        assert char_error_rate("", "") == pytest.approx(0.0)


class TestEditDist:
    def test_deletion_insertion_substitution(self) -> None:
        assert _edit_dist("kitten", "sitting") == 3
        assert _edit_dist("abc", "abc") == 0