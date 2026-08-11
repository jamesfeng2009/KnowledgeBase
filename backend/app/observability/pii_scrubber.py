"""
PII Scrubber — LangFuse span export 前对 input/output/metadata 做 PII 脱敏。

设计目标（P1-6）：
    LangFuse 是外部可观测系统（SaaS 或自托管），span 的 input/output/metadata
    会携带 Agent Loop 的实际推理内容（query / retrieved_docs / tool_results /
    LLM 输出），其中可能包含用户输入的 PII（手机号 / 身份证 / 邮箱 / 银行卡）。
    在 span 发送给 LangFuse 之前必须脱敏，否则等同于将 PII 泄露给第三方观测平台。

接入点（langfuse_tracer.py）：
    1. TraceContext.end_span()    — 节点 Span 闭合时
    2. TraceContext.span()        — 一次性记录已闭合 Span 时
    3. TraceContext.finalize()    — Trace 收尾更新 output/metadata 时

同时作用于本地 SpanRecord（双写分支 1）：input_ref / output_ref / metadata
同样是观测数据，本地记录不应比 LangFuse 侧更宽松。

正则来源：
    与 app/finetune/data_cleaner.py 保持一致（手机号 / 身份证 / 邮箱 / 银行卡），
    未来可抽取到 app/utils/pii_patterns.py 共享。当前为避免 P1-6 改动扩散，
    暂时在 observability 层独立维护。

替换顺序（关键不变量）：
    身份证（18 位）→ 银行卡（16-19 位）→ 手机号（11 位）→ 邮箱
    长正则先替换，避免被短正则吞并子串。例如未先替换身份证时，
    "110101199003070019" 会被银行卡正则（16-19 位）整段命中，导致
    身份证号被错误归为银行卡。

单一职责：本模块只做 PII 脱敏改写，不做内容过滤/剔除/打标（那是 finetune 的事）。
"""

from __future__ import annotations

import re
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# ======================================================================
# PII 正则 — 与 app/finetune/data_cleaner.py 保持一致
# ======================================================================
#: 中国大陆手机号：1 开头 + 第二位 3-9，共 11 位，前后边界不能是数字
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
#: 身份证：17 位数字 + 校验位（数字或 X），前后边界不能是数字/字母
_IDCARD_RE = re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")
#: 邮箱
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
#: 银行卡：16-19 位连续数字，前后边界不能是数字
_BANKCARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")

#: 脱敏占位符（与 finetune/data_cleaner 保持一致，便于跨模块对账）
MASK_PHONE = "[PHONE]"
MASK_IDCARD = "[IDCARD]"
MASK_EMAIL = "[EMAIL]"
MASK_BANKCARD = "[BANKCARD]"

#: 各 PII 类型 → 占位符映射（用于统计和调试）
_MASK_BY_TYPE: dict[str, str] = {
    "idcard": MASK_IDCARD,
    "bankcard": MASK_BANKCARD,
    "phone": MASK_PHONE,
    "email": MASK_EMAIL,
}

#: 按替换顺序排列的 (类型名, 正则, 占位符) 三元组
#: 身份证先于银行卡（避免 18 位身份证被 16-19 位银行卡正则吞并），
#: 手机号放最后（边界断言已保证不命中长数字串内部）。
_SCRUB_ORDER: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("idcard", _IDCARD_RE, MASK_IDCARD),
    ("bankcard", _BANKCARD_RE, MASK_BANKCARD),
    ("phone", _PHONE_RE, MASK_PHONE),
    ("email", _EMAIL_RE, MASK_EMAIL),
)


