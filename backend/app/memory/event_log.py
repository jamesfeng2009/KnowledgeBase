"""
Agent Loop 事件日志管理器 — 单一职责：与 Checkpoint 互补的混合恢复机制。

定位：LangGraph Checkpoint 保存状态快照，EventLog 保存事件流，二者组合实现
任意时间点状态恢复。

设计理念（混合恢复）：

    时间轴 →→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→
    事件：  [e1]  [e2]  [e3]  [CP]  [e4]  [e5]  [e6]  [e7]
                            ↑                ↑
                       base_checkpoint    current

    恢复 = 加载 base_checkpoint 状态 + 重放 e4..e7 = current 状态

    若 base_checkpoint 损坏，可从更早的 e1..e3 + 更早 Checkpoint 重建。

事件类型：
    - node_end: Agent Loop 节点执行结束，含 output（state 增量）
    - node_start: 节点开始（可选，主要用于延迟统计）
    - state_update: 显式状态更新（如 budget 触发压缩）

重放语义（与 LangGraph Annotated[list, operator.add] reducer 对齐）：
    - output_data 中的 list 字段（messages / retrieved_docs / tool_results）：
      重放时 extend 到 base_state 对应字段
    - 其他字段（标量 / dict）：覆盖 base_state 对应字段
    - 未在 output_data 中出现的字段：保留 base_state 原值

接入点：
    - langfuse_tracer.py 的 trace_node 装饰器 finally 块 → append(node_end)
    - CheckpointManager.save_checkpoint_with_event_log → 同步记录 base_seq

遵循单一职责：本模块只负责事件存取与重放，不修改 Agent Loop 逻辑。
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Generator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: 当前激活的 EventLogManager（contextvar，跨 async 调用链传递）
#: 与 span_record._recorder_var 同款模式 — 评测/请求入口注入，节点内拾取
_event_log_var: ContextVar[EventLogManager | None] = ContextVar(
    "event_log_manager", default=None
)

#: list 类型字段的合并策略 — extend（与 LangGraph Annotated[list, operator.add] 对齐）
#: 重放时这些字段的 output_data 会被 extend 到 base_state 对应字段
_LIST_FIELDS: frozenset[str] = frozenset({
    "messages",
    "retrieved_docs",
    "tool_results",
    "quarantined_docs",
    "milestones",
})


@dataclass
class EventRecord:
    """事件记录 — 一次 Agent Loop 节点执行的增量证据。

    Attributes:
        seq: 会话内自增序号（从 1 开始）。
        event_type: 事件类型（node_start / node_end / state_update）。
        node_name: 节点名（think / retrieve / tool_call / generate / reflect）。
        iteration: Agent Loop 迭代轮次。
        input_data: 节点输入摘要（已 PII 脱敏，可选）。
        output_data: 节点输出 state 增量（重放时按 reducer 语义合并）。
        metadata: 额外元数据（latency_ms / token_count / error）。
        created_at: 事件创建时间戳（ISO 格式字符串）。
    """

    seq: int
    event_type: str
    node_name: str
    iteration: int
    input_data: dict[str, Any] | None = None
    output_data: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


class EventLogManager:
    """Agent Loop 事件日志管理器 — 与 Checkpoint 互补的混合恢复。

    使用方式::

        manager = EventLogManager(db_session)
        # 节点执行后追加事件
        seq = await manager.append(
            session_id="abc",
            event_type="node_end",
            node_name="think",
            output_data={"messages": [...]},
            iteration=1,
        )
        # 混合恢复
        base_state = await checkpoint.load_checkpoint(session_id)
        final_state = await manager.replay(session_id, base_state, after_seq=base_seq)

    所有方法均容忍 PII — 调用方应先 scrub input_data / output_data（trace_node
    装饰器已在 end_span 阶段完成 scrub，append 时直接接收 scrubbed 数据）。
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def append(
        self,
        session_id: str,
        event_type: str,
        node_name: str,
        output_data: dict[str, Any] | None = None,
        iteration: int = 0,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """追加一条事件日志，返回会话内 seq。

        seq 通过 ``COALESCE(MAX(seq), 0) + 1`` 在 INSERT 时计算，
        避免独立的 SELECT-then-INSERT 竞态（同一事务内仍可能有并发 INSERT，
        但 PG 的 READ COMMITTED + UNIQUE 约束会拒绝重复 seq，调用方
        收到 IntegrityError 时可重试一次）。

        Args:
            session_id: 会话 ID。
            event_type: 事件类型（node_start / node_end / state_update）。
            node_name: 节点名。
            output_data: 节点输出 state 增量（重放用）。
            iteration: Agent Loop 迭代轮次。
            input_data: 节点输入摘要（可选，已脱敏）。
            metadata: 额外元数据（latency_ms / token_count / error）。

        Returns:
            新事件的 seq；写入失败时返回 0（不抛异常，仅记日志 — 事件日志
            不应阻塞主流程，Checkpoint 仍是兜底恢复机制）。
        """
        input_json = json.dumps(input_data, default=str, ensure_ascii=False) if input_data else None
        output_json = json.dumps(output_data, default=str, ensure_ascii=False) if output_data else None
        meta_json = json.dumps(metadata or {}, default=str, ensure_ascii=False)

        try:
            # 注意：:session_id 在 VALUES 和 WHERE 中复用，asyncpg 会合并为 $1，
            # 但列是 VARCHAR(64) 而 text() 默认推断为 text，导致
            # "inconsistent types deduced for parameter" 错误。
            # 解决：WHERE 子句用 CAST 显式指定类型，避免类型推断冲突。
            result = await self.db.execute(
                text("""
                    INSERT INTO agent_event_logs
                        (session_id, seq, event_type, node_name, iteration,
                         input_data, output_data, metadata)
                    VALUES (
                        :session_id,
                        COALESCE(
                            (SELECT MAX(seq) FROM agent_event_logs
                             WHERE session_id = CAST(:session_id AS VARCHAR)),
                            0
                        ) + 1,
                        :event_type, :node_name, :iteration,
                        CAST(:input_data AS jsonb),
                        CAST(:output_data AS jsonb),
                        CAST(:metadata AS jsonb)
                    )
                    RETURNING seq
                """),
                {
                    "session_id": session_id,
                    "event_type": event_type,
                    "node_name": node_name,
                    "iteration": iteration,
                    "input_data": input_json,
                    "output_data": output_json,
                    "metadata": meta_json,
                },
            )
            await self.db.flush()
            row = result.first()
            seq = int(row[0]) if row else 0
            logger.info(
                "event_log.appended",
                session_id=session_id,
                seq=seq,
                node_name=node_name,
                event_type=event_type,
            )
            return seq
        except Exception as exc:
            # 事件日志失败不阻塞主流程 — Checkpoint 是兜底
            logger.warning(
                "event_log.append_error",
                session_id=session_id,
                node_name=node_name,
                error=str(exc),
            )
            return 0

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def list_after(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[EventRecord]:
        """列出指定 seq 之后的事件（按 seq 升序）。

        Args:
            session_id: 会话 ID。
            after_seq: 起始 seq（不含），0 表示从最早事件开始。
            limit: 返回数量上限。

        Returns:
            事件记录列表（按 seq 升序）。
        """
        result = await self.db.execute(
            text("""
                SELECT seq, event_type, node_name, iteration,
                       input_data, output_data, metadata, created_at
                FROM agent_event_logs
                WHERE session_id = :session_id AND seq > :after_seq
                ORDER BY seq ASC
                LIMIT :limit
            """),
            {
                "session_id": session_id,
                "after_seq": after_seq,
                "limit": limit,
            },
        )
        records: list[EventRecord] = []
        for row in result:
            records.append(self._row_to_record(row))
        return records

    async def get_last_seq(self, session_id: str) -> int:
        """获取会话的最新 seq（无事件时返回 0）。

        用于 Checkpoint 保存时记录 base_seq，恢复时从 base_seq 之后重放。
        """
        result = await self.db.execute(
            text(
                "SELECT MAX(seq) FROM agent_event_logs WHERE session_id = :session_id"
            ),
            {"session_id": session_id},
        )
        row = result.first()
        if not row or row[0] is None:
            return 0
        return int(row[0])

    async def get_event_count(self, session_id: str) -> int:
        """获取会话的事件总数（用于监控/清理决策）。"""
        result = await self.db.execute(
            text(
                "SELECT COUNT(*) FROM agent_event_logs WHERE session_id = :session_id"
            ),
            {"session_id": session_id},
        )
        row = result.first()
        return int(row[0]) if row and row[0] else 0

    # ------------------------------------------------------------------
    # 重放 — 混合恢复核心
    # ------------------------------------------------------------------

    async def replay(
        self,
        session_id: str,
        base_state: dict[str, Any],
        after_seq: int = 0,
    ) -> dict[str, Any]:
        """从 base_state 开始重放事件流，返回最终状态。

        重放语义（与 LangGraph Annotated[list, operator.add] reducer 对齐）：
        - output_data 中的 list 字段（messages / retrieved_docs / tool_results）：
          extend 到 base_state 对应字段
        - 其他字段：覆盖 base_state 对应字段
        - 未在 output_data 中出现的字段：保留 base_state 原值

        Args:
            session_id: 会话 ID。
            base_state: 基线状态（来自 Checkpoint）。
            after_seq: 从此 seq 之后开始重放（应等于 Checkpoint 保存时的 base_seq）。

        Returns:
            重放后的最终状态（深拷贝，不修改 base_state 原对象）。
        """
        events = await self.list_after(session_id, after_seq=after_seq)
        # 深拷贝避免修改调用方传入的 base_state
        state = copy.deepcopy(base_state) if base_state else {}

        applied_count = 0
        for event in events:
            output = event.output_data
            if not output or not isinstance(output, dict):
                continue
            for key, value in output.items():
                if key in _LIST_FIELDS:
                    # list 字段：extend（与 LangGraph Annotated[list, operator.add] 对齐）
                    if isinstance(value, list):
                        existing = state.get(key)
                        if isinstance(existing, list):
                            existing.extend(value)
                        else:
                            state[key] = list(value)
                    # 非 list 值落到 list 字段：覆盖（异常情况，记录日志）
                    else:
                        state[key] = value
                else:
                    # 标量 / dict 字段：覆盖
                    state[key] = value
            applied_count += 1

        logger.info(
            "event_log.replayed",
            session_id=session_id,
            after_seq=after_seq,
            total_events=len(events),
            applied_events=applied_count,
        )
        return state

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    async def truncate(
        self,
        session_id: str,
        keep_last_n: int = 1000,
    ) -> int:
        """保留最近 N 条事件，删除更早的。

        Args:
            session_id: 会话 ID。
            keep_last_n: 保留的最近事件数。

        Returns:
            删除的事件数。
        """
        result = await self.db.execute(
            text("""
                DELETE FROM agent_event_logs
                WHERE session_id = :session_id
                  AND seq <= (
                    SELECT MAX(seq) - :keep FROM agent_event_logs
                    WHERE session_id = :session_id
                  )
            """),
            {"session_id": session_id, "keep": keep_last_n},
        )
        await self.db.flush()
        deleted = result.rowcount or 0
        logger.info(
            "event_log.truncated",
            session_id=session_id,
            deleted=deleted,
            kept=keep_last_n,
        )
        return deleted

    async def delete_all(self, session_id: str) -> int:
        """删除会话的全部事件（会话结束时调用）。"""
        result = await self.db.execute(
            text("DELETE FROM agent_event_logs WHERE session_id = :session_id"),
            {"session_id": session_id},
        )
        await self.db.flush()
        deleted = result.rowcount or 0
        logger.info(
            "event_log.deleted_all",
            session_id=session_id,
            deleted=deleted,
        )
        return deleted

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: Any) -> EventRecord:
        """将数据库行转为 EventRecord。"""
        # JSONB 字段可能是 dict 或 str（取决于驱动）
        input_data = row[4] if isinstance(row[4], dict) else (
            json.loads(row[4]) if row[4] else None
        )
        output_data = row[5] if isinstance(row[5], dict) else (
            json.loads(row[5]) if row[5] else None
        )
        metadata = row[6] if isinstance(row[6], dict) else (
            json.loads(row[6]) if row[6] else {}
        )
        created_at = row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7])

        return EventRecord(
            seq=int(row[0]),
            event_type=str(row[1]),
            node_name=str(row[2]),
            iteration=int(row[3] or 0),
            input_data=input_data,
            output_data=output_data,
            metadata=metadata or {},
            created_at=created_at,
        )


