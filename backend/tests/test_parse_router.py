"""DOC_PARSER_ROUTE 路由决策的纯函数单测。

覆盖两条路由策略、复杂度评分边界、非法 route 兜底，以及染色样本数值。
"""

from __future__ import annotations

import pytest

from app.document.parse_router import (
    ParseSignals,
    choose_engine,
    complexity_from_html,
    compute_complexity,
)


class TestComputeComplexity:
    def test_digital_pdf_low(self) -> None:
        # 数字 PDF、少量图片、无公式 → 复杂度低
        s = ParseSignals(doc_type="pdf", text_layer_ratio=1.0, n_images=1)
        assert compute_complexity(s) < 0.6

    def test_scanned_pdf_high(self) -> None:
        # 纯扫描 PDF（无文本层）→ 复杂度高
        s = ParseSignals(doc_type="pdf", text_layer_ratio=0.0, n_images=8)
        assert compute_complexity(s) >= 0.6

    def test_image_dense_pdf_high(self) -> None:
        s = ParseSignals(doc_type="pdf", text_layer_ratio=1.0, n_images=30)
        assert compute_complexity(s) >= 0.6

    def test_formula_table_riches_complexity(self) -> None:
        base = ParseSignals(doc_type="pdf", text_layer_ratio=1.0)
        heavy = ParseSignals(
            doc_type="pdf", text_layer_ratio=0.4, formula_markers=6, complex_table=True
        )
        assert compute_complexity(heavy) > compute_complexity(base)

    def test_out_of_range_clamped(self) -> None:
        s = ParseSignals(doc_type="pdf", text_layer_ratio=2.0, n_images=-3)
        c = compute_complexity(s)
        assert 0.0 <= c <= 1.0


class TestChooseEngineDefault:
    # docling_default_conditional_mineru
    def test_image_goes_mineru(self) -> None:
        s = ParseSignals(doc_type="png")
        assert choose_engine("docling_default_conditional_mineru", s, mineru_available=True) == "mineru"

    def test_scanned_pdf_goes_mineru(self) -> None:
        s = ParseSignals(doc_type="pdf", text_layer_ratio=0.0)
        assert choose_engine("docling_default_conditional_mineru", s, mineru_available=True) == "mineru"

    def test_digital_pdf_stays_docling(self) -> None:
        s = ParseSignals(doc_type="pdf", text_layer_ratio=1.0)
        assert choose_engine("docling_default_conditional_mineru", s, mineru_available=True) == "docling"

    def test_office_stays_docling(self) -> None:
        for t in ("docx", "pptx", "xlsx"):
            s = ParseSignals(doc_type=t)
            assert choose_engine("docling_default_conditional_mineru", s, mineru_available=True) == "docling"


class TestChooseEngineComplexity:
    # mineru_by_doc_complexity
    def test_complex_doc_escalates_mineru(self) -> None:
        s = ParseSignals(doc_type="pdf", text_layer_ratio=0.0, n_images=10)
        assert choose_engine("mineru_by_doc_complexity", s, mineru_available=True, threshold=0.6) == "mineru"

    def test_low_complexity_stays_docling(self) -> None:
        s = ParseSignals(doc_type="pdf", text_layer_ratio=1.0, n_images=1)
        assert choose_engine("mineru_by_doc_complexity", s, mineru_available=True, threshold=0.9) == "docling"

    def test_image_unconditional_mineru(self) -> None:
        s = ParseSignals(doc_type="jpg")
        assert choose_engine("mineru_by_doc_complexity", s, mineru_available=True) == "mineru"


class TestChooseEngineGuards:
    def test_mineru_unavailable_always_docling(self) -> None:
        s = ParseSignals(doc_type="png", text_layer_ratio=0.0)
        assert choose_engine("mineru_by_doc_complexity", s, mineru_available=False) == "docling"

    def test_unknown_route_falls_back_to_default(self) -> None:
        s = ParseSignals(doc_type="pdf", text_layer_ratio=0.0)
        # 非法 route → 兜底默认策略：扫描 PDF 仍应升级 MinerU
        assert choose_engine("typo_route", s, mineru_available=True) == "mineru"


class TestComplexityFromHtml:
    def test_table_formula_image_detection(self) -> None:
        html = (
            "<table><tr><td>a</td></tr></table>"
            "<table><tr><td>b</td></tr></table>"
            "<img src='x'/> [图片描述: 图] "
            "公式 $E=mc^2$ \\alpha {sub}"
        )
        s = complexity_from_html(html, text_layer_ratio=0.3)
        assert s.n_images >= 1
        assert s.formula_markers >= 1
        assert s.complex_table is True
        assert compute_complexity(s) >= 0.6

    def test_plain_text_low_complexity(self) -> None:
        s = complexity_from_html("<p>hello world 纯文本</p>", text_layer_ratio=1.0)
        assert s.complex_table is False
        assert compute_complexity(s) < 0.6