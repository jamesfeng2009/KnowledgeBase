"""
LangGraph Checkpoint 会话状态管理 — 单一职责：持久化 Agent Loop 状态。

定位：多轮对话中断恢复、Agent Loop 状态持久化。
特点：基于 PostgreSQL 的 Checkpoint，支持会话恢复。

P2-13: 里程碑字段 — 长任务按阶段存检查点（milestones 列表存于 agent_state
JSONB 内），失败重试时从最近已完成里程碑恢复，跳过已做阶段。

遵循单一职责：只管状态存取，不管业务逻辑。
"""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import ModuleType
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: P2-13: 里程碑列表在 agent_state 中的字段名
MILESTONES_FIELD = "milestones"

# ---------------------------------------------------------------------------
# Checkpoint 状态序列化防腐蚀 — save_checkpoint 入口统一过滤
#
# 背景：agent_state 若含 callable（如请求级 permission_filter）或连接句柄，
# json.dumps(default=str) 会将其静默序列化成 "<function ...>" 垃圾字符串，
# 恢复时以字符串形态腐化状态。此处递归清洗：
#   - JSON 标量（str/int/float/bool/None）原样保留；
#   - datetime / UUID / Decimal 确定性地字符串化（与原 default=str 一致）；
#   - dict / list / tuple / set 递归处理（dict 键统一转 str）；
#   - callable / module 直接剔除（记录 dropped 路径并告警）；
#   - 其余未知对象保底 str()（兼容 default=str 历史行为，记录告警）。
# ---------------------------------------------------------------------------

_JSON_SCALARS = (str, int, float, bool)
_STR_TYPES = (datetime, uuid.UUID, Decimal)


class _Drop:
    """私有哨兵 — 标记被剔除的值（与合法的 None 值区分）。"""


_DROP = _Drop()


def _sanitize_state_for_json(
    value: Any,
    path: str = "state",
    dropped: list[str] | None = None,
) -> Any:
    """递归清洗 agent_state，产出可安全 CAST 为 jsonb 的值。

    Args:
        value: 待清洗的值（任意嵌套结构）。
        path: 当前值在 state 中的路径（用于告警定位）。
        dropped: 收集被剔除/降级字段路径的列表（可为 None 表示不收集）。

    Returns:
        清洗后的值；被剔除时返回 ``_DROP`` 哨兵（由父层从容器中移除，
        根层传入的 agent_state 恒为 dict，不会以哨兵形态外泄）。
    """
    if value is None or isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, _STR_TYPES):
        return str(value)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            sub = _sanitize_state_for_json(v, f"{path}.{k}", dropped)
            if sub is not _DROP:
                cleaned[k if isinstance(k, str) else str(k)] = sub
        return cleaned
    if isinstance(value, (list, tuple, set)):
        cleaned_list: list[Any] = []
        for i, item in enumerate(value):
            sub = _sanitize_state_for_json(item, f"{path}[{i}]", dropped)
            if sub is not _DROP:
                cleaned_list.append(sub)
        return cleaned_list
    # callable / module — 剔除（反序列化后只是垃圾字符串，还会占通道）
    if callable(value) or isinstance(value, ModuleType):
        if dropped is not None:
            dropped.append(f"{path}({type(value).__name__})")
        return _DROP
    # 其余未知对象 — 保底字符串化（兼容原 default=str 行为），记录告警
    if dropped is not None:
        dropped.append(f"{path}~str({type(value).__name__})")
    return str(value)