class PIIScrubber:
    """PII 脱敏器 — 对文本或任意嵌套结构递归脱敏。

    使用方式::

        scrubber = PIIScrubber(enabled=True)
        safe_text = scrubber.scrub_text("联系我: 13800138000")
        safe_meta = scrubber.scrub_value({"user": "张三 <zs@example.com>"})

    线程安全：实例仅持有计数器（int 自增非原子，但本场景单 Trace 串行调用，
    不存在并发问题；如需并发可改为 threading.Lock）。
    """

    def __init__(self, enabled: bool = True) -> None:
        """初始化 PII 脱敏器。

        Args:
            enabled: 是否启用脱敏。False 时所有 scrub_* 原样返回输入，
                     用于配置关闭或测试场景。
        """
        self._enabled = enabled
        # 命中统计 — 按 PII 类型计数，供监控/日志消费
        self._hit_counts: dict[str, int] = {name: 0 for name, _, _ in _SCRUB_ORDER}
        # 被脱敏的字段总数（每次 scrub_span_io 调用计一次）
        self._field_scrub_count: int = 0

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """是否启用脱敏。"""
        return self._enabled

    def scrub_text(self, text: str) -> str:
        """对纯文本做 PII 脱敏改写。

        按替换顺序依次应用四类正则。未命中时原样返回。

        Args:
            text: 原始文本。

        Returns:
            脱敏后的文本。
        """
        if not self._enabled or not text:
            return text

        masked = text
        for name, pattern, replacement in _SCRUB_ORDER:
            # subn 返回 (新文本, 替换次数)
            masked, count = pattern.subn(replacement, masked)
            if count > 0:
                self._hit_counts[name] += count
        return masked

    def scrub_value(self, value: Any) -> Any:
        """递归对任意嵌套结构做 PII 脱敏。

        支持 str / dict / list / tuple，其他类型原样返回。
        dict 的 key 不脱敏（key 通常是字段名，不含 PII）；
        tuple 脱敏后转为 list（避免 tuple 不可变导致递归困难）。

        Args:
            value: 待脱敏的值（任意类型）。

        Returns:
            脱敏后的值；类型可能从 tuple 变为 list，其他保持原类型。
        """
        if not self._enabled:
            return value

        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {k: self.scrub_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub_value(v) for v in value]
        if isinstance(value, tuple):
            # tuple 不可变，转 list 递归后保持 list（调用方通常不依赖 tuple 类型）
            return [self.scrub_value(v) for v in value]
        return value

    def scrub_span_io(
        self,
        input_data: Any,
        output_data: Any,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Any, Any, dict[str, Any]]:
        """一次性对 span 的 input/output/metadata 三字段脱敏。

        LangFuse span export 的标准三字段批处理入口。
        metadata 为 None 时返回空 dict（与 LangFuse SDK 期望一致）。

        Args:
            input_data: span.input 字段值。
            output_data: span.output 字段值。
            metadata: span.metadata 字段值（None 时按空 dict 处理）。

        Returns:
            (scrubbed_input, scrubbed_output, scrubbed_metadata) 三元组。
        """
        if not self._enabled:
            return input_data, output_data, metadata or {}

        scrubbed_input = self.scrub_value(input_data)
        scrubbed_output = self.scrub_value(output_data)
        scrubbed_metadata = self.scrub_value(metadata or {}) if metadata else {}
        # scrub_value 对 dict 返回 dict，但类型注解层面 Any 已覆盖
        if not isinstance(scrubbed_metadata, dict):
            scrubbed_metadata = {}
        self._field_scrub_count += 1
        return scrubbed_input, scrubbed_output, scrubbed_metadata

    # ------------------------------------------------------------------
    # 统计与诊断
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """返回脱敏统计信息（用于监控和日志）。

        Returns:
            含 ``hit_counts``（按 PII 类型）和 ``field_scrub_count``
            （span 字段批处理次数）的字典。
        """
        return {
            "enabled": self._enabled,
            "hit_counts": dict(self._hit_counts),
            "total_hits": sum(self._hit_counts.values()),
            "field_scrub_count": self._field_scrub_count,
        }

    def reset(self) -> None:
        """重置统计（新一轮 Trace 开始时调用）。"""
        self._hit_counts = {name: 0 for name, _, _ in _SCRUB_ORDER}
        self._field_scrub_count = 0


# ======================================================================
# 模块级单例 — 从 config 读取开关，避免每个 TraceContext 重复初始化
# ======================================================================

_default_scrubber: PIIScrubber | None = None


def get_default_scrubber() -> PIIScrubber:
    """获取全局默认 PIIScrubber 单例。

    首次调用时从 app.config 读取 LANGFUSE_PII_SCRUB_ENABLED 配置，
    决定是否启用脱敏。后续调用复用单例。

    config 读取失败时默认启用（保守优先，宁可误脱敏不可漏脱敏）。
    """
    global _default_scrubber
    if _default_scrubber is not None:
        return _default_scrubber

    enabled = True  # 保守默认：读取失败时启用
    try:
        from app.config import get_settings

        settings = get_settings()
        enabled = bool(getattr(settings, "LANGFUSE_PII_SCRUB_ENABLED", True))
    except Exception as exc:
        log.warning("pii_scrubber.config_read_error", error=str(exc), default="enabled")

    _default_scrubber = PIIScrubber(enabled=enabled)
    log.info("pii_scrubber.initialized", enabled=enabled)
    return _default_scrubber


def reset_default_scrubber() -> None:
    """重置全局单例（测试用，生产代码不应调用）。

    下次 get_default_scrubber() 会重新从 config 读取。
    """
    global _default_scrubber
    _default_scrubber = None
