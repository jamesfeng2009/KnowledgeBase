"""引用强制校验测试 — app/rag/citation.py validate_citations / has_citations。

覆盖范围：
    - has_citations: 检测文本是否包含 [n] 引用标注
    - validate_citations: 强制校验答案是否包含有效引用
    - CitationValidationResult: 数据类和 to_dict
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.rag.citation import CitationExtractor, CitationValidationResult


class TestHasCitations:
    """has_citations 方法测试。"""

    def test_text_with_citation(self) -> None:
        """包含 [n] 引用标注的文本返回 True。"""
        extractor = CitationExtractor()
        assert extractor.has_citations("根据文档[1]的说明") is True

    def test_text_with_multiple_citations(self) -> None:
        """包含多个引用标注的文本返回 True。"""
        extractor = CitationExtractor()
        assert extractor.has_citations("根据[1]和[2]的说明") is True

    def test_text_with_chinese_brackets(self) -> None:
        """包含中文全角方括号【n】的文本返回 True。"""
        extractor = CitationExtractor()
        assert extractor.has_citations("根据文档【1】的说明") is True

    def test_text_without_citation(self) -> None:
        """不包含引用标注的文本返回 False。"""
        extractor = CitationExtractor()
        assert extractor.has_citations("这是一个没有引用的答案") is False

    def test_empty_text(self) -> None:
        """空文本返回 False。"""
        extractor = CitationExtractor()
        assert extractor.has_citations("") is False

    def test_text_with_brackets_but_no_number(self) -> None:
        """包含方括号但不是数字引用的文本返回 False。"""
        extractor = CitationExtractor()
        assert extractor.has_citations("[注意]这是一段文字") is False


class TestValidateCitations:
    """validate_citations 方法测试。"""

    def test_valid_citations(self) -> None:
        """答案包含有效引用标注时校验通过。"""
        extractor = CitationExtractor()
        text = "根据文档[1]的说明，该产品支持批量导入。"
        sources = [{"doc_id": "1", "content": "产品支持批量导入"}]

        result = extractor.validate_citations(text, sources)

        assert result.valid is True
        assert result.has_citations is True
        assert result.citation_count == 1
        assert result.source_count == 1
        assert result.unmapped_ids == []

    def test_no_citations_with_sources(self) -> None:
        """有来源文档但答案无引用标注时校验失败。"""
        extractor = CitationExtractor()
        text = "该产品支持批量导入功能。"  # 无 [n] 引用
        sources = [{"doc_id": "1", "content": "产品支持批量导入"}]

        result = extractor.validate_citations(text, sources)

        assert result.valid is False
        assert result.has_citations is False
        assert result.citation_count == 0
        assert "幻觉风险" in result.reason

    def test_no_sources_skips_validation(self) -> None:
        """无来源文档时跳过校验（非 RAG 场景）。"""
        extractor = CitationExtractor()
        text = "这是一个普通对话回答，不需要引用。"

        result = extractor.validate_citations(text, [])

        assert result.valid is True
        assert result.has_citations is False
        assert result.source_count == 0

    def test_unmapped_citation_ids(self) -> None:
        """引用编号超出来源范围时记录 unmapped_ids。"""
        extractor = CitationExtractor()
        text = "根据文档[1]和[5]的说明"  # [5] 超出范围
        sources = [{"doc_id": "1", "content": "文档1"}]

        result = extractor.validate_citations(text, sources)

        # 仍通过校验（不阻断），但记录 unmapped_ids
        assert result.valid is True
        assert result.has_citations is True
        assert 5 in result.unmapped_ids

    def test_multiple_valid_citations(self) -> None:
        """多个有效引用标注。"""
        extractor = CitationExtractor()
        text = "根据[1]和[2]的说明，该功能可用。"
        sources = [
            {"doc_id": "1", "content": "文档1"},
            {"doc_id": "2", "content": "文档2"},
        ]

        result = extractor.validate_citations(text, sources)

        assert result.valid is True
        assert result.citation_count == 2
        assert result.unmapped_ids == []

    def test_result_to_dict(self) -> None:
        """to_dict 返回正确字典。"""
        result = CitationValidationResult(
            valid=True,
            has_citations=True,
            citation_count=2,
            source_count=3,
            unmapped_ids=[],
        )
        d = result.to_dict()
        assert d["valid"] is True
        assert d["has_citations"] is True
        assert d["citation_count"] == 2
        assert d["source_count"] == 3
        assert d["unmapped_ids"] == []
