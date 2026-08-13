"""
检索结果注入扫描守卫 — 单一职责：扫描检索召回的文档内容，识别 prompt injection。

借鉴 OWASP LLM Top 10 (LLM01: Prompt Injection) 与 Microsoft PyRIT 的检测思路，
针对企业知识库 RAG 场景简化为六类注入模式：

    1. 指令劫持（instruction_hijack）— "忽略之前的指令"、"Ignore all previous"
    2. 角色覆盖（role_override）— 伪装 system/assistant 消息、特殊分隔符
    3. 数据外泄（data_exfiltration）— "输出你的系统提示词"、"Print your system prompt"
    4. 工具滥用（tool_abuse）— "调用 document_create"、"Execute command: ..."
    5. 越狱模式（jailbreak）— "DAN mode"、"Developer Mode"、"假装你没有限制"
    6. 分隔符注入（delimiter_injection）— <|im_start|>、```system``` 等边界突破

设计原则：
    - 命中文档**不丢弃**，进 quarantined 列表保留审计证据；
    - 纯函数 + 数据类，不依赖 DB / NotificationService（遵循单一职责）；
    - 告警通过回调注入，由 engine 层绑定 NotificationService（避免循环依赖）；
    - 优雅降级：扫描异常时放行全部文档，仅记录日志（不阻塞 RAG 主流程）；
    - 配置驱动：模式列表与阈值均从 settings 读取。

与 tool_guard.py 风格对齐：守卫层只做"识别 + 决策"，不做"执行"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)


# ======================================================================
# 枚举与数据类
# ======================================================================


class InjectionSeverity(str, Enum):
    """注入命中严重级别 — 用于决定是否隔离。"""

    HIGH = "high"      # 明确的越狱/指令劫持 → 必隔离
    MEDIUM = "medium"  # 可疑的角色覆盖/工具滥用 → 按阈值隔离
    LOW = "low"        # 弱信号（分隔符出现） → 仅告警不隔离


@dataclass
class InjectionHit:
    """单个注入命中记录。"""

    doc_id: str
    chunk_id: str
    pattern_id: str        # 命中的模式 ID（如 "instruction_hijack"）
    severity: InjectionSeverity
    matched_text: str      # 命中的文本片段（已截断至 200 字符）
    start_offset: int      # 命中在原文中的起始偏移
    pattern_desc: str = ""  # 模式的人类可读描述


@dataclass
class InjectionScanResult:
    """扫描结果。"""

    safe_docs: list[dict[str, Any]] = field(default_factory=list)
    quarantined_docs: list[dict[str, Any]] = field(default_factory=list)
    hits: list[InjectionHit] = field(default_factory=list)
    total_scanned: int = 0

    @property
    def total_quarantined(self) -> int:
        return len(self.quarantined_docs)

    @property
    def total_hits(self) -> int:
        return len(self.hits)


# 告警回调类型 — 由 engine 层注入实际实现（调用 NotificationService）
# 失败时仅记录日志，不影响主流程。回调签名：
#   async def alert_callback(scan_result: InjectionScanResult) -> None
AlertCallback = Callable[["InjectionScanResult"], Awaitable[None]]


# ======================================================================
# 注入模式定义
# ======================================================================


@dataclass(frozen=True)
class _InjectionPattern:
    """单个注入检测模式。"""

    pattern_id: str
    severity: InjectionSeverity
    description: str
    # 多个正则任一命中即视为命中（OR 语义）
    regexes: tuple[re.Pattern[str], ...]


# ------------------------------------------------------------------
# 六类注入模式 — 中英双语匹配，覆盖企业知识库主要风险面
# ------------------------------------------------------------------
# 注：正则使用 re.IGNORECASE，大小写不敏感；re.DOTALL 不开启，避免跨段误匹配。

_PATTERN_INSTRUCTION_HIJACK = _InjectionPattern(
    pattern_id="instruction_hijack",
    severity=InjectionSeverity.HIGH,
    description="指令劫持 — 试图覆盖系统指令、重置 AI 行为",
    regexes=(
        re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)", re.IGNORECASE),
        re.compile(r"disregard\s+(?:all\s+)?(?:prior|previous|above)\s+(?:instructions?|prompts?|rules?)", re.IGNORECASE),
        re.compile(r"forget\s+(?:everything|all\s+(?:previous|prior))", re.IGNORECASE),
        # 中文：允许 "的" 与 "所有" 任意组合出现（如 "之前的所有指令"）
        re.compile(r"忽略(?:之前|前面|上面|先前)(?:所有|的)*(?:指令|提示|规则|内容)"),
        re.compile(r"无视(?:之前|前面|上面|先前)(?:所有|的)*(?:指令|提示|规则)"),
        re.compile(r"忘记(?:之前|前面|上面|先前)(?:所有|的)*(?:指令|内容)"),
        re.compile(r"从现在起(?:你|AI)?(?:是|扮演)"),
        re.compile(r"new\s+instructions?\s*[:：]\s*", re.IGNORECASE),
    ),
)

_PATTERN_ROLE_OVERRIDE = _InjectionPattern(
    pattern_id="role_override",
    severity=InjectionSeverity.MEDIUM,
    description="角色覆盖 — 伪装 system/assistant 消息或特殊角色标记",
    regexes=(
        re.compile(r"^\s*system\s*[:：]", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^\s*assistant\s*[:：]", re.IGNORECASE | re.MULTILINE),
        re.compile(r"\[\s*system\s*\]", re.IGNORECASE),
        re.compile(r"\[\s*assistant\s*\]", re.IGNORECASE),
        re.compile(r"<\|system\|>", re.IGNORECASE),
        re.compile(r"<\|assistant\|>", re.IGNORECASE),
        re.compile(r"<\|im_start\|>", re.IGNORECASE),
        re.compile(r"<\|im_end\|>", re.IGNORECASE),
        re.compile(r"#\s*system\s*[:：]", re.IGNORECASE),
    ),
)

_PATTERN_DATA_EXFILTRATION = _InjectionPattern(
    pattern_id="data_exfiltration",
    severity=InjectionSeverity.HIGH,
    description="数据外泄 — 试图诱导 AI 泄露系统提示/配置/可见文档",
    regexes=(
        re.compile(r"(?:print|show|reveal|display)\s+(?:your|the)\s+(?:system\s+)?prompt", re.IGNORECASE),
        re.compile(r"what\s+(?:is|are)\s+your\s+(?:system|initial)\s+(?:prompt|instructions?)", re.IGNORECASE),
        re.compile(r"(?:输出|显示|打印|泄露)(?:你的)?(?:系统|初始)?(?:提示词?|指令|配置|prompt)"),
        re.compile(r"列出(?:所有|全部)?(?:可见|能访问|能看到的)?文档"),
        re.compile(r"(?:列出|show\s+me)\s+(?:all\s+)?(?:documents?|files?|knowledge)", re.IGNORECASE),
        re.compile(r"(?:泄露|exfiltrate|export)\s+(?:all\s+)?(?:data|content)", re.IGNORECASE),
    ),
)

_PATTERN_TOOL_ABUSE = _InjectionPattern(
    pattern_id="tool_abuse",
    severity=InjectionSeverity.MEDIUM,
    description="工具滥用 — 试图通过文档诱导 AI 调用写操作/危险工具",
    regexes=(
        re.compile(r"(?:call|invoke|execute|run)\s+(?:tool\s+)?(?:document_create|create_it_ticket|delete_)", re.IGNORECASE),
        re.compile(r"(?:调用|执行|使用)(?:工具|函数)?[:：]?\s*(?:document_create|create_it_ticket|delete_)"),
        re.compile(r"execute\s+(?:command|shell|bash)\s*[:：]", re.IGNORECASE),
        re.compile(r"(?:执行|运行)(?:命令|shell|脚本)\s*[:：]"),
        re.compile(r"please\s+(?:create|delete|update)\s+(?:a\s+)?(?:document|ticket|expense)", re.IGNORECASE),
        re.compile(r"(?:请|现在)(?:帮我|直接)?(?:创建|删除|修改|提交)(?:文档|工单|报销单)"),
    ),
)

_PATTERN_JAILBREAK = _InjectionPattern(
    pattern_id="jailbreak",
    severity=InjectionSeverity.HIGH,
    description="越狱模式 — DAN/Developer Mode/解除限制",
    regexes=(
        re.compile(r"\bDAN\b\s*(?:mode|prompt)?", re.IGNORECASE),
        re.compile(r"developer\s+mode", re.IGNORECASE),
        re.compile(r"jailbreak\s+(?:mode|prompt)?", re.IGNORECASE),
        re.compile(r"(?:假装|假定|现在)(?:你是|AI是|你处于)(?:一个)?没有(?:限制|约束)"),
        re.compile(r"(?:现在|从现在起)(?:你)?(?:不受|没有)(?:任何)?(?:限制|约束)"),
        re.compile(r"do\s+anything\s+now", re.IGNORECASE),
        re.compile(r"(?:进入|启用)(?:开发者|开发|越狱)模式"),
    ),
)

_PATTERN_DELIMITER_INJECTION = _InjectionPattern(
    pattern_id="delimiter_injection",
    severity=InjectionSeverity.LOW,
    description="分隔符注入 — 试图破坏 context 边界",
    regexes=(
        # 紧凑形式：```system``` 单行
        re.compile(r"```\s*(?:system|assistant|user)\s*```", re.IGNORECASE),
        # 代码块开头形式：```system\n 后跟任意内容（最常见的伪 system 消息模式）
        re.compile(r"```\s*(?:system|assistant|user)\s*(?:```|\n|$)", re.IGNORECASE | re.MULTILINE),
        re.compile(r"---\s*(?:system|assistant)\s*---", re.IGNORECASE),
        re.compile(r"<<\s*(?:SYSTEM|ASSISTANT)\s*>>", re.IGNORECASE),
        re.compile(r"===+\s*(?:system|assistant)\s*===+", re.IGNORECASE),
    ),
)


# 全部模式（按严重级别排序，HIGH 优先匹配便于短路）
_ALL_PATTERNS: tuple[_InjectionPattern, ...] = (
    _PATTERN_INSTRUCTION_HIJACK,
    _PATTERN_JAILBREAK,
    _PATTERN_DATA_EXFILTRATION,
    _PATTERN_ROLE_OVERRIDE,
    _PATTERN_TOOL_ABUSE,
    _PATTERN_DELIMITER_INJECTION,
)


# 严重级别 → 是否隔离的映射（按配置阈值过滤）
_SEVERITY_RANK: dict[InjectionSeverity, int] = {
    InjectionSeverity.LOW: 1,
    InjectionSeverity.MEDIUM: 2,
    InjectionSeverity.HIGH: 3,
}


def _threshold_severity(value: str) -> InjectionSeverity:
    """从字符串解析阈值级别，无效值回退到 MEDIUM（保守）。"""
    try:
        return InjectionSeverity(value.lower())
    except (ValueError, AttributeError):
        log.warning("injection_guard.invalid_threshold", value=value, fallback="medium")
        return InjectionSeverity.MEDIUM


# ======================================================================
# InjectionGuard 守卫类
# ======================================================================


class InjectionGuard:
    """检索结果注入扫描守卫。

    使用方式::

        guard = InjectionGuard()
        result = guard.scan(candidates)
        safe_docs = result.safe_docs           # 继续走重排/生成
        quarantined = result.quarantined_docs  # 不丢弃，保留审计证据

    严重级别阈值（``RAG_INJECTION_GUARD_QUARANTINE_THRESHOLD``）：
        - "high"   ：仅 HIGH 级别命中隔离（最宽松，适合低风险场景）
        - "medium" ：MEDIUM + HIGH 命中隔离（默认，平衡误报与安全）
        - "low"    ：所有级别命中均隔离（最严格，适合高敏感场景）

    告警回调（``alert_callback``）：
        - 由 engine 层注入实际实现（调用 NotificationService.send_admin_alert）；
        - 守卫本身不依赖 DB / NotificationService，遵循单一职责；
        - 回调失败仅记录日志，不影响主流程。
    """

    def __init__(
        self,
        *,
        alert_callback: AlertCallback | None = None,
        patterns: tuple[_InjectionPattern, ...] | None = None,
    ) -> None:
        self._patterns: tuple[_InjectionPattern, ...] = patterns or _ALL_PATTERNS
        self._alert_callback: AlertCallback | None = alert_callback

        # 配置懒加载（不在 __init__ 触发 settings 重读，便于测试注入）
        self._threshold: InjectionSeverity | None = None

    def _get_threshold(self) -> InjectionSeverity:
        """懒解析隔离阈值，避免构造期触发配置读取。"""
        if self._threshold is None:
            try:
                settings = get_settings()
                raw = getattr(settings, "RAG_INJECTION_GUARD_QUARANTINE_THRESHOLD", "medium")
                self._threshold = _threshold_severity(raw)
            except Exception:
                self._threshold = InjectionSeverity.MEDIUM
        return self._threshold

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def scan(
        self,
        docs: list[dict[str, Any]],
        *,
        query: str | None = None,
    ) -> InjectionScanResult:
        """扫描检索结果，返回安全文档 + 隔离文档 + 命中详情。

        Args:
            docs: HybridRetriever.search 返回的候选文档列表。每项应含
                  ``doc_id`` / ``chunk_id`` / ``content`` 字段。
            query: 可选的原始用户查询（用于避免将用户输入误判为注入）。

        Returns:
            InjectionScanResult — safe_docs 可继续走重排/生成；
            quarantined_docs 保留命中文档（带 ``injection_hits`` 字段）；
            hits 包含所有命中详情。
        """
        if not docs:
            return InjectionScanResult(total_scanned=0)

        threshold = self._get_threshold()
        threshold_rank = _SEVERITY_RANK[threshold]

        safe_docs: list[dict[str, Any]] = []
        quarantined_docs: list[dict[str, Any]] = []
        all_hits: list[InjectionHit] = []

        for doc in docs:
            doc_id = str(doc.get("doc_id") or "")
            chunk_id = str(doc.get("chunk_id") or "")
            content = doc.get("content") or ""
            if not isinstance(content, str):
                content = str(content)

            hits = self._scan_content(content, doc_id, chunk_id)
            all_hits.extend(hits)

            # 决策：命中级别 >= 阈值 → 隔离
            should_quarantine = any(
                _SEVERITY_RANK[h.severity] >= threshold_rank for h in hits
            )

            if should_quarantine:
                # 不丢弃：浅拷贝并附上命中详情，进 quarantined 列表
                # 同时补全 doc_id/chunk_id 字段（缺失时填空字符串），
                # 保证审计字段一致性（避免下游消费者 KeyError）。
                quarantined_doc = dict(doc)
                quarantined_doc.setdefault("doc_id", doc_id)
                quarantined_doc.setdefault("chunk_id", chunk_id)
                quarantined_doc["injection_hits"] = [
                    {
                        "pattern_id": h.pattern_id,
                        "severity": h.severity.value,
                        "matched_text": h.matched_text,
                        "start_offset": h.start_offset,
                        "pattern_desc": h.pattern_desc,
                    }
                    for h in hits
                ]
                quarantined_docs.append(quarantined_doc)
            else:
                # 低级别命中（仅 LOW）仍进入 safe_docs，但记录告警
                safe_docs.append(doc)
                if hits:
                    log.info(
                        "injection_guard.low_severity_passthrough",
                        doc_id=doc_id,
                        chunk_id=chunk_id,
                        hit_count=len(hits),
                        severities=[h.severity.value for h in hits],
                    )

        result = InjectionScanResult(
            safe_docs=safe_docs,
            quarantined_docs=quarantined_docs,
            hits=all_hits,
            total_scanned=len(docs),
        )

        # 命中即告警（含 LOW 级别，便于运营观察注入趋势）
        if all_hits:
            log.warning(
                "injection_guard.detected",
                total_scanned=result.total_scanned,
                total_hits=result.total_hits,
                quarantined=result.total_quarantined,
                threshold=threshold.value,
                patterns_hit=list({h.pattern_id for h in all_hits}),
                query_preview=(query or "")[:100],
                alert=bool(self._alert_callback),
            )
            # 异步触发 admin 告警（由 engine 注入的回调实现）
            if self._alert_callback is not None:
                self._fire_alert(result)

        return result

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _scan_content(
        self,
        content: str,
        doc_id: str,
        chunk_id: str,
    ) -> list[InjectionHit]:
        """扫描单个文档内容，返回所有命中。"""
        if not content:
            return []

        hits: list[InjectionHit] = []
        for pattern in self._patterns:
            for regex in pattern.regexes:
                # finditer 捕获所有匹配，避免一次命中后短路漏报后续
                for match in regex.finditer(content):
                    matched = match.group(0)
                    hits.append(
                        InjectionHit(
                            doc_id=doc_id,
                            chunk_id=chunk_id,
                            pattern_id=pattern.pattern_id,
                            severity=pattern.severity,
                            matched_text=matched[:200],  # 截断防止日志爆炸
                            start_offset=match.start(),
                            pattern_desc=pattern.description,
                        )
                    )
        return hits

    def _fire_alert(self, result: InjectionScanResult) -> None:
        """触发告警回调 — 失败仅记录日志，不影响主流程。

        使用 asyncio.create_task 异步触发，避免阻塞 RAG 主循环；
        回调内部应自行处理异常，此处再加一层防御性捕获。
        """
        import asyncio

        async def _safe_alert() -> None:
            try:
                await self._alert_callback(result)  # type: ignore[arg-type]
            except Exception as exc:
                log.warning(
                    "injection_guard.alert_callback_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_safe_alert())
            # 持有强引用防止任务被 GC 提前回收，完成后自动从集合移除
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except RuntimeError:
            # 无运行中事件循环（测试场景）— 同步降级仅记录日志
            log.debug("injection_guard.no_event_loop_for_alert")


# ======================================================================
# 模块级单例 + 工厂
# ======================================================================


_guard_instance: InjectionGuard | None = None

# 后台任务强引用集合 — 事件循环对 Task 仅持弱引用，不保存强引用任务可能
# 被 GC 提前回收（CPython 官方文档明确警告）；加入本集合并通过
# done_callback 自动移除，兼顾防 GC 与防泄漏。
_background_tasks: set[asyncio.Task] = set()


def get_injection_guard(
    *,
    alert_callback: AlertCallback | None = None,
    force_new: bool = False,
) -> InjectionGuard | None:
    """获取 InjectionGuard 单例。

    - 未启用（``RAG_INJECTION_GUARD_ENABLED=False``）返回 None；
    - alert_callback 仅在首次创建时注入，后续调用忽略（保证单例一致性）；
    - force_new=True 用于测试场景强制重建实例。
    """
    global _guard_instance

    if force_new:
        _guard_instance = None

    if _guard_instance is not None:
        return _guard_instance

    try:
        settings = get_settings()
        if not getattr(settings, "RAG_INJECTION_GUARD_ENABLED", True):
            return None
    except Exception as exc:
        log.warning("injection_guard.config_unavailable", error=str(exc))
        return None

    _guard_instance = InjectionGuard(alert_callback=alert_callback)
    return _guard_instance


def reset_injection_guard_singleton() -> None:
    """重置模块单例 — 仅供测试使用。"""
    global _guard_instance
    _guard_instance = None
