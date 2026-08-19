"""
P0-2 结构化引用卡片提取测试。

覆盖：
    - 基本 [n] 标注映射到 sources
    - 中文全角括号【n】
    - 引用编号越界（未映射）跳过且不抛异常
    - snippet 超长截断
    - 答案无引用标注 → 空列表
    - 无来源文档 → 空列表
"""

import pytest

from app.rag.citation import CitationExtractor


def _sources():
    return [
        {"doc_id": "doc1", "title": "LangGraph 文档", "content": "LangGraph是一个状态机框架。"},
        {"doc_id": "doc2", "title": "Agent 设计指南", "content": "使用 LangGraph 构建 Agent 循环。"},
    ]


@pytest.fixture
def extractor():
    return CitationExtractor()


class TestCitationExtraction:
    def test_basic_mapping(self, extractor):
        """[1][2] 映射到前两个来源。"""
        answer = "LangGraph是一个状态机框架[1]，用于构建Agent循环[2]。"
        citations = extractor.extract(answer, _sources())
        assert len(citations) == 2
        assert citations[0]["citation_id"] == 1
        assert citations[0]["doc_id"] == "doc1"
        assert citations[1]["citation_id"] == 2
        assert citations[1]["doc_id"] == "doc2"

    def test_fullwidth_brackets(self, extractor):
        """中文全角括号【1】同样被识别。"""
        answer = "状态机框架【1】。"
        citations = extractor.extract(answer, _sources())
        assert len(citations) == 1
        assert citations[0]["citation_id"] == 1
        assert citations[0]["doc_id"] == "doc1"

    def test_unmapped_id_skipped(self, extractor):
        """引用编号越界时跳过该卡片，不抛异常。"""
        answer = "引用不存在的文档[9]。"
        citations = extractor.extract(answer, _sources())
        assert citations == []

    def test_snippet_truncated(self, extractor):
        """来源 content 超长时 snippet 截断到 200 字符并带省略号。"""
        long_content = "x" * 500
        sources = [{"doc_id": "doc1", "content": long_content}]
        citations = extractor.extract("引用[1]", sources)
        assert len(citations) == 1
        assert citations[0]["snippet"].endswith("...")
        # 设计：截到 _SNIPPET_MAX(200) 字符后追加省略号
        assert len(citations[0]["snippet"]) == 203
        assert citations[0]["snippet"].startswith("x" * 200)

    def test_no_citation_markers(self, extractor):
        """答案无 [n] 标注 → 空列表。"""
        citations = extractor.extract("这是一个没有引用的答案。", _sources())
        assert citations == []

    def test_no_sources(self, extractor):
        """无来源文档 → 空列表。"""
        citations = extractor.extract("引用[1]", [])
        assert citations == []