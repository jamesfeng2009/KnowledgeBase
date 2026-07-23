"""
P3-E Scratchpad 草稿本单元测试。

覆盖：
    - AgentState scratchpad 字段初始化
    - ContextBudgetManager.compress 感知 Scratchpad
    - Scratchpad 截断到 200 字
    - 无 Scratchpad 时的兼容性
"""

import pytest

from app.rag.context_budget import ContextBudgetManager


class TestContextBudgetScratchpad:
    """ContextBudgetManager.compress 的 Scratchpad 感知测试。"""

    def setup_method(self):
        self.budget = ContextBudgetManager(
            max_tokens=100,
            keep_recent=2,
        )

    def test_compress_with_scratchpad(self):
        """压缩时 Scratchpad 追加到摘要。"""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "query"},
            {"role": "user", "content": "[系统] 已检索到 3 篇文档"},
            {"role": "user", "content": "[系统] 工具结果：OA系统返回审批状态"},
            {"role": "user", "content": "[系统] 已检索到 5 篇文档"},
            {"role": "user", "content": "当前状态：迭代 3/5"},
        ]
        scratchpad = "[轮1] retrieve: 检索到 3 篇\n[轮2] tool_call: OA → 审批中"

        result = self.budget.compress(messages, scratchpad=scratchpad)

        # 验证结构：head(2) + compressed(1) + tail(2) = 5
        assert len(result) == 5
        # 验证 Scratchpad 在压缩摘要中
        compressed_msg = result[2]["content"]
        assert "推理轨迹" in compressed_msg
        assert "retrieve" in compressed_msg or "tool_call" in compressed_msg

    def test_compress_without_scratchpad(self):
        """无 Scratchpad 时 — 兼容现有行为。"""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "query"},
            {"role": "user", "content": "[系统] 已检索到 3 篇文档"},
            {"role": "user", "content": "[系统] 工具结果：OA系统返回审批状态"},
            {"role": "user", "content": "[系统] 已检索到 5 篇文档"},
            {"role": "user", "content": "当前状态：迭代 3/5"},
        ]

        result = self.budget.compress(messages, scratchpad="")

        assert len(result) == 5
        compressed_msg = result[2]["content"]
        assert "推理轨迹" not in compressed_msg

    def test_compress_empty_scratchpad(self):
        """空字符串 Scratchpad — 不追加。"""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "query"},
            {"role": "user", "content": "[系统] 已检索到 3 篇文档"},
            {"role": "user", "content": "当前状态：迭代 1/5"},
            {"role": "user", "content": "当前状态：迭代 2/5"},
        ]

        result1 = self.budget.compress(messages, scratchpad="")
        result2 = self.budget.compress(messages)

        # 两者应该等价
        assert len(result1) == len(result2)
        assert result1[2]["content"] == result2[2]["content"]

    def test_scratchpad_truncation(self):
        """Scratchpad 超过 200 字 → 截断保留最后 200 字。"""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "query"},
            {"role": "user", "content": "[系统] 已检索到 3 篇文档"},
            {"role": "user", "content": "当前状态：迭代 1/5"},
            {"role": "user", "content": "当前状态：迭代 2/5"},
        ]
        # 构造超长 Scratchpad
        long_scratchpad = "x" * 500

        result = self.budget.compress(messages, scratchpad=long_scratchpad)

        compressed_msg = result[2]["content"]
        # 验证截断后不超过 200 + 前缀长度
        # "推理轨迹:" = 5 chars, so max 205 chars from scratchpad
        assert "推理轨迹" in compressed_msg
        # 提取推理轨迹部分
        sp_part = compressed_msg.split("推理轨迹:")[-1]
        assert len(sp_part) <= 200

    def test_short_messages_no_compression(self):
        """短消息列表不触发压缩 — Scratchpad 也不追加。"""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "query"},
        ]

        result = self.budget.compress(messages, scratchpad="test scratchpad")

        # 不足 2 + keep_recent 条，不压缩
        assert len(result) == 2
        assert result == messages
