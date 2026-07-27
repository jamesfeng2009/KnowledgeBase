"""
AI 对话服务 — 单一职责：编排对话会话与 Agentic RAG 引擎流式生成。

P0-4 重构：从直接调用 LLMProvider 升级为通过 AgenticRAGEngine.answer() 统一走
Agent Loop（think → retrieve/tool_call → generate → reflect），所有 agent_type
共用同一引擎路径，agent_type 仅影响系统提示词和工具集。

遵循单一职责：ChatService 只负责对话流程编排（会话管理 → 消息持久化 → 记忆加载 →
引擎调用 → SSE 输出），不感知具体 RAG 实现（依赖 AgenticRAGEngine 抽象），
也不直接操作数据库表（委托 Repository）。

遵循开闭原则：通过依赖注入组合 ConversationRepository / MessageRepository /
MemoryManager / AgenticRAGEngine，新增 Agent 类型只需在 _SYSTEM_PROMPTS
注册表中追加提示词，不修改 chat 方法分支逻辑。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.memory import MemoryContext, MemoryManager
from app.models.conversation import Conversation, Message as MessageModel
from app.models.user import User
from app.rag.factory import get_rag_engine, get_rag_engine_by_model
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
)
from app.utils.logger import get_logger
from app.utils.sse import SSEEvent, SSEEventType

logger = get_logger(__name__)


@dataclass
class PreparedChat:
    """流式开始前完成准备工作的聊天上下文。

    所有 DB 读写均已在 ``prepare_chat`` 中完成 — 携带的数据均为
    原生类型（UUID / str），不持有 ORM 对象或 DB 连接引用。
    """

    query: str
    conversation_id: UUID
    agent_type: str
    tenant_id: str | None
    memory_context: str
    resolved_model_id: str
    default_model_id: str
    # P3-A: 原始查询（指代消解前的用户输入，供前端展示）
    original_query: str = ""
    # P3-A: 对话焦点（供 SSE 事件推送）
    conversation_focus: dict[str, Any] | None = None
    # P4-A: 漂移检测结果
    drift_info: dict[str, Any] | None = None
    # P4-F: 偏好偏移检测结果
    preference_overrides: dict[str, Any] | None = None
    # P4-G: 重复提问检测结果
    repetition_info: dict[str, Any] | None = None
    # P4-B: 后台矛盾检测任务引用（stream_chat 中检查完成状态）
    contradiction_task: asyncio.Task | None = None

# Agent 类型 → 系统提示词映射。
# 开闭原则落点：新增 Agent 类型只需在此字典追加一项，chat 方法无需改动。
# P0-4：系统提示词通过 memory_context 注入引擎的 generate 阶段，
# 引擎的 think 阶段使用自己的稳定决策 prompt（_THINK_SYSTEM_STABLE）。
_SYSTEM_PROMPTS: dict[str, str] = {
    "qa": (
        "你是一个企业知识库问答助手。请基于上下文和知识库内容，"
        "准确、简洁地回答用户问题；若信息不足请如实说明。"
    ),
    "workflow": (
        "你是一个企业工作流执行助手。请理解用户意图，"
        "引导用户完成业务流程，并在必要时调用可用工具。"
    ),
    "action": (
        "你是一个行动执行助手。请将用户指令转化为具体可执行步骤，"
        "并逐步协助用户完成操作。"
    ),
}


class ChatService:
    """AI 对话服务 — Agentic RAG 引擎集成。

    P0-4：chat 方法通过 AgenticRAGEngine.answer() 走完整的 Agent Loop，
    yield SSEEvent | str 供 ``sse_response()`` 包装为 SSE 文本流。

    会话生命周期：
    1. 若未提供 conversation_id，则创建新对话；
    2. 持久化用户消息；
    3. 加载四级记忆上下文（Mem0 偏好 + Checkpoint 状态 + 工作记忆）；
    4. 构建引擎 memory_context（系统提示词 + 记忆 + 对话历史）；
    5. yield meta 事件（conversation_id 绑定）；
    6. 调用 AgenticRAGEngine.answer()，透传所有 SSE 事件和 token；
    7. 流式结束后持久化完整 AI 回复消息；
    8. 保存记忆（Checkpoint 快照 + 提取用户偏好）。
    """

    def __init__(
        self, db: AsyncSession, user: User, tenant_id: UUID | None = None
    ) -> None:
        """初始化对话服务，注入依赖。

        Args:
            db: 异步数据库会话。
            user: 当前已认证用户。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: User = user
        self._tenant_id = tenant_id
        self.conv_repo: ConversationRepository = ConversationRepository(
            db, tenant_id=tenant_id
        )
        self.msg_repo: MessageRepository = MessageRepository(
            db, tenant_id=tenant_id
        )
        self.llm: LLMProvider = get_llm_provider()
        self.memory: MemoryManager = MemoryManager(db)

        # P1 IntentRouter — 懒初始化，失败不阻断现有功能
        self._intent_router = None
        self._shortcut_handler = None
        # P3-A: 焦点追踪器 + 指代消解器 — 懒初始化
        self._topic_tracker = None
        self._coreference_resolver = None

    # ------------------------------------------------------------------
    # 核心对话
    # ------------------------------------------------------------------

    async def prepare_chat(
        self,
        query: str,
        conversation_id: UUID | None,
        agent_type: str,
        tenant_id: str | None = None,
    ) -> PreparedChat:
        """流式开始前的全部 DB 读写（准备阶段）。

        完成会话获取/创建、权限校验、用户消息持久化、记忆上下文加载、
        模型解析。调用方在此之后应立即 commit 并释放 DB 连接回池，
        使 SSE 长连接期间不持有连接池连接（防高并发池耗尽）。

        Args:
            query: 用户输入的问题。
            conversation_id: 对话 ID，为 None 时创建新对话。
            agent_type: Agent 类型 — qa / workflow / action。
            tenant_id: 多租户预留（当前不实施隔离逻辑）。

        Returns:
            PreparedChat: 流式阶段所需的全部上下文（原生类型，不含 ORM 对象）。

        Raises:
            PermissionError: 指定的对话不属于当前用户 — 在 SSE 流开始前
                抛出，由 API 层转为 SSE error 事件返回友好错误。
        """
        # 1. 获取或创建对话
        if conversation_id is None:
            conversation = await self.conv_repo.create(
                user_id=self.user.id,
                title=query[:50] if query else "新对话",
                agent_type=agent_type,
            )
            conversation_id = conversation.id
        else:
            conversation = await self.conv_repo.get_by_id(conversation_id)
            if conversation is None or conversation.user_id != self.user.id:
                raise PermissionError("无权访问该对话")

        # 2. 持久化用户消息
        await self.msg_repo.create_message(
            conversation_id, "user", query
        )

        # 3. 加载记忆上下文（四级记忆：短期窗口 + Checkpoint + Mem0 偏好 + 工作记忆）
        memory_ctx = await self.memory.build_context(
            user_id=self.user.id,
            session_id=str(conversation_id),
            recent_messages=[{"role": "user", "content": query}],
        )

        # P3-A: 焦点追踪 + 指代消解 — 在构建 memory_context 之前执行
        # P4: 漂移检测 + 偏好偏移 + 重复提问 — 在焦点追踪之后执行
        resolved_query = query
        conversation_focus = None
        drift_info = None
        preference_overrides = None
        repetition_info = None
        try:
            from app.config import get_settings

            _settings = get_settings()
            if _settings.CONTEXT_FOCUS_TRACKING_ENABLED:
                # 从 DB 加载最近 N 轮历史（用于焦点追踪）
                _focus_window = _settings.CONTEXT_FOCUS_HISTORY_WINDOW
                focus_history = await self.msg_repo.get_by_conversation(
                    conversation_id, limit=_focus_window
                )
                history_dicts = [
                    {"role": msg.role, "content": msg.content}
                    for msg in focus_history
                ]

                topic_tracker = self._get_topic_tracker()
                if topic_tracker:
                    from app.context.focus_tracker import ConversationFocus

                    focus = await topic_tracker.extract_focus(history_dicts)
                    if focus:
                        conversation_focus = focus.to_dict()

                    # P4-A: 漂移检测 — 焦点提取后、指代消解前
                    if focus and getattr(_settings, "DRIFT_DETECTION_ENABLED", True):
                        drift_detector = self._get_drift_detector()
                        if drift_detector:
                            drift_result = await drift_detector.check(
                                query, focus, history_dicts,
                            )
                            if drift_result.is_drift and drift_result.action == "reset_focus":
                                # 漂移！重置焦点，重新提取
                                topic_tracker.reset_focus()
                                focus = await topic_tracker.extract_focus(history_dicts)
                                if focus:
                                    conversation_focus = focus.to_dict()
                                logger.info(
                                    "chat.drift_detected",
                                    drift_score=drift_result.drift_score,
                                    method=drift_result.detection_method,
                                )
                            drift_info = drift_result.to_dict()

                    # 指代消解 — P4-C 增强：注入历史 + 焦点栈
                    if focus and _settings.COREFERENCE_RESOLUTION_ENABLED:
                        resolver = self._get_coreference_resolver()
                        if resolver:
                            focus_stack = topic_tracker.get_focus_history(n=3)
                            resolved_query = await resolver.resolve(
                                query, focus,
                                history=history_dicts,
                                focus_stack=focus_stack,
                            )

                    if resolved_query != query:
                        logger.info(
                            "chat.query_resolved",
                            original=query[:100],
                            resolved=resolved_query[:100],
                        )

                    # P4-F: 偏好偏移检测 — 纯规则，零 Token，零延迟
                    if getattr(_settings, "PREFERENCE_DRIFT_ENABLED", True):
                        pref_detector = self._get_preference_drift_detector()
                        if pref_detector:
                            pref_result = pref_detector.detect(query)
                            if pref_result.has_preference_change:
                                preference_overrides = pref_result.to_dict()
                                logger.info(
                                    "chat.preference_changed",
                                    preference_type=pref_result.preference_type,
                                    new_value=pref_result.new_value,
                                )

                    # P4-G: 重复提问检测 — 复用 embedding
                    if getattr(_settings, "REPETITION_DETECTION_ENABLED", True):
                        rep_detector = self._get_repetition_detector()
                        if rep_detector:
                            rep_result = await rep_detector.check(query, history_dicts)
                            if rep_result.is_repetition:
                                repetition_info = rep_result.to_dict()
                                logger.info(
                                    "chat.repetition_detected",
                                    similarity=round(rep_result.similarity_score, 3),
                                    count=rep_result.repetition_count,
                                )
        except Exception as exc:
            logger.warning("chat.focus_tracking_failed", error=str(exc))
            resolved_query = query  # 优雅降级

        # 4. 构建引擎 memory_context（系统提示词 + 记忆 + 对话历史）
        memory_context = await self._build_engine_memory_context(
            conversation_id, agent_type, memory_ctx, resolved_query=resolved_query
        )

        # 5. 解析会话级模型选择（两级优先级：session > system default）
        from app.llm.model_config import get_default_model
        from app.services.model_selection_service import ModelSelectionService

        model_service = ModelSelectionService(self.db)
        resolved_model_id = await model_service.resolve_model(
            self.user.id, str(conversation_id)
        )
        default_model = get_default_model()
        default_model_id = default_model["id"] if default_model else ""

        return PreparedChat(
            query=resolved_query,
            conversation_id=conversation_id,
            agent_type=agent_type,
            tenant_id=tenant_id,
            memory_context=memory_context,
            resolved_model_id=resolved_model_id,
            default_model_id=default_model_id,
            original_query=query,
            conversation_focus=conversation_focus,
            drift_info=drift_info,
            preference_overrides=preference_overrides,
            repetition_info=repetition_info,
        )

    async def stream_chat(
        self,
        prepared: PreparedChat,
    ) -> AsyncIterator[SSEEvent | str]:
        """流式阶段 — SSE 长连接期间不持有 DB 连接池连接。

        进入本方法前调用方应已完成 ``prepare_chat`` 并释放连接；
        引擎仅在危险工具审批等极少数路径才按需短暂重新获取连接；
        流式结束后的持久化使用短事务，写完即释放。

        权限异常统一转为 SSE error 事件 — 前端可收到友好错误，
        而不是裸断流 / HTTP 500。

        Yields:
            SSEEvent | str: SSE 事件对象（meta/thinking/retrieve/tool_call/
            sources/quality/done）或 token 字符串。
        """
        query = prepared.query
        conversation_id = prepared.conversation_id
        agent_type = prepared.agent_type
        resolved_model_id = prepared.resolved_model_id
        default_model_id = prepared.default_model_id

        # 5. 向客户端推送对话元数据（便于前端绑定会话）
        yield SSEEvent(
            data={
                "conversation_id": str(conversation_id),
                "agent_type": agent_type,
                "model_id": resolved_model_id,
            },
            event=SSEEventType.META,
        )

        # P3-A: 推送上下文消解结果（如果查询被改写）
        if prepared.original_query and prepared.original_query != prepared.query:
            yield SSEEvent(
                data={
                    "original_query": prepared.original_query,
                    "resolved_query": prepared.query,
                    "focus": prepared.conversation_focus,
                },
                event=SSEEventType.CONTEXT_RESOLVED,
            )

        # P4-A: 推送漂移检测结果
        if prepared.drift_info and prepared.drift_info.get("is_drift"):
            yield SSEEvent(
                data=prepared.drift_info,
                event=SSEEventType.DRIFT_DETECTED,
            )

        # P4-F: 推送偏好偏移结果
        if prepared.preference_overrides:
            yield SSEEvent(
                data=prepared.preference_overrides,
                event=SSEEventType.PREFERENCE_CHANGED,
            )

        # P4-G: 推送重复提问检测结果
        if prepared.repetition_info and prepared.repetition_info.get("is_repetition"):
            yield SSEEvent(
                data=prepared.repetition_info,
                event=SSEEventType.REPETITION_DETECTED,
            )

        # P4-F: 偏好偏移 → 注入 system prompt 风格指令
        memory_context = prepared.memory_context
        if prepared.preference_overrides:
            from app.context.preference_drift_detector import PreferenceDriftDetector

            pref_detector = PreferenceDriftDetector()
            from app.context.preference_drift_detector import PreferenceDriftResult

            pref_result = PreferenceDriftResult(
                has_preference_change=True,
                preference_type=prepared.preference_overrides.get("preference_type", ""),
                new_value=prepared.preference_overrides.get("new_value", ""),
            )
            modifier = pref_detector.get_system_prompt_modifier(pref_result)
            if modifier:
                memory_context = memory_context + "\n\n" + modifier

        full_response_parts: list[str] = []

        # P1: IntentRouter 稳态/敏态分离 — 简单查询走快捷路径，复杂查询走 Agent Loop
        shortcut_taken = False
        try:
            from app.config import get_settings

            settings = get_settings()
            if settings.INTENT_ROUTER_ENABLED and settings.INTENT_SHORTCUT_ENABLED:
                intent_router = self._get_intent_router()
                intent_result = await intent_router.route(
                    query=query,
                    memory_context=prepared.memory_context,
                    agent_type=agent_type,
                )

                # 推送意图识别结果事件
                yield SSEEvent(
                    data={
                        "intent": intent_result.intent.value,
                        "confidence": intent_result.confidence,
                        "shortcut": intent_result.use_shortcut,
                    },
                    event=SSEEventType.INTENT,
                )

                if intent_result.use_shortcut:
                    # 快捷路径 — 确定性检索 + 1 次 LLM 生成
                    shortcut_handler = self._get_shortcut_handler()
                    async for chunk in shortcut_handler.handle(
                        intent=intent_result,
                        query=query,
                        user=self.user,
                        db=self.db,
                        tenant_id=self._tenant_id,
                        memory_context=prepared.memory_context,
                    ):
                        if isinstance(chunk, str):
                            full_response_parts.append(chunk)
                        yield chunk
                    shortcut_taken = True
        except Exception as exc:
            logger.warning("chat.intent_router_failed", error=str(exc))
            # IntentRouter 失败 → 回退到 Agent Loop（不阻断）

        if not shortcut_taken:
            # 6. 调用 Agentic RAG 引擎，透传所有 SSE 事件和 token
            # P1-4: 传入 db / user_uuid 以支持危险工具审批记录持久化
            # P2-5: 如果会话选择了非默认模型，使用该模型的引擎
            if resolved_model_id and resolved_model_id != default_model_id:
                try:
                    engine = get_rag_engine_by_model(resolved_model_id)
                except ValueError:
                    # 模型配置无效 — 回退到默认引擎
                    engine = get_rag_engine()
            else:
                engine = get_rag_engine()

            # P4-B: 后台启动用户陈述矛盾检测（不阻塞首 token）
            contra_task = None
            try:
                from app.config import get_settings

                contra_settings = get_settings()
                if getattr(contra_settings, "CONTRADICTION_DETECTION_ENABLED", True) and \
                   getattr(contra_settings, "CONTRADICTION_CHECK_USER_STATEMENTS", True):
                    contra_detector = self._get_contradiction_detector()
                    if contra_detector:
                        # 加载历史用于矛盾检测
                        contra_history = await self.msg_repo.get_by_conversation(
                            conversation_id, limit=12
                        )
                        contra_history_dicts = [
                            {"role": msg.role, "content": msg.content}
                            for msg in contra_history
                        ]
                        contra_task = asyncio.create_task(
                            contra_detector.check_user_contradiction(
                                query, contra_history_dicts,
                            )
                        )
            except Exception as exc:
                logger.warning("chat.contradiction_task_init_failed", error=str(exc))

            contra_pushed = False
            try:
                async for chunk in engine.answer(
                    query=query,
                    user_id=str(self.user.id),
                    session_id=str(conversation_id),
                    memory_context=memory_context,
                    tenant_id=prepared.tenant_id,
                    db=self.db,
                    user_uuid=self.user.id,
                    # P4-E: 传入对话焦点和漂移信息
                    conversation_focus=prepared.conversation_focus,
                    drift_info=prepared.drift_info,
                ):
                    # P4-B: 在 token 流中检查后台矛盾检测是否完成
                    if contra_task and not contra_pushed and contra_task.done():
                        try:
                            contra_result = contra_task.result()
                            if contra_result.has_contradiction:
                                yield SSEEvent(
                                    data=contra_result.to_dict(),
                                    event=SSEEventType.CONTRADICTION_DETECTED,
                                )
                        except Exception:
                            pass  # 优雅降级
                        contra_pushed = True

                    if isinstance(chunk, str):
                        full_response_parts.append(chunk)
                    yield chunk
            except PermissionError as exc:
                # 权限异常 → SSE error 事件（前端可友好展示，而非断流）
                logger.info(
                    "chat.stream_permission_denied",
                    error=str(exc),
                    conversation_id=str(conversation_id),
                )
                yield SSEEvent(
                    data={"type": "error", "message": str(exc)},
                    event=SSEEventType.ERROR,
                )
                return

            # P4-B: 引擎流结束后，检查矛盾检测是否已完成且尚未推送
            if contra_task and not contra_pushed:
                try:
                    await asyncio.wait_for(contra_task, timeout=2.0)
                    if contra_task.done():
                        contra_result = contra_task.result()
                        if contra_result.has_contradiction:
                            yield SSEEvent(
                                data=contra_result.to_dict(),
                                event=SSEEventType.CONTRADICTION_DETECTED,
                            )
                except Exception:
                    pass  # 超时或异常 — 优雅降级

        # 7-8. 流式结束后的持久化 — 短事务，写完即释放连接回池
        await self._persist_assistant_result(
            conversation_id,
            query,
            "".join(full_response_parts),
            resolved_model_id,
        )

    async def _persist_assistant_result(
        self,
        conversation_id: UUID,
        query: str,
        assistant_content: str,
        resolved_model_id: str,
    ) -> None:
        """持久化 AI 回复并写入记忆 — 短事务，写完即释放连接回池。

        session 若在流式前已被 close（连接已归还池），此处由 SQLAlchemy
        按需重新获取连接；补设 RLS 租户上下文后一次性 commit + close。
        """
        from sqlalchemy import text

        if self._tenant_id:
            # 补设 RLS 租户上下文（连接已更换，原 SET LOCAL 随事务结束失效）
            await self.db.execute(
                text("SET LOCAL app.tenant_id = :tid"),
                {"tid": str(self._tenant_id)},
            )

        # 7. 持久化完整 AI 回复
        await self.msg_repo.create_message(
            conversation_id,
            "assistant",
            assistant_content,
            token_count=len(assistant_content),
            model_used=resolved_model_id or None,
        )

        # 8. 保存记忆（Checkpoint 快照 + 提取用户偏好 + 关键决策持久化）
        try:
            await self.memory.save_session(
                user_id=self.user.id,
                session_id=str(conversation_id),
                agent_state={"iteration": 0, "retrieved_docs": []},
                summary=f"用户提问：{query[:60]}；AI回复：{assistant_content[:60]}",
            )
            await self.memory.extract_and_save_facts(
                self.user.id,
                [{"role": "user", "content": query}],
            )
            # P1-2: 跨轮关键决策显式持久化到 working memory
            # 防中间遗忘：关键决策不依赖模型从历史中"找回"
            await self.memory.extract_and_save_key_decisions(
                user_id=self.user.id,
                query=query,
                answer=assistant_content,
            )
        except Exception as e:
            logger.warning("memory_save_failed", error=str(e))

        await self.db.commit()
        await self.db.close()

        # 引擎已 yield done 事件，无需重复发送

    async def chat(
        self,
        query: str,
        conversation_id: UUID | None,
        agent_type: str,
        tenant_id: str | None = None,
    ) -> AsyncIterator[SSEEvent | str]:
        """与 AI 对话，流式返回 SSE 事件和 token。

        P0-4 重构：通过 AgenticRAGEngine.answer() 走完整的 Agent Loop，
        所有 agent_type 统一走引擎路径，不分流。

        组合 ``prepare_chat``（准备阶段 DB 读写）与 ``stream_chat``
        （流式生成），供非 SSE 端点（如 agents）复用。

        流程：
        1. 获取或创建对话（无 conversation_id 时新建）；
        2. 保存用户消息到数据库；
        3. 加载四级记忆上下文（短期窗口 + Checkpoint + Mem0 偏好 + 工作记忆）；
        4. 构建引擎 memory_context（系统提示词 + 记忆 + 对话历史）；
        5. yield meta 事件（conversation_id + agent_type）；
        6. 调用 AgenticRAGEngine.answer()，透传所有 SSE 事件和 token；
        7. 累积完整回复后保存为 assistant 消息；
        8. 保存记忆（Checkpoint 快照 + 提取用户偏好）。

        Args:
            query: 用户输入的问题。
            conversation_id: 对话 ID，为 None 时创建新对话。
            agent_type: Agent 类型 — qa / workflow / action。
            tenant_id: 多租户预留（当前不实施隔离逻辑）。

        Yields:
            SSEEvent | str: SSE 事件对象（meta/thinking/retrieve/tool_call/
            sources/quality/done）或 token 字符串。

        Raises:
            PermissionError: 指定的对话不属于当前用户。
        """
        prepared = await self.prepare_chat(
            query=query,
            conversation_id=conversation_id,
            agent_type=agent_type,
            tenant_id=tenant_id,
        )
        async for chunk in self.stream_chat(prepared):
            yield chunk

    # ------------------------------------------------------------------
    # 对话查询
    # ------------------------------------------------------------------

    async def get_conversations(self) -> list[Conversation]:
        """查询当前用户的所有对话列表（按创建时间倒序）。"""
        return await self.conv_repo.get_by_user(self.user.id)

    async def get_conversation_messages(
        self, conversation_id: UUID
    ) -> list[MessageModel]:
        """查询指定对话下的全部消息（按时间正序，保证对话顺序）。

        Args:
            conversation_id: 对话 ID。

        Returns:
            Message 列表。

        Raises:
            PermissionError: 对话不存在或不属于当前用户。
        """
        conversation = await self.conv_repo.get_by_id(conversation_id)
        if conversation is None or conversation.user_id != self.user.id:
            raise PermissionError("无权访问该对话")
        return await self.msg_repo.get_by_conversation(conversation_id)

    # ------------------------------------------------------------------
    # Agent 调用
    # ------------------------------------------------------------------

    async def stream_agent_response(
        self,
        query: str,
        agent_config: Any,
        session_id: str | None = None,
        context: dict | None = None,
    ) -> AsyncIterator[str]:
        """Agent 调用入口 — 复用 chat 流式管线，仅提取 token 文本。

        P0-4 更新：chat() 现在 yield SSEEvent | str，本方法过滤出 str token
        供 Agent 调用方使用（跳过 SSEEvent 事件对象）。

        Args:
            query: 用户输入的问题。
            agent_config: AgentConfig ORM 实例（含 name / agent_type / system_prompt 等）。
            session_id: 可选，已有对话 ID（字符串形式）。为 None 时新建对话。
            context: 可选，额外上下文（如 system_prompt 覆盖）。

        Yields:
            str: 纯文本 token 片段。
        """
        # 推断 agent_type：优先 agent_config.agent_type，回退 name
        agent_type = getattr(agent_config, "agent_type", None) or getattr(
            agent_config, "name", "qa"
        )

        # session_id 字符串转 UUID
        conversation_id: UUID | None = None
        if session_id:
            try:
                conversation_id = UUID(session_id)
            except (ValueError, AttributeError):
                conversation_id = None

        # 复用 chat() 的流式管线，仅提取 str token
        async for chunk in self.chat(query, conversation_id, agent_type):
            if isinstance(chunk, str):
                yield chunk

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _get_intent_router(self):
        """懒初始化 IntentRouter — 失败返回 None，不影响现有功能。"""
        if self._intent_router is None:
            try:
                from app.intent.router import IntentRouter

                self._intent_router = IntentRouter(llm_provider=self.llm)
            except Exception as exc:
                logger.warning("chat.intent_router_init_failed", error=str(exc))
                return None
        return self._intent_router

    def _get_shortcut_handler(self):
        """懒初始化 ShortcutHandler。"""
        if self._shortcut_handler is None:
            try:
                from app.intent.shortcut_handler import ShortcutHandler

                self._shortcut_handler = ShortcutHandler()
            except Exception as exc:
                logger.warning("chat.shortcut_handler_init_failed", error=str(exc))
                return None
        return self._shortcut_handler

    def _get_topic_tracker(self):
        """懒初始化 TopicTracker — P3-A 焦点追踪。"""
        if self._topic_tracker is None:
            try:
                from app.context.focus_tracker import TopicTracker

                self._topic_tracker = TopicTracker(llm=self.llm)
            except Exception as exc:
                logger.warning("chat.topic_tracker_init_failed", error=str(exc))
                return None
        return self._topic_tracker

    def _get_coreference_resolver(self):
        """懒初始化 CoreferenceResolver — P3-A 指代消解。"""
        if self._coreference_resolver is None:
            try:
                from app.context.coreference_resolver import CoreferenceResolver

                self._coreference_resolver = CoreferenceResolver(llm=self.llm)
            except Exception as exc:
                logger.warning("chat.coreference_resolver_init_failed", error=str(exc))
                return None
        return self._coreference_resolver

    def _get_drift_detector(self):
        """懒初始化 DriftDetector — P4-A 漂移检测。"""
        if not hasattr(self, "_drift_detector") or self._drift_detector is None:
            self._drift_detector = None
            try:
                from app.context.drift_detector import DriftDetector

                self._drift_detector = DriftDetector()
            except Exception as exc:
                logger.warning("chat.drift_detector_init_failed", error=str(exc))
        return self._drift_detector

    def _get_contradiction_detector(self):
        """懒初始化 ContradictionDetector — P4-B 矛盾检测。"""
        if not hasattr(self, "_contradiction_detector") or self._contradiction_detector is None:
            self._contradiction_detector = None
            try:
                from app.context.contradiction_detector import ContradictionDetector

                self._contradiction_detector = ContradictionDetector(llm=self.llm)
            except Exception as exc:
                logger.warning("chat.contradiction_detector_init_failed", error=str(exc))
        return self._contradiction_detector

    def _get_preference_drift_detector(self):
        """懒初始化 PreferenceDriftDetector — P4-F 偏好偏移检测。"""
        if not hasattr(self, "_preference_drift_detector") or self._preference_drift_detector is None:
            self._preference_drift_detector = None
            try:
                from app.context.preference_drift_detector import PreferenceDriftDetector

                self._preference_drift_detector = PreferenceDriftDetector()
            except Exception as exc:
                logger.warning("chat.preference_drift_detector_init_failed", error=str(exc))
        return self._preference_drift_detector

    def _get_repetition_detector(self):
        """懒初始化 RepetitionDetector — P4-G 重复提问检测。"""
        if not hasattr(self, "_repetition_detector") or self._repetition_detector is None:
            self._repetition_detector = None
            try:
                from app.context.repetition_detector import RepetitionDetector

                self._repetition_detector = RepetitionDetector()
            except Exception as exc:
                logger.warning("chat.repetition_detector_init_failed", error=str(exc))
        return self._repetition_detector

    async def _build_engine_memory_context(
        self,
        conversation_id: UUID,
        agent_type: str,
        memory_ctx: MemoryContext | None = None,
        resolved_query: str = "",
    ) -> str:
        """构建传给 AgenticRAGEngine.answer() 的 memory_context 字符串。

        P0-4：将系统提示词 + 记忆上下文 + 对话历史合并为一个字符串，
        传入引擎的 generate 阶段作为上下文补充。

        P3-B：对话历史使用语义选择器筛选，不再全量注入。
        P3-C：超过阈值时旧历史压缩为摘要。

        结构：[系统提示词] + [记忆片段（偏好 + 短期窗口）] + [摘要(可选)] + [对话历史]

        Args:
            conversation_id: 对话 ID。
            agent_type: Agent 类型，用于选择系统提示词。
            memory_ctx: 记忆上下文（四级记忆合并），为 None 时不注入。
            resolved_query: P3-A 消解后的查询（用于 P3-B 语义选择）。

        Returns:
            memory_context 字符串。
        """
        parts: list[str] = []

        # 1. 系统提示词（agent_type 决定角色定位）
        system_prompt = _SYSTEM_PROMPTS.get(agent_type, _SYSTEM_PROMPTS["qa"])
        parts.append(system_prompt)

        # 2. 记忆片段（偏好 + 短期窗口）
        if memory_ctx:
            fragment = memory_ctx.to_system_prompt(render_short_term=True)
            if fragment:
                parts.append(fragment)

        # 3. 对话历史 — 若记忆上下文无 short_term，从 DB 加载
        if not (memory_ctx and memory_ctx.short_term):
            _HISTORY_WINDOW = 16  # 最近 8 轮对话（16 条消息）
            history = await self.msg_repo.get_by_conversation(
                conversation_id, limit=_HISTORY_WINDOW
            )
            if history:
                history_dicts = [
                    {"role": msg.role, "content": msg.content}
                    for msg in history[:-1]  # 排除最后一条（刚保存的当前用户消息）
                ]

                # P3-C: 滚动摘要压缩 — 旧历史超阈值时压缩为摘要 + 保留近期原文
                try:
                    from app.config import get_settings

                    _settings = get_settings()
                    if _settings.CONVERSATION_SUMMARIZER_ENABLED and history_dicts:
                        from app.context.conversation_summarizer import (
                            ConversationSummarizer,
                        )

                        # 获取已有摘要（从 memory_facts 的 summary 类别检索）
                        existing_summary = ""
                        if memory_ctx and memory_ctx.user_facts:
                            for fact in memory_ctx.user_facts[:5]:
                                if fact.get("category") == "summary":
                                    existing_summary = fact.get("fact_text", "")
                                    break

                        summarizer = ConversationSummarizer(
                            llm=self.llm,
                            max_tokens=_settings.CONVERSATION_SUMMARIZER_MAX_TOKENS,
                            retained_tokens=_settings.CONVERSATION_SUMMARIZER_RETAINED_TOKENS,
                        )
                        summary, recent_msgs = await summarizer.summarize_if_needed(
                            history_dicts, existing_summary=existing_summary,
                        )
                        # 摘要非空时注入，recent_msgs 替换 history_dicts
                        if summary and summary != existing_summary:
                            parts.append(f"对话摘要：\n{summary}")
                        history_dicts = recent_msgs
                except Exception as exc:
                    logger.warning("chat.conversation_summarizer_failed", error=str(exc))
                    # 降级：使用原始历史

                # P3-B: 语义上下文选择
                try:
                    from app.config import get_settings

                    _settings = get_settings()
                    if _settings.CONTEXT_SELECTOR_ENABLED and resolved_query:
                        from app.context.context_selector import ContextSelector

                        selector = ContextSelector(
                            max_tokens=_settings.CONTEXT_SELECTOR_MAX_TOKENS,
                        )
                        history_dicts = await selector.select(
                            resolved_query,
                            history_dicts,
                            top_k=_settings.CONTEXT_SELECTOR_TOP_K,
                        )
                except Exception as exc:
                    logger.warning("chat.context_selector_failed", error=str(exc))
                    # 降级：使用原始历史

                if history_dicts:
                    history_lines: list[str] = []
                    for msg_dict in history_dicts:
                        role_label = "用户" if msg_dict.get("role") == "user" else "助手"
                        history_lines.append(f"[{role_label}] {msg_dict.get('content', '')}")
                    if history_lines:
                        parts.append("对话历史：\n" + "\n".join(history_lines))

        return "\n\n".join(parts)
