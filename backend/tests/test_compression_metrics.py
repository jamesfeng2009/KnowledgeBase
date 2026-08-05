"""
压缩信息损耗评估测试 — P1-6。

覆盖：
    1. 规则法关键实体抽取（数值/日期/引号术语/英文标识符/中文专有名词）；
    2. 实体保留率计算（全保留 / 部分丢失 / 空基准 / 快照入口）；
    3. 与 ContextBudgetManager 的集成（真实压缩快照 → 保留率报告）；
    4. LLM Judge 双跑一致性（Mock LLM：正常解析、verdict 映射、异常降级）；
    5. 双跑 faithfulness 衰减对比（Mock LLM 队列响应）。

不依赖真实 LLM API — 所有 Judge 调用均使用 Mock。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.eval.compression_metrics import (
    CompressionJudge,
    compute_entity_retention,
    compute_entity_retention_from_snapshot,
    extract_key_entities,
)
from app.rag.context_budget import ContextBudgetManager


# ======================================================================
# Mock LLM
# ======================================================================


class QueueLLM:
    """Mock LLM — 按队列依次返回预设响应。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: int = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        yield self._responses[idx]


class ErrorLLM:
    """Mock LLM — chat 调用直接抛异常。"""

    async def chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        raise RuntimeError("LLM unavailable")
        yield  # noqa: E701 — 使其为 async generator


# ======================================================================
# 1. 实体抽取
# ======================================================================


class TestExtractKeyEntities:
    """规则法关键实体抽取测试。"""

    def test_extract_amounts_and_percentages(self) -> None:
        text = "单次报销上限 5000 元，超出部分需额外审批，审批通过率约 85%。"
        entities = extract_key_entities(text)
        assert "5000 元" in entities or "5000元" in {e.replace(" ", "") for e in entities} or any("5000" in e for e in entities)
        assert any("85%" in e or "85％" in e for e in entities)

    def test_extract_dates(self) -> None:
        text = "新制度自 2026年3月1日 起生效，旧版同时废止。"
        entities = extract_key_entities(text)
        assert any("2026" in e and "3" in e for e in entities)

    def test_extract_quoted_terms(self) -> None:
        text = "详见《差旅费管理制度》和“费用报销操作指引”第3条。"
        entities = extract_key_entities(text)
        assert "《差旅费管理制度》" in entities
        assert "“费用报销操作指引”" in entities

    def test_extract_english_identifiers(self) -> None:
        text = "通过 SSO 登录 OA 系统后调用 REST-API 提交申请。"
        entities = extract_key_entities(text)
        assert "SSO" in entities
        assert any("REST-API" in e for e in entities)

    def test_extract_cjk_proper_nouns(self) -> None:
        text = "报销流程需经过财务部门审批，涉及预算系统校验。"
        entities = extract_key_entities(text)
        assert any(e.endswith("流程") for e in entities)
        assert any(e.endswith("部门") for e in entities)

    def test_empty_text_returns_empty(self) -> None:
        assert extract_key_entities("") == set()

    def test_filters_short_noise(self) -> None:
        # 单字符与纯标点不应产生实体
        entities = extract_key_entities("。，、")
        assert len(entities) == 0


# ======================================================================
# 2. 实体保留率
# ======================================================================


