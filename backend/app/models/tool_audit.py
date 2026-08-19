"""
工具调用审计日志 — 持久化关键 tool.call Span，供安全审计与评测回溯。

设计要点：
    - 参考 EvalResultRecord 的 ORM 模式（UUIDMixin + TimestampMixin + Base）；
    - 只持久化关键 span（tool.call / permission.decision / failure.recover），
      避免全量 span 写入膨胀；
    - result_summary 截断存储（500 字符），完整内容通过 result_ref 引用
      （对象存储），关键审计事件另以 result_full JSONB 兜底原文；
    - evidence_ref：P3 贯穿的工具/文档证据引用；
    - 数据库不可用时优雅降级（仅记日志，不抛异常）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 需要持久化的关键 span 类型
_AUDITED_SPAN_TYPES: frozenset[str] = frozenset(
    {"tool.call", "permission.decision", "failure.recover"}
)

#: 结果摘要最大长度
_SUMMARY_MAX_CHARS: int = 500

#: 关键审计事件 — 低频率高价值，额外以 result_full JSONB 存原文吊底，
#: 保证"证据与行永不分离"、可逐年离线回溯（P2 对象存储为主 + JSONB 兜底）。
_KEY_EVENT_SPAN_TYPES: frozenset[str] = frozenset(
    {"permission.decision", "failure.recover"}
)


class ToolAuditLog(UUIDMixin, TimestampMixin, Base):
    """工具调用审计日志表。

    字段说明：
    - run_id / session_id：关联一次 task run / 会话；
    - tool_name / arguments：工具名与调用参数（JSONB）；
    - result_summary：返回摘要（截断 500 字符）；
    - status：success / error / timeout / blocked；
    - duration_ms：工具耗时；
    - span_id / trace_id：关联标准 Span 记录。
    """

    __tablename__ = "tool_audit_log"

    run_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, comment="task run ID"
    )
    session_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, comment="会话 ID"
    )
    span_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="关联的标准 Span ID"
    )
    tool_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="工具名"
    )
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, comment="调用参数"
    )
    result_summary: Mapped[str] = mapped_column(
        Text, default="", comment="返回摘要（截断 500 字符）"
    )
    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="错误信息"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, default=0, comment="耗时（毫秒）"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="success", comment="success/error/timeout/blocked"
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
    # P2: 完整结果引用 — 对象存储引用为主，关键事件 JSONB 兜底
    result_ref: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="完整结果的对象存储引用（artifact ID/路径/URL）",
    )
    result_full: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True,
        comment="关键事件原文兜底（permission.decision/failure.recover）",
    )
    evidence_ref: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="证据引用（P3 贯穿，doc_id/artifact ID）",
    )


def _audit_arguments(meta: dict[str, Any]) -> dict[str, Any]:
    """从 span metadata 提取 arguments — 排除落专列的大字段，避免冗余存储。"""
    return {k: v for k, v in meta.items() if k not in ("result_ref", "result_full")}


async def persist_tool_spans(
    spans: list[Any],
    session: Any,
    run_id: str,
    session_id: str,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """将关键 Span 持久化到 tool_audit_log（best-effort）。

    仅持久化 _AUDITED_SPAN_TYPES 中的 span 类型；任一 span 写入失败
    不影响其余 span，整体异常向上抛出由调用方降级处理。

    Args:
        spans: SpanRecord 列表（或具有同名属性的对象）。
        session: 数据库会话（由调用方管理事务）。
        run_id: 本次 task run ID。
        session_id: 会话 ID。

    Returns:
        实际写入的条数。
    """
    written = 0
    for span in spans:
        span_type = str(getattr(span, "span_type", ""))
        if span_type not in _AUDITED_SPAN_TYPES:
            continue
        try:
            raw_status = str(getattr(span, "status", "ok"))
            meta = getattr(span, "metadata", {}) or {}
            # P2 三级降级：关键事件 → result_full 原文；普通 → result_ref
            # 对象存储引用；皆无 → 仅截断摘要 result_summary。
            is_key_event = span_type in _KEY_EVENT_SPAN_TYPES
            result_full = meta.get("result_full") if is_key_event else None
            result_ref = meta.get("result_ref") if not is_key_event else None
            record = ToolAuditLog(
                run_id=run_id,
                session_id=session_id,
                span_id=str(getattr(span, "span_id", uuid.uuid4().hex[:16])),
                tool_name=str(getattr(span, "name", "")),
                arguments=_audit_arguments(meta),
                result_summary=str(getattr(span, "output_ref", "") or "")[
                    :_SUMMARY_MAX_CHARS
                ],
                result_ref=result_ref,
                result_full=result_full,
                evidence_ref=str(getattr(span, "evidence_ref", None) or "")[:500]
                or None,
                error=getattr(span, "error", None),
                duration_ms=int(
                    getattr(span, "latency_ms", None)
                    or (getattr(span, "cost", {}) or {}).get("latency_ms", 0)
                    or 0
                ),
                status="success" if raw_status == "ok" else raw_status,
                tenant_id=tenant_id,
            )
            session.add(record)
            written += 1
        except Exception as exc:
            log.warning("tool_audit.span_write_error", error=str(exc))
    return written
