"""
P4-B 矛盾检测器单元测试。

覆盖：
    - ContradictionResult 数据结构
    - 用户陈述矛盾检测（LLM 成功 / 一致 / 无关）
    - 共同实体预筛跳过
    - 历史不足跳过
    - LLM 异常降级
    - 回答-知识库矛盾检测
    - 文档间矛盾检测
    - ContradictionDetector 无 LLM 降级
"""

import json
import pytest

from app.context.contradiction_detector import (
    ContradictionDetector,
    ContradictionResult,
)


# ============================================================
# ContradictionResult
# ============================================================

class TestContradictionResult:
    """ContradictionResult 数据结构测试。"""

    def test_to_dict_no_contradiction(self):
        result = ContradictionResult(
            has_contradiction=False,
            contradiction_type="user_statement",
        )
        d = result.to_dict()
        assert d["has_contradiction"] is False
        assert d["contradiction_type"] == "user_statement"
        assert d["conflicting_sources"] == []

    def test_to_dict_with_contradiction(self):
        result = ContradictionResult(
            has_contradiction=True,
            contradiction_type="answer_vs_kb",
            description="回答与文档A矛盾",
            conflicting_sources=["doc_1", "doc_2"],
            severity="high",
            action="block",
        )
        d = result.to_dict()
        assert d["has_contradiction"] is True
        assert d["description"] == "回答与文档A矛盾"
        assert d["conflicting_sources"] == ["doc_1", "doc_2"]
        assert d["severity"] == "high"


# ============================================================
# Mock LLM Provider
# ============================================================

class MockLLMProvider:
    """Mock LLM Provider — 返回预设 JSON 响应。"""

    def __init__(self, response: str = '{"contradiction": false}'):
        self._response = response

    async def chat(self, messages, tools=None, stream=False, **kwargs):
        yield self._response


class FailingLLMProvider:
    """Mock LLM Provider — 总是抛异常。"""

    async def chat(self, messages, tools=None, stream=False, **kwargs):
        raise RuntimeError("LLM unavailable")
        yield  # unreachable


# ============================================================
# 用户陈述矛盾检测
# ============================================================

class TestUserContradiction:
    """用户陈述矛盾检测测试。"""

    @pytest.mark.asyncio
    async def test_contradiction_detected(self):
        """LLM 检测到矛盾。"""
        llm = MockLLMProvider(
            '{"contradiction": true, "description": "用户之前说A，现在说B", "severity": "high"}'
        )
        detector = ContradictionDetector(llm=llm)
        history = [
            {"role": "user", "content": "北京今天限号5"},
            {"role": "assistant", "content": "是的"},
            {"role": "user", "content": "北京今天限号3"},
        ]
        result = await detector.check_user_contradiction(
            "北京今天限号3", history,
        )
        assert result.has_contradiction is True
        assert result.contradiction_type == "user_statement"
        assert result.action == "warn"
        assert result.severity == "high"

    @pytest.mark.asyncio
    async def test_no_contradiction_consistent(self):
        """LLM 判定一致。"""
        llm = MockLLMProvider(
            '{"contradiction": false, "description": "", "severity": "low"}'
        )
        detector = ContradictionDetector(llm=llm)
        history = [
            {"role": "user", "content": "北京今天天气晴"},
            {"role": "assistant", "content": "是的"},
            {"role": "user", "content": "北京今天天气情况"},
        ]
        result = await detector.check_user_contradiction(
            "北京今天天气情况", history,
        )
        assert result.has_contradiction is False

    @pytest.mark.asyncio
    async def test_history_insufficient_skip(self):
        """历史不足 2 条 → 跳过。"""
        detector = ContradictionDetector(llm=MockLLMProvider())
        result = await detector.check_user_contradiction(
            "问题", [{"role": "user", "content": "只有一个"}],
        )
        assert result.has_contradiction is False
        assert result.contradiction_type == "user_statement"

    @pytest.mark.asyncio
    async def test_no_common_entities_skip(self):
        """无共同实体 → 跳过 LLM。"""
        llm = MockLLMProvider('{"contradiction": true}')  # 不应被调用
        detector = ContradictionDetector(llm=llm)
        history = [
            {"role": "user", "content": "苹果手机怎么样"},
            {"role": "assistant", "content": "不错"},
            {"role": "user", "content": "今天午饭吃什么"},
        ]
        result = await detector.check_user_contradiction(
            "今天午饭吃什么", history,
        )
        # 无共同实体 → 跳过，返回无矛盾
        assert result.has_contradiction is False

    @pytest.mark.asyncio
    async def test_llm_exception_degrade(self):
        """LLM 异常 → 降级。"""
        detector = ContradictionDetector(llm=FailingLLMProvider())
        history = [
            {"role": "user", "content": "北京限号5"},
            {"role": "assistant", "content": "是的"},
            {"role": "user", "content": "北京限号3"},
        ]
        result = await detector.check_user_contradiction(
            "北京限号3", history,
        )
        assert result.has_contradiction is False

    @pytest.mark.asyncio
    async def test_no_llm_skip(self):
        """无 LLM → 跳过。"""
        detector = ContradictionDetector(llm=None)
        history = [
            {"role": "user", "content": "陈述A"},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": "陈述A"},
        ]
        result = await detector.check_user_contradiction("陈述A", history)
        assert result.has_contradiction is False


# ============================================================
# 回答-知识库矛盾检测
# ============================================================