# ======================================================================
# contextvar 集成 — 请求级 EventLogManager 注入
# ======================================================================


def get_current_event_log() -> EventLogManager | None:
    """获取当前激活的 EventLogManager（未注入时返回 None）。

    trace_node 装饰器通过此函数拾取请求级 manager，避免 engine 单例
    在并发请求间共享 manager 状态。
    """
    return _event_log_var.get()


class event_log_scope:
    """激活一个 EventLogManager（contextvar 作用域）。

    使用方式::

        with event_log_scope(EventLogManager(db)) as manager:
            ...执行 Agent Loop...
            # trace_node 装饰器内通过 get_current_event_log() 拾取 manager

    与 span_record.span_recorder 同款模式 — engine.answer() 入口注入，
    节点装饰器内 contextvar 拾取，实现 engine 零改动。

    以显式类实现替代 ``@contextmanager`` 装饰器，规避 contextlib 装饰器
    在静态检查/后续 Python 版本中的潜在变更。
    """

    def __init__(self, manager: EventLogManager | None) -> None:
        self.manager = manager
        self._token: Token[EventLogManager | None] | None = None

    def __enter__(self) -> EventLogManager | None:
        self._token = _event_log_var.set(self.manager)
        return self.manager

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        if self._token is not None:
            _event_log_var.reset(self._token)
            self._token = None
