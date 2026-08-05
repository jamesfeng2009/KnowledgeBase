"""
上下文预算管理器 — 当 Agent Loop 的 think 上下文超过 token 预算时，压缩早期消息。

借鉴 Headroom 项目的 Memory Budget + Time Decay 设计。Agent Loop 多轮迭代中，
每轮都会向 messages 列表追加工具结果摘要。即使经过 P1-Opt3 跨轮去重，
在 5 次迭代后 messages 仍可能累积到 2500+ tokens，挤占 LLM 上下文窗口。

核心策略（三段式压缩）：
- Head（前 2 条）：system prompt + user query — 永不压缩（保持 KV Cache 前缀稳定）
- Middle（中间消息）：压缩为单条摘要消息，保留关键动作轨迹
- Tail（最近 N 条）：保留原文（Live Zone，当前轮次的活跃上下文）

两个硬不变量（与 Headroom Memory Budget 一致）：
1. Head 不变性：system + query 始终保留，保证 KV Cache 命中
2. 信息保真：压缩摘要保留每条消息的关键动作类型和核心数据指针，
   原始完整内容保存在 state["retrieved_docs"] 和 state["tool_results"] 中

遵循单一职责：本模块只负责 token 估算和消息压缩，不修改 Agent Loop 逻辑。
"""

from __future__ import annotations

from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# Think 上下文的最大 token 预算 — 超过此值触发压缩
_MAX_THINK_TOKENS: int = 2000  # ~7000 字符，为生成阶段留出足够空间

# 压缩时保留的最近消息条数（不压缩的 Live Zone 大小）
_KEEP_RECENT: int = 2

# 中英文混合 token 估算系数：约 1 token ≈ 3.5 字符
_CHARS_PER_TOKEN: float = 3.5

# 压缩摘要中每条消息的最大保留长度
_COMPRESSED_MSG_MAX_CHARS: int = 80