class TestComputeEntityRetention:
    """压缩前后实体保留率计算测试。"""

    def test_full_retention(self) -> None:
        before = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "报销上限 5000 元，见《差旅费管理制度》"},
        ]
        after = list(before)  # 未压缩
        report = compute_entity_retention(before, after)
        assert report.total_entities > 0
        assert report.retention_rate == 1.0
        assert report.passed
        assert report.missing == []

    def test_partial_loss(self) -> None:
        before = [
            {"role": "user", "content": "报销上限 5000 元，审批需 3 个工作日，见《差旅费管理制度》"},
        ]
        after = [
            {"role": "user", "content": "报销相关制度说明"},  # 实体全部丢失
        ]
        report = compute_entity_retention(before, after)
        assert report.total_entities > 0
        assert report.retention_rate < 0.5
        assert not report.passed
        assert len(report.missing) == report.total_entities

    def test_empty_before_is_full_retention(self) -> None:
        report = compute_entity_retention(
            [{"role": "user", "content": "你好"}],
            [{"role": "user", "content": "你好"}],
        )
        # 无实体可丢失 → 视为完全保留
        assert report.total_entities == 0
        assert report.retention_rate == 1.0
        assert report.passed

    def test_system_messages_excluded(self) -> None:
        # system prompt 不参与实体统计（压缩不改变它）
        before = [{"role": "system", "content": "你是助手，预算 100 万元"}]
        after: list[dict[str, Any]] = []
        report = compute_entity_retention(before, after)
        assert report.total_entities == 0

    def test_snapshot_entry_none(self) -> None:
        assert compute_entity_retention_from_snapshot(None) is None

    def test_snapshot_entry_invalid(self) -> None:
        assert compute_entity_retention_from_snapshot({"before": "x"}) is None
        assert compute_entity_retention_from_snapshot({}) is None


# ======================================================================
# 3. 与 ContextBudgetManager 集成
# ======================================================================


class TestContextBudgetIntegration:
    """真实压缩快照 → 保留率报告 集成测试。"""

    def _make_long_messages(self) -> list[dict[str, Any]]:
        """构造超过压缩预算的消息列表。"""
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": "你是企业知识库助手。"},
            {"role": "user", "content": "报销流程怎么走？"},
        ]
        # 中间塞入多条超长工具结果，使总 token 超过默认预算
        for i in range(6):
            msgs.append({
                "role": "user",
                "content": f"[系统] 工具结果：{'查询结果内容' * 200} 第{i}轮",
            })
        msgs.append({"role": "assistant", "content": "继续分析"})
        msgs.append({"role": "user", "content": "请给出结论"})
        return msgs

    def test_snapshot_populated_after_compression(self) -> None:
        manager = ContextBudgetManager()
        messages = self._make_long_messages()
        assert manager.should_compress(messages)

        compressed = manager.compress(messages)
        snapshot = manager.get_last_snapshot()

        assert snapshot is not None
        assert snapshot["before_tokens"] > snapshot["after_tokens"]
        assert len(compressed) < len(messages)

    def test_retention_report_from_real_snapshot(self) -> None:
        manager = ContextBudgetManager()
        messages = self._make_long_messages()
        manager.compress(messages)

        report = compute_entity_retention_from_snapshot(manager.get_last_snapshot())
        assert report is not None
        # 压缩摘要截断到 80 字，长工具结果中的实体大多丢失 → 报告应如实反映
        assert 0.0 <= report.retention_rate <= 1.0
        assert report.total_entities == len(report.retained) + len(report.missing)

    def test_reset_clears_snapshot(self) -> None:
        manager = ContextBudgetManager()
        manager.compress(self._make_long_messages())
        manager.reset()
        assert manager.get_last_snapshot() is None

    def test_no_compression_no_snapshot(self) -> None:
        manager = ContextBudgetManager()
        short = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        manager.compress(short)  # 消息太少不触发压缩
        assert manager.get_last_snapshot() is None


# ======================================================================
# 4. LLM Judge 一致性
# ======================================================================


