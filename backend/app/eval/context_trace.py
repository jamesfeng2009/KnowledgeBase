"""
上下文选择证据记录 — 评测.md §9.1 ContextTraceRecord。

补齐 §2.3 缺失的上下文管理观测能力：不复制全文，只记录选择证据与引用。

数据来源：
    Agent Loop 执行期间，context.load / state.update 等标准 Span 的
    metadata 携带上下文选择证据（included_refs / excluded_refs /
    trust_levels / compaction_event / subagent_summary / token_cost），
    由 Task1 的 SpanRecorder 收集后，经 ``from_spans`` 聚合成本记录。

字段语义（§2.3 缺口表逐项对应）：
    - context_sources：哪些信息源进入了 Context（knowledge_base / web / tool …）
    - context_included_refs：真正进入模型的文件/片段引用
    - context_excluded_refs：被排除的大文件/大日志引用
    - trust_levels：每个源的信任等级（user / internal / external / tool_output）
    - compaction_events：压缩触发原因、保留摘要、丢弃内容类型
    - subagent_summaries：子 Agent 摘要及证据引用
    - token_cost：压缩前后 token 对比（before / after）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _span_get(span: Any, key: str, default: Any = None) -> Any:
    """兼容 SpanRecord 对象与 dict 两种形态的字段读取。"""
    if isinstance(span, dict):
        return span.get(key, default)
    return getattr(span, key, default)


@dataclass
class ContextTraceRecord:
    """上下文选择证据记录（一次 task run 级别）。"""

    context_sources: list[str] = field(default_factory=list)
    context_included_refs: list[str] = field(default_factory=list)
    context_excluded_refs: list[str] = field(default_factory=list)
    trust_levels: dict[str, str] = field(default_factory=dict)
    compaction_events: list[dict[str, Any]] = field(default_factory=list)
    subagent_summaries: list[dict[str, Any]] = field(default_factory=list)
    token_cost: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_sources": self.context_sources,
            "context_included_refs": self.context_included_refs,
            "context_excluded_refs": self.context_excluded_refs,
            "trust_levels": self.trust_levels,
            "compaction_events": self.compaction_events,
            "subagent_summaries": self.subagent_summaries,
            "token_cost": self.token_cost,
        }

    @classmethod
    def from_spans(cls, spans: list[Any]) -> ContextTraceRecord:
        """从标准 Span 记录聚合上下文选择证据。

        聚合规则：
            - 所有 span 的 metadata 中出现以下键即被采集：
              source / included_refs / excluded_refs / trust_levels /
              compaction_event / subagent_summary / token_cost；
            - 列表型键做去重合并（保持首次出现顺序）；
            - trust_levels / token_cost 做 dict 合并（后者同键累加）；
            - compaction_event / subagent_summary 每条追加为一个事件。

        Args:
            spans: SpanRecord 列表或其 to_dict() 字典列表。

        Returns:
            聚合后的 ContextTraceRecord（无证据时各字段为空）。
        """
        record = cls()

        def _merge_list(target: list[str], values: Any) -> None:
            for v in values or []:
                s = str(v)
                if s not in target:
                    target.append(s)

        for span in spans:
            metadata = _span_get(span, "metadata", None) or {}
            if not isinstance(metadata, dict):
                continue

            # 信息源：metadata.source 单值或 metadata.context_sources 列表
            source = metadata.get("source")
            if source:
                _merge_list(record.context_sources, [source])
            _merge_list(record.context_sources, metadata.get("context_sources"))

            _merge_list(
                record.context_included_refs, metadata.get("included_refs")
            )
            _merge_list(
                record.context_excluded_refs, metadata.get("excluded_refs")
            )

            trust = metadata.get("trust_levels")
            if isinstance(trust, dict):
                for k, v in trust.items():
                    record.trust_levels[str(k)] = str(v)

            compaction = metadata.get("compaction_event")
            if isinstance(compaction, dict):
                record.compaction_events.append(dict(compaction))

            subagent = metadata.get("subagent_summary")
            if isinstance(subagent, dict):
                record.subagent_summaries.append(dict(subagent))

            token_cost = metadata.get("token_cost")
            if isinstance(token_cost, dict):
                for k, v in token_cost.items():
                    try:
                        record.token_cost[str(k)] = (
                            record.token_cost.get(str(k), 0) + int(v)
                        )
                    except (TypeError, ValueError):
                        continue

        return record

    @property
    def has_evidence(self) -> bool:
        """是否采集到任何上下文证据（无证据时不做上下文评分）。"""
        return bool(
            self.context_sources
            or self.context_included_refs
            or self.context_excluded_refs
            or self.trust_levels
            or self.compaction_events
            or self.subagent_summaries
            or self.token_cost
        )