class ContextBudgetManager:
    """上下文预算管理器 — 监控并压缩 Agent Loop 的 think 上下文。

    使用方式::

        budget = ContextBudgetManager()
        # 每轮迭代后检查
        if budget.should_compress(messages):
            messages = budget.compress(messages)
            # messages 现在更短，但保留了关键信息
    """

    def __init__(
        self,
        max_tokens: int = _MAX_THINK_TOKENS,
        keep_recent: int = _KEEP_RECENT,
    ) -> None:
        """初始化上下文预算管理器。

        Args:
            max_tokens: think 上下文的最大 token 预算，超过则触发压缩。
            keep_recent: 压缩时保留的最近消息条数（不压缩的 Live Zone）。
        """
        self._max_tokens = max_tokens
        self._keep_recent = keep_recent
        # 压缩统计（用于日志和监控）
        self._compress_count: int = 0
        self._total_tokens_saved: int = 0
        # P1-6: 最近一次压缩的前后快照 — 供压缩信息损耗评估双跑对比
        self._last_snapshot: dict[str, Any] | None = None

    @staticmethod
    def estimate_tokens(messages: list[dict[str, Any]]) -> int:
        """估算消息列表的总 token 数。

        使用字符数 / 3.5 的粗略估算（中英文混合场景的保守值）。

        Args:
            messages: 消息列表（每条含 role 和 content）。

        Returns:
            估算的 token 数。
        """
        total_chars = sum(len(m.get("content", "")) for m in messages)
        return int(total_chars / _CHARS_PER_TOKEN)

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """判断是否需要压缩。

        当总 token 数超过 max_tokens 且消息数多于 (2 + keep_recent) 时返回 True。
        前者确保只在真正超预算时压缩，后者确保至少有中间消息可压缩。

        Args:
            messages: 当前消息列表。

        Returns:
            True 如果需要压缩。
        """
        if len(messages) <= 2 + self._keep_recent:
            return False
        return self.estimate_tokens(messages) > self._max_tokens

    def compress(
        self,
        messages: list[dict[str, Any]],
        scratchpad: str = "",
    ) -> list[dict[str, Any]]:
        """压缩消息列表 — 三段式策略。

        保留前 2 条（system + query）和最后 keep_recent 条，
        中间消息压缩为单条摘要消息。

        P3-E: Scratchpad 作为高密度信息追加到摘要末尾（截断到 200 字），
        保证推理轨迹在压缩后仍可被 LLM 参考。

        压缩后的消息结构::

            [system, query, "[系统] 早期上下文摘要：...；推理轨迹:...", recent_msg1, recent_msg2]

        Args:
            messages: 原始消息列表。
            scratchpad: P3-E Scratchpad 内容（可选）。

        Returns:
            压缩后的消息列表（可能比原始列表短）。
        """
        if len(messages) <= 2 + self._keep_recent:
            return messages

        # 三段式切分
        head = messages[:2]  # system + query（KV Cache 前缀，不动）
        tail = messages[-self._keep_recent:]  # Live Zone（最近上下文，不动）
        middle = messages[2:-self._keep_recent]  # 待压缩的中间消息

        before_tokens = self.estimate_tokens(messages)

        # 将中间消息压缩为单条摘要
        summary_parts: list[str] = []
        for msg in middle:
            compressed = self._compress_single_message(msg.get("content", ""))
            if compressed:
                summary_parts.append(compressed)

        # P3-E: Scratchpad 作为高密度信息追加到摘要
        if scratchpad:
            # 保留 Scratchpad 最后 200 字（最新的推理笔记）
            recent_sp = scratchpad[-200:] if len(scratchpad) > 200 else scratchpad
            summary_parts.append(f"推理轨迹:{recent_sp}")

        if not summary_parts:
            # 中间消息无有效内容，直接拼接 head + tail
            return head + tail

        compressed_msg: dict[str, Any] = {
            "role": "user",
            "content": "[系统] 早期上下文摘要：" + "；".join(summary_parts),
        }

        result = head + [compressed_msg] + tail

        after_tokens = self.estimate_tokens(result)
        saved = before_tokens - after_tokens
        self._compress_count += 1
        self._total_tokens_saved += max(0, saved)

        # P1-6: 记录压缩前后快照 — 压缩信息损耗评估（实体保留率 / 一致性双跑）
        self._last_snapshot = {
            "before": [dict(m) for m in messages],
            "after": [dict(m) for m in result],
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
        }

        log.info(
            "context_budget.compressed",
            before_msgs=len(messages),
            after_msgs=len(result),
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            tokens_saved=saved,
            compress_count=self._compress_count,
        )

        return result

    @staticmethod
    def _compress_single_message(content: str) -> str:
        """压缩单条消息内容为简短摘要。

        提取关键动作类型和核心数据指针：
        - "[系统] 已检索到 N 篇文档" → "检索N篇"
        - "[系统] 工具结果：..." → "工具结果:前80字"
        - "[系统] 工具结果：↑ [见第N轮 ...]" → "重复结果(见N轮)"
        - 其他 → 截断到 80 字符

        Args:
            content: 原始消息内容。

        Returns:
            压缩后的摘要文本。
        """
        if not content:
            return ""

        # 检索结果摘要
        if "[系统] 已检索到" in content:
            # 提取 "已检索到 N 篇文档" 中的数字
            import re

            match = re.search(r"已检索到\s*(\d+)\s*篇", content)
            if match:
                return f"检索{match.group(1)}篇"
            return "检索文档"

        # 工具结果 — 指针引用（已被 P1-Opt3 去重）
        if "↑ [见第" in content:
            import re

            match = re.search(r"见第(\d+)轮\s*(\S+)", content)
            if match:
                return f"重复结果(见{match.group(1)}轮{match.group(2)})"
            return "重复结果"

        # 工具结果 — 完整摘要
        if "[系统] 工具结果：" in content:
            # 去掉前缀，截断到最大长度
            raw = content.replace("[系统] 工具结果：", "").strip()
            return f"工具:{raw[:_COMPRESSED_MSG_MAX_CHARS]}"

        # 动态上下文（think 的 live zone 追加消息）
        if "当前状态" in content:
            import re

            iter_match = re.search(r"迭代\s*(\d+)", content)
            if iter_match:
                return f"第{iter_match.group(1)}轮决策"
            return "决策上下文"

        # 兜底：截断到最大长度
        return content[:_COMPRESSED_MSG_MAX_CHARS]

    def get_stats(self) -> dict[str, int]:
        """返回压缩统计信息（用于监控和日志）。

        Returns:
            包含 compress_count 和 total_tokens_saved 的字典。
        """
        return {
            "compress_count": self._compress_count,
            "total_tokens_saved": self._total_tokens_saved,
        }

    def get_last_snapshot(self) -> dict[str, Any] | None:
        """返回最近一次压缩的前后快照（P1-6 压缩信息损耗评估）。

        Returns:
            含 ``before`` / ``after`` 消息列表与对应 token 数的字典；
            未发生过压缩时返回 None。
        """
        return self._last_snapshot

    def reset(self) -> None:
        """重置统计信息（新一轮对话开始时调用）。"""
        self._compress_count = 0
        self._total_tokens_saved = 0
        self._last_snapshot = None
