"""
P0-2 五级渐进压缩测试。

覆盖：
    - _compute_level 按 ratio 分级映射
    - Level 1 TOOL_COMPRESS：仅压缩超长工具结果（首500+尾200）
    - Level 3 TOPIC_SUMMARY：主题级摘要（只保留最近 1 轮）
    - Level 4 EMERGENCY：只留 system+query+最近 1 轮
    - get_stats 暴露最近压缩级别
"""

import pytest

from app.rag.context_budget import (
    CompressionLevel,
    ContextBudgetManager,
)


def _usage(messages) -> int:
    return ContextBudgetManager.estimate_tokens(messages)


def _make_tool_messages(n_tool: int = 3, tool_size: int = 3000, extra: int = 2):
    """构造含多条工具结果的消息列表。"""
    msgs = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "用户问题"},
    ]
    for i in range(n_tool):
        msgs.append(
            {"role": "user", "content": f"[系统] 工具结果：第{i + 1}轮 " + "x" * tool_size}
        )
    for i in range(extra):
        msgs.append({"role": "user", "content": f"当前状态：迭代 {i + 1}/5"})
    return msgs


class TestComputeLevel:
    """_compute_level 按 ratio 分级映射。"""

    def test_none_below_budget(self):
        mgr = ContextBudgetManager(max_tokens=100)
        assert mgr._compute_level(50) == CompressionLevel.NONE

    def test_tool_compress_just_over(self):
        mgr = ContextBudgetManager(max_tokens=100)
        assert mgr._compute_level(120) == CompressionLevel.TOOL_COMPRESS

    def test_history_summary(self):
        mgr = ContextBudgetManager(max_tokens=100)
        assert mgr._compute_level(200) == CompressionLevel.HISTORY_SUMMARY

    def test_topic_summary(self):
        mgr = ContextBudgetManager(max_tokens=100)
        assert mgr._compute_level(400) == CompressionLevel.TOPIC_SUMMARY

    def test_emergency(self):
        mgr = ContextBudgetManager(max_tokens=100)
        assert mgr._compute_level(600) == CompressionLevel.EMERGENCY


class TestToolCompress:
    """Level 1 工具结果压缩。"""

    def test_oversized_tool_result_truncated(self):
        msgs = _make_tool_messages(n_tool=1, tool_size=3000, extra=2)
        usage = _usage(msgs)
        # ratio 落在 TOOL_COMPRESS（1.0-1.5）
        mgr = ContextBudgetManager(max_tokens=int(usage / 1.2), keep_recent=2)
        result = mgr.compress(msgs)

        tool_content = result[2]["content"]
        assert "…[中间已压缩]…" in tool_content
        # 其余消息原样保留（不产生摘要）
        assert len(result) == len(msgs)
        assert result[0] == msgs[0]
        assert result[-1] == msgs[-1]

    def test_short_tool_result_not_truncated(self):
        msgs = _make_tool_messages(n_tool=1, tool_size=100, extra=2)
        usage = _usage(msgs)
        mgr = ContextBudgetManager(max_tokens=int(usage / 1.2), keep_recent=2)
        result = mgr.compress(msgs)
        assert "…[中间已压缩]…" not in result[2]["content"]


class TestTopicSummary:
    """Level 3 主题级摘要（只保留最近 1 轮）。"""

    def test_topic_summary_keeps_one_recent(self):
        msgs = _make_tool_messages(n_tool=3, tool_size=1000, extra=3)
        usage = _usage(msgs)
        # ratio 落在 TOPIC_SUMMARY（3.0-5.0）
        mgr = ContextBudgetManager(max_tokens=int(usage / 4), keep_recent=2)
        result = mgr.compress(msgs)

        # head(2) + summary(1) + tail(1) = 4
        assert len(result) == 4
        assert "[系统] 早期上下文摘要" in result[2]["content"]
        assert result[-1] == msgs[-1]


class TestEmergency:
    """Level 4 紧急模式（只留 system+query+最近 1 轮）。"""

    def test_emergency_keeps_head_and_last(self):
        msgs = _make_tool_messages(n_tool=5, tool_size=3000, extra=3)
        usage = _usage(msgs)
        # ratio 落在 EMERGENCY（>=5.0）
        mgr = ContextBudgetManager(max_tokens=int(usage / 6), keep_recent=2)
        result = mgr.compress(msgs)

        # head(2) + last(1) = 3
        assert len(result) == 3
        assert result[0] == msgs[0]
        assert result[1] == msgs[1]
        assert result[-1] == msgs[-1]


class TestStats:
    """get_stats 暴露最近压缩级别。"""

    def test_stats_include_last_level(self):
        msgs = _make_tool_messages(n_tool=5, tool_size=3000, extra=3)
        usage = _usage(msgs)
        mgr = ContextBudgetManager(max_tokens=int(usage / 6), keep_recent=2)
        mgr.compress(msgs)
        stats = mgr.get_stats()
        assert stats["last_level"] == CompressionLevel.EMERGENCY.name
