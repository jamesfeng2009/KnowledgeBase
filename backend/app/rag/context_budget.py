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

import os
import re
import tempfile
import uuid
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
# P0-2: 五级渐进压缩 — 按 ratio 阈值分级触发，从"尽量保真"到"只保命"
# ======================================================================
#
# 背景：原实现是"单级三段式一刀切压缩"（超预算即把中间消息全压成一条摘要）。
# 粒度太粗——从"最近 N 轮原文"直接跳到"全部摘要"，中间没有过渡，且每次都
# 牺牲全部中间信息。五级压缩让每一级只做最小的必要牺牲，最大限度保留有用信息。
#
# ratio = 当前总 token / 窗口预算（_WINDOW_BUDGET）
#   窗口预算取 think 预算的 2 倍作为"满窗"参考，使原 2000 tok 触发点
#   恰好落在 Level 2（50%）——保持向后兼容的触发时机，同时引入分级升级。
#
# 压缩黄金法则：永远从 L3（最老的历史/工具日志）开始压，L0（system+query）
# 与 L1（当前状态）是禁区。级别只升不降，不会叠加。

from enum import Enum


class CompressionLevel(Enum):
    """五级渐进压缩：0-4 从保真到保命。"""
    NONE = 0            # < 50%：无压缩
    TOOL_COMPRESS = 1   # 50-70%：工具结果压缩（首500+尾200+中间截断）
    HISTORY_SUMMARY = 2 # 70-85%：历史摘要（关键动作轨迹）
    TOPIC_SUMMARY = 3   # 85-92%：主题级摘要（更激进，保留更少近期）
    EMERGENCY = 4       # > 92%：紧急模式（只留 system+query+最近1轮）


# 五级压缩触发阈值（ratio = 当前 token / max_tokens）
#   ratio < 1.0            → NONE（未超预算，should_compress 已拦截）
#   1.0  <= ratio < 1.5    → TOOL_COMPRESS（刚超预算，轻触：仅压超长工具结果）
#   1.5  <= ratio < 3.0    → HISTORY_SUMMARY（历史摘要，保留最近 keep_recent 轮）
#   3.0  <= ratio < 5.0    → TOPIC_SUMMARY（主题级摘要，只保留最近 1 轮）
#   ratio >= 5.0           → EMERGENCY（紧急模式，只留 system+query+最近 1 轮）
_TOOL_COMPRESS_AT: float = 1.0
_HISTORY_SUMMARY_AT: float = 1.5
_TOPIC_SUMMARY_AT: float = 3.0
_EMERGENCY_AT: float = 5.0

# Level 2 工具结果压缩参数
_TOOL_RESULT_COMPRESS_THRESHOLD: int = 2000  # 超过此字符数的工具结果才压缩
_TOOL_RESULT_HEAD_CHARS: int = 500           # 保留首部（字段名/结构）
_TOOL_RESULT_TAIL_CHARS: int = 200           # 保留尾部（状态码/结论）

# ======================================================================
# P1-1: 大工具结果写盘（Spill to Disk）
# ======================================================================
#
# 背景：一次检索/查询可能返回数千 token 的工具结果，即使经 _summarize
# 截断到 300 字，原始大结果仍会撑大 L3（历史/工具日志）体量。写盘机制把
# 超大原始结果写到磁盘，think 上下文只留一个 placeholder + 相对路径，
# Agent 需要全文时再通过 read_tool_result 工具按需读回。
#
# 安全：路径含 tenant_id 防跨租户泄漏；read 时校验解析后路径必须落在
# 基础目录内，防止路径穿越读取任意文件。
_SPILL_TOOL_RESULT_THRESHOLD: int = 2000  # 原始结果超过此字符数即写盘
_DEFAULT_SPILL_DIR: str = os.path.join(tempfile.gettempdir(), "ekb_spill")


