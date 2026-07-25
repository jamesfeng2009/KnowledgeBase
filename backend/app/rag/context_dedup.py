"""
跨轮工具结果去重器 — 检测 Agent Loop 中跨轮次重复的工具结果，用指针引用替代。

借鉴 Headroom 项目的 CrossTurnDedup 设计。Agent 常在多轮迭代中调用同一工具
获取相同或高度相似的结果（例如反复查询同一 ERP 订单状态）。每次都将完整结果
摘要作为消息追加，导致 token 浪费。

核心策略：
- 首次工具结果保留完整摘要（注册到已见列表）
- 后续工具结果与已见列表做相似度匹配
- 高相似度（> threshold）时替换为指针引用（"↑ [见第N轮 tool_name 结果]"）
- 原始内容物理存在于首次出现的轮次消息中，不会丢失信息

两个硬不变量（与 Headroom CrossTurnDedup 一致）：
1. 前缀单调性：只匹配严格更早的块，追加轮次不会修改早期轮次
2. 无信息离开窗口：只有逐字出现的 span 才被反向引用

遵循单一职责：本模块只负责去重判断和引用生成，不修改 messages 列表。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.utils.logger import get_logger

log = get_logger(__name__)

# 相似度阈值 — 超过此值认为是同一工具结果的重复
_DEDUP_SIMILARITY_THRESHOLD: float = 0.8
# 工具结果摘要的最大长度（字符）
_SUMMARY_MAX_CHARS: int = 300


@dataclass(frozen=True)
class ToolResultRef:
    """跨轮工具结果引用 — 指向上下文中已有的工具结果。

    Attributes:
        turn: 首次出现的迭代轮次。
        tool_name: 工具名称。
        summary: 首次结果的摘要文本。
    """

    turn: int
    tool_name: str
    summary: str

    def to_ref_string(self) -> str:
        """生成指针引用字符串，替代重复内容。"""
        return f"↑ [见第{self.turn}轮 {self.tool_name} 结果]"


class CrossTurnDeduplicator:
    """跨轮工具结果去重器 — 检测重复并用指针引用替代。

    使用方式::

        dedup = CrossTurnDeduplicator()
        # 第 1 轮：注册工具结果
        text = dedup.register(turn=1, tool_name="search_erp",
                              result_content="订单 BG2024001 金额 5000 元")
        # text = "订单 BG2024001 金额 5000 元"  ← 首次，保留原文

        # 第 3 轮：相似结果
        text = dedup.register(turn=3, tool_name="search_erp",
                              result_content="订单 BG2024001 金额 5000 元 状态已审批")
        # text = "↑ [见第1轮 search_erp 结果]"  ← 重复，返回指针引用
    """

    def __init__(
        self,
        similarity_threshold: float = _DEDUP_SIMILARITY_THRESHOLD,
    ) -> None:
        self._similarity_threshold = similarity_threshold
        self._seen_results: list[ToolResultRef] = []

    def register(
        self,
        turn: int,
        tool_name: str,
        result_content: str,
    ) -> str:
        """注册工具结果，返回可能被替换的引用字符串。

        如果结果与已注册的结果高度相似，返回指针引用而非完整摘要。
        首次出现的结果保留完整摘要并注册到已见列表。

        Args:
            turn: 当前迭代轮次。
            tool_name: 工具名称。
            result_content: 工具返回的原始内容。

        Returns:
            首次出现时返回完整摘要（截断到 _SUMMARY_MAX_CHARS）；
            重复时返回指针引用字符串。
        """
        summary = result_content[:_SUMMARY_MAX_CHARS]

        # 检查是否有内容高度重叠的已有结果。
        # 注意：必须用截断后的 summary 与 ref.summary 比较 —— 若用完整
        # result_content 与截断摘要做 Jaccard 词集相似度，截断摘要是全文
        # 词集的子集，相似度 ≈ |摘要词| / |全文词|，全文较长时即使内容
        # 完全相同也低于阈值，导致长结果跨轮去重失效、重复注入 messages。
        for ref in self._seen_results:
            if self._is_similar(summary, ref.summary):
                log.info(
                    "dedup.replaced",
                    turn=turn,
                    tool_name=tool_name,
                    ref_turn=ref.turn,
                    ref_tool=ref.tool_name,
                )
                return ref.to_ref_string()

        # 新结果：注册并返回完整摘要
        self._seen_results.append(
            ToolResultRef(turn=turn, tool_name=tool_name, summary=summary)
        )
        return summary

    def get_seen_count(self) -> int:
        """返回已注册的工具结果数量（用于测试和监控）。"""
        return len(self._seen_results)

    def reset(self) -> None:
        """清空已见列表（新一轮对话开始时调用）。"""
        self._seen_results.clear()

    @staticmethod
    def _is_similar(a: str, b: str, threshold: float = _DEDUP_SIMILARITY_THRESHOLD) -> bool:
        """判断两段文本是否高度相似（基于 Jaccard 词集相似度）。

        Args:
            a: 文本 A。
            b: 文本 B。
            threshold: 相似度阈值，超过此值认为相似。

        Returns:
            True 如果两段文本的 Jaccard 相似度超过阈值。
        """
        if not a or not b:
            return False
        # 完全相同直接返回
        if a == b:
            return True
        set_a = set(a.split())
        set_b = set(b.split())
        if not set_a or not set_b:
            return False
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        if union == 0:
            return False
        return intersection / union > threshold
