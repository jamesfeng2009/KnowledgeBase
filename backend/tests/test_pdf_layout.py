"""PDF 版式分层（layout_analyzer + pdf_parser 集成）测试。

分两层的测试策略：
    - 算法层：直接构造 TextBlock 测栏检测/阅读顺序，不依赖 pymupdf；
    - 集成层：mock pymupdf Page 测 pdf_parser 的接入、开关与降级。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.document.layout_analyzer import (
    TextBlock,
    extract_page_text_ordered,
    sort_reading_order,
    _detect_columns,
)

# A4 页宽（pt），与验证脚本一致
PAGE_W = 595.0


def _mk_block(
    x0: float, y0: float, x1: float, y1: float, text: str = ""
) -> TextBlock:
    """构造 TextBlock 的便捷工厂。"""
    return TextBlock(bbox=(x0, y0, x1, y1), text=text)


def _mk_pymupdf_block(
    x0: float, y0: float, x1: float, y1: float, text: str
) -> dict[str, Any]:
    """构造模拟 pymupdf get_text("dict") 的文本 block。"""
    return {
        "type": 0,
        "bbox": (x0, y0, x1, y1),
        "lines": [{"spans": [{"text": text, "font": "Helvetica", "size": 11.0, "flags": 0}]}],
    }


# =========================================================================
# 算法层：TextBlock.from_pymupdf
# =========================================================================


class TestTextBlockFromPymupdf:
    def test_parses_text_block(self) -> None:
        raw = _mk_pymupdf_block(72, 100, 300, 115, "Hello world")
        tb = TextBlock.from_pymupdf(raw)
        assert tb is not None
        assert tb.bbox == (72.0, 100.0, 300.0, 115.0)
        assert tb.text == "Hello world"

    def test_skips_non_text_block(self) -> None:
        raw = {"type": 1, "bbox": (0, 0, 100, 100)}  # image block
        assert TextBlock.from_pymupdf(raw) is None

    def test_skips_missing_bbox(self) -> None:
        raw = {"type": 0, "lines": []}
        assert TextBlock.from_pymupdf(raw) is None

    def test_multiline_joined_with_newline(self) -> None:
        raw = {
            "type": 0,
            "bbox": (0, 0, 100, 40),
            "lines": [
                {"spans": [{"text": "line one"}]},
                {"spans": [{"text": "line two"}]},
            ],
        }
        tb = TextBlock.from_pymupdf(raw)
        assert tb is not None
        assert tb.text == "line one\nline two"

    def test_geometry_properties(self) -> None:
        tb = _mk_block(10, 20, 110, 45)
        assert tb.width == pytest.approx(100.0)
        assert tb.height == pytest.approx(25.0)


# =========================================================================
# 算法层：栏检测 _detect_columns
# =========================================================================


class TestDetectColumns:
    def test_single_column(self) -> None:
        blocks = [
            _mk_block(72, 100, 300, 115, "p1"),
            _mk_block(72, 130, 300, 145, "p2"),
            _mk_block(72, 160, 300, 175, "p3"),
        ]
        cols = _detect_columns(blocks, PAGE_W)
        assert len(cols) == 1
        assert [b.text for b in cols[0]] == ["p1", "p2", "p3"]

    def test_two_columns_left_then_right(self) -> None:
        blocks = [
            _mk_block(72, 100, 280, 115, "L1"),
            _mk_block(72, 130, 280, 145, "L2"),
            _mk_block(330, 100, 540, 115, "R1"),
            _mk_block(330, 130, 540, 145, "R2"),
        ]
        cols = _detect_columns(blocks, PAGE_W)
        assert len(cols) == 2
        assert [b.text for b in cols[0]] == ["L1", "L2"]  # 左栏
        assert [b.text for b in cols[1]] == ["R1", "R2"]  # 右栏

    def test_column_x_tolerance_merges_near_columns(self) -> None:
        # x0 差 10pt（容差 20 内）应视为同栏
        blocks = [
            _mk_block(72, 100, 280, 115, "a"),
            _mk_block(82, 130, 290, 145, "b"),
        ]
        cols = _detect_columns(blocks, PAGE_W)
        assert len(cols) == 1

    def test_empty_input(self) -> None:
        assert _detect_columns([], PAGE_W) == []


# =========================================================================
# 算法层：sort_reading_order（含通栏处理）
# =========================================================================


class TestSortReadingOrder:
    def test_two_column_reading_order(self) -> None:
        """双栏正文：左栏全部 → 右栏全部。"""
        blocks = [
            _mk_block(72, 118, 280, 133, "L1"),
            _mk_block(72, 138, 280, 153, "L2"),
            _mk_block(72, 158, 280, 173, "L3"),
            _mk_block(330, 118, 540, 133, "R1"),
            _mk_block(330, 138, 540, 153, "R2"),
        ]
        ordered = sort_reading_order(blocks, PAGE_W)
        assert [b.text for b in ordered] == ["L1", "L2", "L3", "R1", "R2"]

    def test_full_width_title_stays_on_top(self) -> None:
        """通栏标题在双栏正文之上。"""
        title = _mk_block(72, 60, 500, 85, "TITLE")  # 宽 428 >= 595*0.7 → 通栏
        body = [
            _mk_block(72, 118, 280, 133, "L1"),
            _mk_block(330, 118, 540, 133, "R1"),
        ]
        ordered = sort_reading_order([*body, title], PAGE_W)
        assert ordered[0].text == "TITLE"
        assert [b.text for b in ordered[1:]] == ["L1", "R1"]

    def test_full_width_footnote_goes_last(self) -> None:
        """通栏脚注在双栏正文之下。"""
        foot = _mk_block(72, 770, 500, 785, "FOOT")
        body = [
            _mk_block(72, 118, 280, 133, "L1"),
            _mk_block(330, 118, 540, 133, "R1"),
        ]
        ordered = sort_reading_order([foot, *body], PAGE_W)
        assert ordered[-1].text == "FOOT"

    def test_title_and_footnote_sandwich_body(self) -> None:
        """标题 + 双栏 + 脚注 的完整版式：标题最前、脚注在所有栏之后。"""
        blocks = [
            _mk_block(330, 118, 540, 133, "R1"),
            _mk_block(72, 770, 500, 785, "FOOT"),
            _mk_block(72, 118, 280, 133, "L1"),
            _mk_block(72, 60, 500, 85, "TITLE"),
            _mk_block(72, 138, 280, 153, "L2"),
            _mk_block(330, 138, 540, 153, "R2"),
        ]
        ordered = sort_reading_order(blocks, PAGE_W)
        assert [b.text for b in ordered] == ["TITLE", "L1", "L2", "R1", "R2", "FOOT"]

    def test_short_title_footnote_not_full_width(self) -> None:
        """真实场景：短标题/短脚注宽度 < 70% 页宽，仍应正确识别为通栏块。

        回归用例：标题 292pt、脚注 127pt（< 595*0.7=416pt），x0 与左栏对齐，
        第一轮栏检测会把它们聚进左栏，需靠 y 间隙断段剥离。
        """
        blocks = [
            _mk_block(72, 60.7, 364.1, 85.4, "TITLE"),   # 短标题，x0=72 同左栏
            _mk_block(72, 118.2, 236.5, 133.3, "L1"),
            _mk_block(72, 138.2, 252.4, 153.3, "L2"),
            _mk_block(72, 158.2, 240.1, 173.3, "L3"),
            _mk_block(330, 118.2, 503.0, 133.3, "R1"),
            _mk_block(330, 138.2, 518.9, 153.3, "R2"),
            _mk_block(330, 158.2, 506.7, 173.3, "R3"),
            _mk_block(72, 770.3, 199.1, 782.7, "FOOT"),  # 短脚注，x0=72 同左栏
        ]
        ordered = sort_reading_order(blocks, PAGE_W)
        assert [b.text for b in ordered] == [
            "TITLE", "L1", "L2", "L3", "R1", "R2", "R3", "FOOT",
        ]

    def test_single_column_pure_y_order(self) -> None:
        """单栏退化为纯 y 排序（输入乱序也能还原）。"""
        blocks = [
            _mk_block(72, 300, 300, 315, "c"),
            _mk_block(72, 100, 300, 115, "a"),
            _mk_block(72, 200, 300, 215, "b"),
        ]
        ordered = sort_reading_order(blocks, PAGE_W)
        assert [b.text for b in ordered] == ["a", "b", "c"]

    def test_empty_and_single(self) -> None:
        assert sort_reading_order([], PAGE_W) == []
        single = [_mk_block(0, 0, 100, 10, "x")]
        assert sort_reading_order(single, PAGE_W) == single


# =========================================================================
# 算法层：extract_page_text_ordered 一站式接口
# =========================================================================


class TestExtractPageTextOrdered:
    def test_two_column_page(self) -> None:
        page_dict = {
            "blocks": [
                _mk_pymupdf_block(72, 118, 280, 133, "L1"),
                _mk_pymupdf_block(72, 138, 280, 153, "L2"),
                _mk_pymupdf_block(330, 118, 540, 133, "R1"),
                _mk_pymupdf_block(330, 138, 540, 153, "R2"),
            ]
        }
        text = extract_page_text_ordered(page_dict, PAGE_W)
        assert text == "L1\nL2\nR1\nR2"

    def test_full_layout(self) -> None:
        page_dict = {
            "blocks": [
                _mk_pymupdf_block(330, 118, 540, 133, "R1"),
                _mk_pymupdf_block(72, 60, 500, 85, "TITLE"),
                _mk_pymupdf_block(72, 118, 280, 133, "L1"),
                _mk_pymupdf_block(72, 770, 500, 785, "FOOT"),
            ]
        }
        text = extract_page_text_ordered(page_dict, PAGE_W)
        assert text == "TITLE\nL1\nR1\nFOOT"

    def test_empty_page(self) -> None:
        assert extract_page_text_ordered({"blocks": []}, PAGE_W) == ""

    def test_exception_returns_empty(self) -> None:
        """脏数据不应抛异常（防御）。"""
        assert extract_page_text_ordered({"blocks": [{"bad": "data"}]}, PAGE_W) == ""
        assert extract_page_text_ordered({}, PAGE_W) == ""


# =========================================================================
# 集成层：pdf_parser 接入（mock pymupdf）
# =========================================================================


def _mk_mock_page(
    dict_blocks: list[dict[str, Any]],
    plain_text: str = "fallback text",
    width: float = PAGE_W,
) -> MagicMock:
    """构造模拟 pymupdf Page：get_text() 按参数返回。"""
    page = MagicMock()
    page.rect.width = width

    def _get_text(mode: str = "text", **kwargs: Any) -> Any:
        if mode == "dict":
            return {"blocks": dict_blocks}
        return plain_text

    page.get_text.side_effect = _get_text
    return page


class TestPDFParserLayoutIntegration:
    """pdf_parser._extract_text_ordered 的开关与降级行为。"""

    def _make_parser(self) -> Any:
        from app.document.pdf_parser import PDFParser

        return PDFParser()

    def test_layout_enabled_uses_dict_order(self) -> None:
        parser = self._make_parser()
        # dict 中 block 顺序乱（R 在前），版式分析应重排
        blocks = [
            _mk_pymupdf_block(330, 118, 540, 133, "R1"),
            _mk_pymupdf_block(72, 118, 280, 133, "L1"),
        ]
        page = _mk_mock_page(blocks, plain_text="plain fallback")
        result = parser._extract_text_ordered(page, layout_analysis_enabled=True)
        assert result == "L1\nR1"

    def test_layout_disabled_uses_plain_text(self) -> None:
        parser = self._make_parser()
        blocks = [_mk_pymupdf_block(72, 118, 280, 133, "L1")]
        page = _mk_mock_page(blocks, plain_text="plain fallback")
        result = parser._extract_text_ordered(page, layout_analysis_enabled=False)
        assert result == "plain fallback"
        # 确认未走 dict 模式
        assert all(
            call.args == ("text",) or not call.args
            for call in page.get_text.call_args_list
        )

    def test_layout_exception_falls_back_to_plain(self) -> None:
        """版式分析内部异常时降级为 get_text()，内容不丢。"""
        parser = self._make_parser()
        page = _mk_mock_page([], plain_text="plain fallback")
        # 让 get_text("dict") 抛异常
        def _boom(mode: str = "text", **kwargs: Any) -> Any:
            if mode == "dict":
                raise RuntimeError("dict extraction failed")
            return "plain fallback"

        page.get_text.side_effect = _boom
        result = parser._extract_text_ordered(page, layout_analysis_enabled=True)
        assert result == "plain fallback"

    def test_layout_empty_result_falls_back(self) -> None:
        """版式分析返回空（如无文本 block）时降级为 get_text()。"""
        parser = self._make_parser()
        page = _mk_mock_page([], plain_text="  plain fallback  ")
        result = parser._extract_text_ordered(page, layout_analysis_enabled=True)
        assert result == "plain fallback"  # 已 strip