class SpillStore:
    """大工具结果写盘存储 — 超长内容写盘，上下文只留 placeholder。

    写入本地临时目录（默认 ``{tempdir}/ekb_spill``），相对路径含
    ``tenant_id`` 前缀防跨租户泄漏。``read`` 校验路径不越界。
    """

    def __init__(self, base_dir: str | None = None) -> None:
        self._base_dir = base_dir or _DEFAULT_SPILL_DIR

    def spill(self, tenant_id: str, key: str, content: str) -> str:
        """写入内容并返回相对路径（含 tenant_id 前缀）。

        Args:
            tenant_id: 租户标识，用于路径隔离。
            key: 内容标识（如工具名）。
            content: 原始内容。

        Returns:
            相对路径（相对 base_dir），供上下文 placeholder 引用。
        """
        safe_tenant = re.sub(r"[^A-Za-z0-9_-]", "_", str(tenant_id)) or "default"
        safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", str(key)) or "result"
        rel = os.path.join(safe_tenant, f"{safe_key}_{uuid.uuid4().hex[:8]}.txt")
        abs_path = os.path.join(self._base_dir, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return rel

    def read(self, rel_path: str) -> str:
        """按相对路径读回内容，校验路径不越界。

        Args:
            rel_path: spill() 返回的相对路径。

        Returns:
            原始内容。

        Raises:
            ValueError: 路径越界（路径穿越攻击）。
            FileNotFoundError: 文件不存在。
        """
        abs_path = os.path.realpath(os.path.join(self._base_dir, rel_path))
        base = os.path.realpath(self._base_dir)
        if not abs_path.startswith(base + os.sep):
            raise ValueError("spill path escapes base dir")
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

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
        """按五级渐进压缩级别压缩消息列表。

        P0-2: 由 ratio（当前 token / 窗口预算）决定压缩级别，从"尽量保真"
        到"只保命"逐级升级，每一级只做最小的必要牺牲：

        - Level 0 NONE（<50%）：不压缩
        - Level 1 TOOL_COMPRESS（50-70%）：仅压缩超长工具结果（首500+尾200）
        - Level 2 HISTORY_SUMMARY（70-85%）：历史摘要（三段式，保留最近 keep_recent 轮）
        - Level 3 TOPIC_SUMMARY（85-92%）：主题级摘要（更激进，只保留最近 1 轮）
        - Level 4 EMERGENCY（>92%）：只留 system+query+最近 1 轮

        压缩黄金法则：永远从 L3（最老历史/工具日志）开始压，L0（system+query）
        与 L1（当前状态）是禁区。级别只升不降，不会叠加。

        Args:
            messages: 原始消息列表。
            scratchpad: P3-E Scratchpad 内容（可选）。

        Returns:
            压缩后的消息列表。
        """
        if len(messages) <= 2 + self._keep_recent:
            return messages

        before_tokens = self.estimate_tokens(messages)
        level = self._compute_level(before_tokens)

        if level == CompressionLevel.NONE:
            return messages
        if level == CompressionLevel.TOOL_COMPRESS:
            result = self._compress_tool_results(messages)
        elif level == CompressionLevel.HISTORY_SUMMARY:
            result = self._compress_history_summary(
                messages, scratchpad, keep_recent=self._keep_recent
            )
        elif level == CompressionLevel.TOPIC_SUMMARY:
            result = self._compress_history_summary(
                messages, scratchpad, keep_recent=1
            )
        else:  # EMERGENCY
            result = self._compress_emergency(messages)

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
            "level": level.name,
        }

        log.info(
            "context_budget.compressed",
            level=level.name,
            ratio=round(before_tokens / self._max_tokens, 3),
            before_msgs=len(messages),
            after_msgs=len(result),
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            tokens_saved=saved,
            compress_count=self._compress_count,
        )

        return result

    def _compute_level(self, usage: int) -> CompressionLevel:
        """根据当前 token 用量与 max_tokens 预算的 ratio 计算压缩级别。

        ratio = usage / max_tokens，衡量"超出预算多少倍"：
        刚超预算轻触（TOOL_COMPRESS），越超越激进，直至 EMERGENCY 只保命。
        """
        ratio = usage / self._max_tokens
        if ratio < _TOOL_COMPRESS_AT:
            return CompressionLevel.NONE
        if ratio < _HISTORY_SUMMARY_AT:
            return CompressionLevel.TOOL_COMPRESS
        if ratio < _TOPIC_SUMMARY_AT:
            return CompressionLevel.HISTORY_SUMMARY
        if ratio < _EMERGENCY_AT:
            return CompressionLevel.TOPIC_SUMMARY
        return CompressionLevel.EMERGENCY

    def _compress_tool_results(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Level 1: 工具结果压缩 — 仅压缩超长工具结果，其余消息原样保留。

        保留首部（字段名/结构，前 500 字）+ 尾部（状态码/结论，后 200 字），
        中间截断。这是最轻的一级，不触碰历史摘要，最大限度保真。
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content", "")
            if (
                isinstance(content, str)
                and "[系统] 工具结果：" in content
                and len(content) > _TOOL_RESULT_COMPRESS_THRESHOLD
            ):
                head = content[:_TOOL_RESULT_HEAD_CHARS]
                tail = content[-_TOOL_RESULT_TAIL_CHARS:]
                result.append(
                    {**msg, "content": head + "\n…[中间已压缩]…\n" + tail}
                )
            else:
                result.append(msg)
        return result

    def _compress_history_summary(
        self,
        messages: list[dict[str, Any]],
        scratchpad: str,
        keep_recent: int,
    ) -> list[dict[str, Any]]:
        """Level 2/3: 历史摘要 — 三段式（head + 摘要 + tail）。

        head（system+query，L0 禁区）与 tail（最近 keep_recent 轮，Live Zone）
        原样保留，中间历史压缩为单条摘要。keep_recent 越小越激进。

        P3-E: Scratchpad 作为高密度信息追加到摘要末尾（截断到 200 字）。
        """
        head = messages[:2]  # system + query（KV Cache 前缀，不动）
        tail = messages[-keep_recent:]  # Live Zone（最近上下文，不动）
        middle = messages[2:-keep_recent]  # 待压缩的中间消息

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

        return head + [compressed_msg] + tail

    def _compress_emergency(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Level 4: 紧急模式 — 只留 system+query（L0）+ 最近 1 轮（L1 状态）。

        当 ratio > 92% 时触发，牺牲全部历史与工具日志，只保命。
        """
        head = messages[:2]  # system + query（L0 禁区）
        tail = messages[-1:]  # 最近 1 轮（L1 当前状态）
        return head + tail

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
        stats: dict[str, int] = {
            "compress_count": self._compress_count,
            "total_tokens_saved": self._total_tokens_saved,
        }
        # P0-2: 暴露最近一次压缩级别，便于观测分级切换
        if self._last_snapshot and "level" in self._last_snapshot:
            stats["last_level"] = self._last_snapshot["level"]
        return stats

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