class TestAnswerConsistency:
    """回答-知识库矛盾检测测试。"""

    @pytest.mark.asyncio
    async def test_contradiction_detected(self):
        """LLM 检测到回答与文档矛盾。"""
        llm = MockLLMProvider(
            '{"contradiction": true, "description": "回答与文档A矛盾", "severity": "high"}'
        )
        detector = ContradictionDetector(llm=llm)
        docs = [
            {"doc_id": "doc_1", "content": "北京限号是5"},
            {"doc_id": "doc_2", "content": "上海不限号"},
        ]
        result = await detector.check_answer_consistency(
            "北京限号是3", docs,
        )
        assert result.has_contradiction is True
        assert result.contradiction_type == "answer_vs_kb"
        assert result.action == "block"
        assert "doc_1" in result.conflicting_sources

    @pytest.mark.asyncio
    async def test_no_contradiction(self):
        """LLM 判定一致。"""
        llm = MockLLMProvider(
            '{"contradiction": false, "description": "", "severity": "low"}'
        )
        detector = ContradictionDetector(llm=llm)
        docs = [{"doc_id": "doc_1", "content": "北京限号是5"}]
        result = await detector.check_answer_consistency(
            "北京限号是5", docs,
        )
        assert result.has_contradiction is False

    @pytest.mark.asyncio
    async def test_no_docs_skip(self):
        """无文档 → 跳过。"""
        detector = ContradictionDetector(llm=MockLLMProvider())
        result = await detector.check_answer_consistency("回答", [])
        assert result.has_contradiction is False
        assert result.contradiction_type == "answer_vs_kb"

    @pytest.mark.asyncio
    async def test_empty_answer_skip(self):
        """空回答 → 跳过。"""
        detector = ContradictionDetector(llm=MockLLMProvider())
        result = await detector.check_answer_consistency(
            "", [{"doc_id": "1", "content": "内容"}],
        )
        assert result.has_contradiction is False

    @pytest.mark.asyncio
    async def test_llm_exception_degrade(self):
        """LLM 异常 → 降级。"""
        detector = ContradictionDetector(llm=FailingLLMProvider())
        docs = [{"doc_id": "doc_1", "content": "内容"}]
        result = await detector.check_answer_consistency("回答", docs)
        assert result.has_contradiction is False


# ============================================================
# 文档间矛盾检测
# ============================================================

class TestDocContradiction:
    """文档间矛盾检测测试。"""

    @pytest.mark.asyncio
    async def test_contradiction_detected(self):
        """检测到两篇文档矛盾。"""
        llm = MockLLMProvider(
            '{"contradiction": true, "description": "文档A说5，文档B说3", "severity": "medium"}'
        )
        detector = ContradictionDetector(llm=llm)
        docs = [
            {"doc_id": "doc_1", "content": "北京限号是5"},
            {"doc_id": "doc_2", "content": "北京限号是3"},
        ]
        results = await detector.check_doc_contradiction(docs)
        assert len(results) == 1
        assert results[0].has_contradiction is True
        assert results[0].contradiction_type == "doc_vs_doc"
        assert "doc_1" in results[0].conflicting_sources
        assert "doc_2" in results[0].conflicting_sources

    @pytest.mark.asyncio
    async def test_no_contradiction(self):
        """两篇文档一致。"""
        llm = MockLLMProvider(
            '{"contradiction": false, "description": "", "severity": "low"}'
        )
        detector = ContradictionDetector(llm=llm)
        docs = [
            {"doc_id": "doc_1", "content": "北京是首都"},
            {"doc_id": "doc_2", "content": "北京是中国的首都"},
        ]
        results = await detector.check_doc_contradiction(docs)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_insufficient_docs(self):
        """文档不足 2 篇 → 跳过。"""
        detector = ContradictionDetector(llm=MockLLMProvider())
        results = await detector.check_doc_contradiction(
            [{"doc_id": "1", "content": "内容"}],
        )
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_no_llm_skip(self):
        """无 LLM → 跳过。"""
        detector = ContradictionDetector(llm=None)
        docs = [
            {"doc_id": "1", "content": "A"},
            {"doc_id": "2", "content": "B"},
        ]
        results = await detector.check_doc_contradiction(docs)
        assert len(results) == 0


# ============================================================
# JSON 解析
# ============================================================

class TestLLMJsonParsing:
    """LLM JSON 响应解析测试。"""

    @pytest.mark.asyncio
    async def test_markdown_code_block(self):
        """LLM 返回 markdown 包裹的 JSON。"""
        llm = MockLLMProvider(
            '```json\n{"contradiction": true, "description": "矛盾", "severity": "high"}\n```'
        )
        detector = ContradictionDetector(llm=llm)
        history = [
            {"role": "user", "content": "北京限号5"},
            {"role": "assistant", "content": "是的"},
            {"role": "user", "content": "北京限号3"},
        ]
        result = await detector.check_user_contradiction("北京限号3", history)
        assert result.has_contradiction is True

    @pytest.mark.asyncio
    async def test_json_embedded_in_text(self):
        """LLM 返回嵌入文本中的 JSON。"""
        llm = MockLLMProvider(
            '分析结果如下：\n{"contradiction": false, "description": "", "severity": "low"}\n以上是分析。'
        )
        detector = ContradictionDetector(llm=llm)
        history = [
            {"role": "user", "content": "北京限号5"},
            {"role": "assistant", "content": "是的"},
            {"role": "user", "content": "北京限号5"},
        ]
        result = await detector.check_user_contradiction("北京限号5", history)
        assert result.has_contradiction is False
