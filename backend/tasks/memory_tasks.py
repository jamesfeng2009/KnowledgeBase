"""记忆异步写入任务 — 单一职责：对话结束后在 Celery 中写记忆（P0 对标竞品异步沉淀）。

背景：
- 原实现：chat_service._persist_assistant_result / agents.base._save_memory 在
  请求路径内 await save_session + extract_and_save_facts（含 LLM 提取），
  拉长 SSE 尾延迟；
- 现实现：回答落库后立即 .delay() 派发本任务，回答先返回；
  broker 不可用时调用方降级回同步写入（fail-open，不丢记忆）。

任务内容（与原同步路径 1:1 对齐）：
  1. save_session          — Checkpoint 快照 + summary 事实（跨会话摘要）
  2. extract_and_save_facts — LLM 事实提取（含溯源 message_ids）
  3. extract_and_save_key_decisions — 关键决策持久化到 working memory（可选）
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.utils.logger import get_logger
from celery_app import celery_app

logger = get_logger(__name__)


async def _persist_memory_async(
    user_id: str,
    session_id: str,
    query: str,
    assistant_content: str = "",
    summary: str = "",
    user_message_id: str | None = None,
    agent_state: dict[str, Any] | None = None,
    extract_decisions: bool = False,
) -> dict[str, Any]:
    """在独立 DB 会话中执行记忆写入（Celery 任务体）。

    与原同步路径 1:1 对齐，任何一段失败仅记日志不阻断其余段落。
    """
    import uuid as _uuid

    from app.database import task_db_session
    from app.memory.memory_manager import MemoryManager

    result: dict[str, Any] = {
        "session_saved": False,
        "facts": [],
        "decisions": [],
    }

    async with task_db_session() as session:
        from contextlib import nullcontext

        from sqlalchemy import select

        from app.core.tenant_context import apply_tenant, system_scope
        from app.models.conversation import Conversation

        # RLS fail-closed 下必须先拿到会话归属才能写记忆。一次 '*' 作用域
        # 查询同时取 tenant_id 与 deleted_at，区分三种情况：
        #   行不存在 / 已软删除 → 跳过（竞态守卫，防"僵尸记忆"）
        #   tenant_id 非空     → 严格租户作用域写入
        #   tenant_id 为 NULL  → 单租户部署的合法会话。fail-closed 策略里
        #     NULL 行只在 '*' 下可见/可写，若按"无身份"处理会被误判成
        #     "会话已删除"而全丢（B3）。写入体放进带审计理由的 '*' 作用域。
        try:
            conv_uuid = _uuid.UUID(session_id)
        except ValueError:
            logger.warning("memory_task.invalid_session_id", session_id=session_id)
            return result

        async with system_scope(
            session, reason="memory_tasks.lookup_conversation_owner"
        ):
            row = (
                await session.execute(
                    select(Conversation.tenant_id, Conversation.deleted_at).where(
                        Conversation.id == conv_uuid
                    )
                )
            ).first()

        if row is None or row.deleted_at is not None:
            logger.info(
                "memory_task.skipped_conversation_gone",
                session_id=session_id,
                deleted=row is not None,
            )
            return result

        if row.tenant_id is not None:
            await apply_tenant(session, row.tenant_id)
            write_scope = nullcontext()
        else:
            write_scope = system_scope(
                session, reason="memory_tasks.single_tenant_null_tenant"
            )

        async with write_scope:
            memory = MemoryManager(session)

            # 1. Checkpoint + summary（与 save_session 行为一致）
            try:
                await memory.save_session(
                    user_id=_uuid.UUID(user_id),
                    session_id=session_id,
                    agent_state=agent_state,
                    summary=summary or None,
                )
                result["session_saved"] = True
            except Exception as exc:
                # 记忆写入失败 = 数据丢失，必须 error 级可见（B1：不得静默）
                logger.error("memory_task.save_session_failed", error=str(exc))

            # 2. 事实提取（含溯源）
            try:
                from uuid import UUID as _UUID

                result["facts"] = await memory.extract_and_save_facts(
                    _UUID(user_id),
                    [{"role": "user", "content": query}],
                    message_ids=([_UUID(user_message_id)] if user_message_id else None),
                )
            except Exception as exc:
                logger.error("memory_task.extract_facts_failed", error=str(exc))

            # 3. 关键决策 → working memory（chat_service 路径专属）
            if extract_decisions:
                try:
                    from uuid import UUID as _UUID

                    await memory.extract_and_save_key_decisions(
                        user_id=_UUID(user_id),
                        query=query,
                        answer=assistant_content,
                    )
                except Exception as exc:
                    logger.error("memory_task.key_decisions_failed", error=str(exc))

            await session.commit()

    return result


@celery_app.task(
    name="tasks.memory_tasks.persist_conversation_memory",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def persist_conversation_memory(
    self: Any,
    user_id: str,
    session_id: str,
    query: str,
    assistant_content: str = "",
    summary: str = "",
    user_message_id: str | None = None,
    agent_state: dict[str, Any] | None = None,
    extract_decisions: bool = False,
) -> dict[str, Any]:
    """对话结束后异步写记忆（Checkpoint + 摘要 + 事实提取 + 关键决策）。

    参数与调用方（chat_service / agents.base）的原同步写入 1:1 对应。
    失败自动重试（最多 2 次，间隔 30s）。
    """
    logger.info(
        "memory_task.started",
        session_id=session_id,
        user_id=user_id,
        extract_decisions=extract_decisions,
    )
    try:
        result = asyncio.run(
            _persist_memory_async(
                user_id=user_id,
                session_id=session_id,
                query=query,
                assistant_content=assistant_content,
                summary=summary,
                user_message_id=user_message_id,
                agent_state=agent_state,
                extract_decisions=extract_decisions,
            )
        )
        logger.info(
            "memory_task.completed",
            session_id=session_id,
            facts=len(result.get("facts", [])),
        )
        return result
    except Exception as exc:
        logger.error("memory_task.failed", session_id=session_id, error=str(exc))
        raise self.retry(exc=exc)


def dispatch_memory_write(**kwargs: Any) -> bool:
    """派发记忆写入任务 — 开关关闭或 broker 不可用时返回 False。

    调用方（chat_service / agents.base）收到 False 后降级为原同步写入
    （fail-open：宁可同步慢一点，也不丢记忆）。

    Returns:
        True = 已派发到 Celery；False = 需调用方同步写入。
    """
    from app.config import get_settings

    if not get_settings().MEMORY_ASYNC_WRITE_ENABLED:
        return False
    try:
        persist_conversation_memory.delay(**kwargs)
        return True
    except Exception as exc:
        logger.warning("memory_task.dispatch_failed_fallback_sync", error=str(exc))
        return False