def append_milestone_to_state(
    state: dict,
    name: str,
    detail: dict | None = None,
) -> list[dict]:
    """向 agent_state 追加一条里程碑记录（纯函数，原地修改并返回列表）。

    里程碑结构::

        {"seq": 1, "name": "parse", "detail": {...}, "timestamp": "..."}

    Args:
        state: Agent 状态字典（被原地修改）
        name: 里程碑名称（如阶段名 parse/index/graph）
        detail: 附加明细（如 {"status": "done", "duration_ms": 123}）

    Returns:
        更新后的里程碑列表
    """
    milestones = state.setdefault(MILESTONES_FIELD, [])
    entry = {
        "seq": len(milestones) + 1,
        "name": name,
        "detail": detail or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    milestones.append(entry)
    return milestones


class CheckpointManager:
    """LangGraph Checkpoint 管理器 — 会话状态持久化。

    使用 PostgreSQL 存储 Agent Loop 的中间状态，
    支持多轮对话中断恢复和 Agent 迭代状态追踪。
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def save_checkpoint(
        self,
        session_id: str,
        agent_state: dict,
        iteration: int = 0,
    ) -> None:
        """保存 Agent Loop 的当前状态。

        Args:
            session_id: 会话 ID（对应 Conversation ID）
            agent_state: Agent 的完整状态（messages, retrieved_docs, tool_results 等）
            iteration: 当前迭代次数
        """
        # 防腐蚀：递归剔除 callable / module，字符串化简单类型，
        # 防止 default=str 把 permission_filter 等 callable 静默序列化成
        # 垃圾字符串腐化 checkpoint（详见 _sanitize_state_for_json 注释块）。
        dropped: list[str] = []
        safe_state = _sanitize_state_for_json(agent_state, "agent_state", dropped)
        if dropped:
            logger.warning(
                "checkpoint_state_fields_dropped",
                session_id=session_id,
                dropped=dropped[:10],
                dropped_total=len(dropped),
            )

        state_json = json.dumps(safe_state, ensure_ascii=False)

        # 使用 upsert：存在则更新，不存在则插入
        await self.db.execute(
            text("""
                INSERT INTO agent_checkpoints (session_id, agent_state, iteration, updated_at)
                VALUES (:session_id, CAST(:state AS jsonb), :iteration, NOW())
                ON CONFLICT (session_id)
                DO UPDATE SET
                    agent_state = CAST(:state AS jsonb),
                    iteration = :iteration,
                    updated_at = NOW()
            """),
            {
                "session_id": session_id,
                "state": state_json,
                "iteration": iteration,
            },
        )
        await self.db.flush()
        logger.info(
            "checkpoint_saved",
            session_id=session_id,
            iteration=iteration,
        )

    async def load_checkpoint(self, session_id: str) -> dict | None:
        """加载 Agent Loop 的历史状态。

        Returns:
            agent_state 字典，或 None（无历史记录）
        """
        result = await self.db.execute(
            text("""
                SELECT agent_state, iteration
                FROM agent_checkpoints
                WHERE session_id = :session_id
            """),
            {"session_id": session_id},
        )
        row = result.first()
        if row is None:
            return None

        state = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        logger.info(
            "checkpoint_loaded",
            session_id=session_id,
            iteration=row[1],
        )
        return state

    async def delete_checkpoint(self, session_id: str) -> None:
        """删除会话状态（会话结束时调用）。"""
        await self.db.execute(
            text("DELETE FROM agent_checkpoints WHERE session_id = :session_id"),
            {"session_id": session_id},
        )
        await self.db.flush()
        logger.info("checkpoint_deleted", session_id=session_id)

    # ------------------------------------------------------------------
    # P2-7: 混合恢复 — Checkpoint + EventLog 对齐
    # ------------------------------------------------------------------

    async def save_checkpoint_with_event_log(
        self,
        session_id: str,
        agent_state: dict,
        iteration: int = 0,
        base_seq: int | None = None,
    ) -> None:
        """保存 Checkpoint 并记录对应的 EventLog base_seq。

        混合恢复的核心契约：Checkpoint 保存状态快照，同时记录此快照对应的
        EventLog seq。恢复时加载 Checkpoint 后从 base_seq 之后重放事件，
        得到最新状态。

        ``base_seq`` 为 None 时自动取当前 EventLog 的最新 seq（即 Checkpoint
        包含到该 seq 为止的所有事件效果）。显式传入 base_seq 用于"先存事件
        后存 Checkpoint"的场景（如节点执行后立即存 Checkpoint，base_seq
        应为该节点事件的 seq）。

        Args:
            session_id: 会话 ID。
            agent_state: Agent 完整状态。
            iteration: 当前迭代次数。
            base_seq: 对应的 EventLog seq（None 时自动取最新）。
        """
        # 取 base_seq（自动模式：读取 EventLog 最新 seq）
        if base_seq is None:
            try:
                from app.memory.event_log import EventLogManager

                event_log = EventLogManager(self.db)
                base_seq = await event_log.get_last_seq(session_id)
            except Exception as exc:
                logger.warning(
                    "checkpoint.event_log_base_seq_error",
                    session_id=session_id,
                    error=str(exc),
                )
                base_seq = 0

        # 在 agent_state 中嵌入 _base_seq 元数据（恢复时读取）
        # 不污染业务字段，用下划线前缀标记内部元数据
        state_with_meta = dict(agent_state)
        state_with_meta["_base_seq"] = base_seq

        await self.save_checkpoint(session_id, state_with_meta, iteration)
        logger.info(
            "checkpoint_saved_with_event_log",
            session_id=session_id,
            iteration=iteration,
            base_seq=base_seq,
        )

    async def load_checkpoint_with_event_log(
        self,
        session_id: str,
        replay_events: bool = True,
    ) -> dict | None:
        """加载 Checkpoint 并可选重放后续 EventLog 事件，得到最新状态。

        混合恢复入口：先加载 Checkpoint（含 _base_seq 元数据），若
        ``replay_events=True`` 则从 _base_seq 之后重放事件流，得到
        Checkpoint 之后的所有增量变化。

        Args:
            session_id: 会话 ID。
            replay_events: 是否重放 _base_seq 之后的事件（默认 True）。
                False 时仅返回 Checkpoint 快照状态（含 _base_seq）。

        Returns:
            重放后的最新状态；无 Checkpoint 时返回 None。
        """
        state = await self.load_checkpoint(session_id)
        if state is None:
            return None

        if not replay_events:
            return state

        base_seq = state.pop("_base_seq", 0)
        if not isinstance(base_seq, int) or base_seq < 0:
            base_seq = 0

        # 无 EventLog 可重放时直接返回 Checkpoint 状态
        try:
            from app.memory.event_log import EventLogManager

            event_log = EventLogManager(self.db)
            last_seq = await event_log.get_last_seq(session_id)
            if last_seq <= base_seq:
                # 无新增事件，直接返回 Checkpoint 状态
                return state
            return await event_log.replay(session_id, state, after_seq=base_seq)
        except Exception as exc:
            logger.warning(
                "checkpoint.event_log_replay_error",
                session_id=session_id,
                base_seq=base_seq,
                error=str(exc),
            )
            return state

    # ------------------------------------------------------------------
    # P2-13: 长任务里程碑 checkpoint
    # ------------------------------------------------------------------

    async def save_milestone(
        self,
        session_id: str,
        name: str,
        detail: dict | None = None,
        state_extra: dict | None = None,
    ) -> None:
        """记录一个里程碑 — 加载现有状态 → 追加里程碑 → 保存。

        Args:
            session_id: 会话/任务 ID（Celery 任务建议用 "task:{task_id}" 前缀）
            name: 里程碑名称（阶段名）
            detail: 里程碑明细（status/duration_ms 等）
            state_extra: 需要一并合并进 agent_state 的顶层字段（可选）
        """
        state = await self.load_checkpoint(session_id) or {}
        append_milestone_to_state(state, name, detail)
        if state_extra:
            state.update(state_extra)
        await self.save_checkpoint(
            session_id, state, iteration=int(state.get("iteration", 0))
        )
        logger.info(
            "milestone_saved",
            session_id=session_id,
            milestone=name,
        )

    async def get_milestones(self, session_id: str) -> list[dict]:
        """获取全部里程碑列表（按追加顺序）。"""
        state = await self.load_checkpoint(session_id)
        if not state:
            return []
        milestones = state.get(MILESTONES_FIELD, [])
        return list(milestones) if isinstance(milestones, list) else []

    async def get_completed_milestone_names(self, session_id: str) -> set[str]:
        """获取状态为 done 的里程碑名集合 — 断点恢复时跳过这些阶段。"""
        return {
            m.get("name", "")
            for m in await self.get_milestones(session_id)
            if m.get("detail", {}).get("status") == "done"
        }

    async def get_latest_milestone(self, session_id: str) -> dict | None:
        """获取最近一条里程碑（无则 None）。"""
        milestones = await self.get_milestones(session_id)
        return milestones[-1] if milestones else None

    async def list_active_sessions(
        self, user_id: uuid.UUID, limit: int = 20
    ) -> list[dict]:
        """列出用户的活动会话（有 Checkpoint 的）。"""
        result = await self.db.execute(
            text("""
                SELECT ac.session_id, ac.iteration, ac.updated_at,
                       c.title, c.agent_type
                FROM agent_checkpoints ac
                JOIN conversations c ON CAST(c.id AS TEXT) = ac.session_id
                WHERE c.user_id = CAST(:user_id AS UUID)
                  AND c.deleted_at IS NULL
                ORDER BY ac.updated_at DESC
                LIMIT :limit
            """),
            {"user_id": str(user_id), "limit": limit},
        )
        return [
            {
                "session_id": row[0],
                "iteration": row[1],
                "updated_at": row[2],
                "title": row[3],
                "agent_type": row[4],
            }
            for row in result
        ]
