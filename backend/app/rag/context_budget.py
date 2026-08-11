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

# 压缩摘要中每条消息的最大保留长度
_COMPRESSED_MSG_MAX_CHARS: int = 80

# ======================================================================
# P1-5: CJK 感知的 token 估算
# ======================================================================
#
# 原 _CHARS_PER_TOKEN=3.5 对中英文混合场景存在系统性低估：
#   - 中文实际约 1.5-2 字符/token（BPE 分词后每个汉字常独立成 token）
#   - 英文实际约 4 字符/token
#   - 用统一 3.5 估算 1000 字纯中文文档 → 估算 285 token，
#     但真实 token 数可能高达 500-666，导致上下文预算被悄悄击穿。
#
# 改造策略（故意高估，保守优先）：
#   - CJK 字符：1.5 字符/token（每个汉字 ≈ 0.67 token，比真实略高，宁早压缩不超限）
#   - 非 CJK 字符：4.0 字符/token（标准英文估算）
#   - 混合文本按字符类别分别累计，避免一刀切。
#
# CJK Unicode 范围（覆盖中日韩 + 全角符号 + 假名 + 谚文）：
#   - U+3000-U+303F: CJK 符号和标点（　、。「」等）
#   - U+3040-U+309F: 平假名
#   - U+30A0-U+30FF: 片假名
#   - U+3400-U+4DBF: CJK 扩展 A
#   - U+4E00-U+9FFF: CJK 统一表意文字（基本汉字）
#   - U+F900-U+FAFF: CJK 兼容表意文字
#   - U+FF00-U+FFEF: 全角形式（全角字母/数字/符号）
#   - U+20000-U+2A6DF: CJK 扩展 B（罕见汉字， Python 用 \U 转义）
# 保留 _CHARS_PER_TOKEN 仅为向后兼容引用，新代码请用下方 CJK 感知估算。

# 旧系数（向后兼容引用，不应在新代码中使用）
_CHARS_PER_TOKEN: float = 3.5

# CJK 字符的 token 估算系数（字符/token，越小代表每个字符消耗越多 token）
_CJK_CHARS_PER_TOKEN: float = 1.5

# 非 CJK 字符的 token 估算系数（英文/数字/半角标点，约 4 字符/token）
_NON_CJK_CHARS_PER_TOKEN: float = 4.0

# CJK 字符 Unicode 范围元组（用于快速判断）
# 按 code point 升序排列，便于 bisect 或线性扫描
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),    # CJK 符号和标点
    (0x3040, 0x309F),    # 平假名
    (0x30A0, 0x30FF),    # 片假名
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 统一表意文字（基本汉字）
    (0xF900, 0xFAFF),    # CJK 兼容表意文字
    (0xFF00, 0xFFEF),    # 全角形式
    (0x20000, 0x2A6DF),  # CJK 扩展 B
    (0x2A700, 0x2B73F),  # CJK 扩展 C
    (0x2B740, 0x2B81F),  # CJK 扩展 D
    (0x2B820, 0x2CEAF),  # CJK 扩展 E
    (0x2CEB0, 0x2EBEF),  # CJK 扩展 F
)


def _is_cjk_char(ch: str) -> bool:
    """判断单个字符是否为 CJK 字符（中日韩文字 + 全角符号 + 假名 + 谚文）。

    用于 CJK 感知的 token 估算。空字符返回 False。
    """
    if not ch:
        return False
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if cp < lo:
            # 当前字符 code point 小于范围下界，后续范围更大，可短路
            return False
        if cp <= hi:
            return True
    return False


def _count_cjk_chars(text: str) -> int:
    """统计文本中的 CJK 字符数量。"""
    if not text:
        return 0
    return sum(1 for ch in text if _is_cjk_char(ch))


def estimate_tokens_for_text(text: str) -> int:
    """CJK 感知的 token 估算 — 对单段文本估算 token 数。

    CJK 字符按 1.5 字符/token 估（故意高估，保守优先），
    非 CJK 字符按 4.0 字符/token 估（标准英文估算）。

    Args:
        text: 待估算的文本。

    Returns:
        估算的 token 数（至少为 0）。
    """
    if not text:
        return 0
    cjk_count = _count_cjk_chars(text)
    non_cjk_count = len(text) - cjk_count
    tokens = cjk_count / _CJK_CHARS_PER_TOKEN + non_cjk_count / _NON_CJK_CHARS_PER_TOKEN
    return int(tokens)


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
        """估算消息列表的总 token 数（CJK 感知）。

        P1-5: 区分 CJK 字符和非 CJK 字符分别估算，
        中文按 1.5 字符/token（故意高估，保守优先），
        英文/数字/标点按 4.0 字符/token。
        旧版统一 3.5 系数会系统性低估中文 token 数，
        导致上下文预算被悄悄击穿。

        Args:
            messages: 消息列表（每条含 role 和 content）。

        Returns:
            估算的 token 数。
        """
        total_tokens = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total_tokens += estimate_tokens_for_text(content)
            elif content:
                # 非 str 类型（如 list/dict）降级用字符长度估算
                total_tokens += estimate_tokens_for_text(str(content))
        return total_tokens

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
