"""
LangGraph Checkpoint 会话状态管理 — 单一职责：持久化 Agent Loop 状态。

定位：多轮对话中断恢复、Agent Loop 状态持久化。
特点：基于 PostgreSQL 的 Checkpoint，支持会话恢复。

遵循单一职责：只管状态存取，不管业务逻辑。
"""

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.logger import get_logger

logger = get_logger(__name__)


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
        import json
        state_json = json.dumps(agent_state, default=str, ensure_ascii=False)

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

        import json
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