class TestCompressionJudgeConsistency:
    """双跑关键结论一致性评估测试。"""

    @pytest.mark.asyncio
    async def test_consistent_verdict(self) -> None:
        llm = QueueLLM([
            '{"consistency_score": 5, "key_diffs": [], "reasoning": "结论完全一致"}'
        ])
        judge = CompressionJudge(judge_llm=llm)

        result = await judge.evaluate_consistency(
            "报销上限是多少",
            "报销上限为 5000 元，详见《差旅费管理制度》。",
            "上限 5000 元，依据差旅制度。",
        )

        assert result.error is None
        assert result.consistency_score == 5.0
        assert result.verdict == "consistent"
        assert result.passed

    @pytest.mark.asyncio
    async def test_minor_loss_verdict(self) -> None:
        llm = QueueLLM([
            '{"consistency_score": 3, "key_diffs": ["缺少金额上限"], "reasoning": "次要遗漏"}'
        ])
        judge = CompressionJudge(judge_llm=llm)

        result = await judge.evaluate_consistency("q", "answer full", "answer compressed")

        assert result.verdict == "minor_loss"
        assert not result.passed
        assert result.key_diffs == ["缺少金额上限"]

    @pytest.mark.asyncio
    async def test_major_loss_verdict(self) -> None:
        llm = QueueLLM([
            '```json\n{"consistency_score": 1, "key_diffs": ["结论矛盾"], "reasoning": "严重不一致"}\n```'
        ])
        judge = CompressionJudge(judge_llm=llm)

        result = await judge.evaluate_consistency("q", "a", "b")

        assert result.consistency_score == 1.0
        assert result.verdict == "major_loss"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self) -> None:
        llm = QueueLLM(["这不是 JSON"])
        judge = CompressionJudge(judge_llm=llm)

        result = await judge.evaluate_consistency("q", "a", "b")

        assert result.error is not None
        assert not result.passed

    @pytest.mark.asyncio
    async def test_llm_exception_degrades_gracefully(self) -> None:
        judge = CompressionJudge(judge_llm=ErrorLLM())

        result = await judge.evaluate_consistency("q", "a", "b")

        assert result.error == "LLM unavailable"
        assert not result.passed


# ======================================================================
# 5. 双跑 faithfulness 衰减对比
# ======================================================================


class TestDualFaithfulness:
    """双跑 faithfulness 衰减对比测试。"""

    def _judge_json(self, total: float) -> str:
        return (
            '{"citation_accuracy": 4, "completeness": 4, '
            f'"hallucination_inverse": 4, "total_score": {total}, "reasoning": "ok"}}'
        )

    @pytest.mark.asyncio
    async def test_no_degradation_passes(self) -> None:
        llm = QueueLLM([self._judge_json(4.5), self._judge_json(4.2)])
        judge = CompressionJudge(judge_llm=llm)

        report = await judge.evaluate_dual_faithfulness(
            "q", "answer full", ["ctx full"],
            "answer compressed", ["ctx compressed"],
        )

        assert report.error is None
        assert report.score_uncompressed == 4.5
        assert report.score_compressed == 4.2
        assert abs(report.degradation - 0.3) < 1e-6
        assert report.passed  # 衰减 <= 0.5

    @pytest.mark.asyncio
    async def test_large_degradation_fails(self) -> None:
        llm = QueueLLM([self._judge_json(4.5), self._judge_json(2.0)])
        judge = CompressionJudge(judge_llm=llm)

        report = await judge.evaluate_dual_faithfulness(
            "q", "a", ["c"], "b", ["c2"],
        )

        assert report.degradation == 2.5
        assert not report.passed

    @pytest.mark.asyncio
    async def test_llm_exception_degrades_gracefully(self) -> None:
        judge = CompressionJudge(judge_llm=ErrorLLM())

        report = await judge.evaluate_dual_faithfulness(
            "q", "a", ["c"], "b", ["c2"],
        )

        assert report.error is not None
        assert not report.passed

    @pytest.mark.asyncio
    async def test_judge_calls_made_twice(self) -> None:
        llm = QueueLLM([self._judge_json(4.0), self._judge_json(4.0)])
        judge = CompressionJudge(judge_llm=llm)

        await judge.evaluate_dual_faithfulness("q", "a", ["c"], "b", ["c2"])

        assert llm.calls == 2  # 完整 / 压缩各评一次
