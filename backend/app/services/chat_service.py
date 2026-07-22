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

        # 4. 构建引擎 memory_context（系统提示词 + 记忆 + 对话历史）
        memory_context = await self._build_engine_memory_context(
            conversation_id, agent_type, memory_ctx
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
            query=query,
            conversation_id=conversation_id,
            agent_type=agent_type,
            tenant_id=tenant_id,
            memory_context=memory_context,
            resolved_model_id=resolved_model_id,
            default_model_id=default_model_id,
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
        full_response_parts: list[str] = []
        try:
            async for chunk in engine.answer(
                query=query,
                user_id=str(self.user.id),
                session_id=str(conversation_id),
                memory_context=prepared.memory_context,
                tenant_id=prepared.tenant_id,
                db=self.db,
                user_uuid=self.user.id,
            ):
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

        # 8. 保存记忆（Checkpoint 快照 + 提取用户偏好）
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

    async def _build_engine_memory_context(
        self,
        conversation_id: UUID,
        agent_type: str,
        memory_ctx: MemoryContext | None = None,
    ) -> str:
        """构建传给 AgenticRAGEngine.answer() 的 memory_context 字符串。

        P0-4：将系统提示词 + 记忆上下文 + 对话历史合并为一个字符串，
        传入引擎的 generate 阶段作为上下文补充。

        结构：[系统提示词] + [记忆片段（偏好 + 短期窗口）] + [对话历史]

        Args:
            conversation_id: 对话 ID。
            agent_type: Agent 类型，用于选择系统提示词。
            memory_ctx: 记忆上下文（四级记忆合并），为 None 时不注入。

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
                history_lines: list[str] = []
                for msg in history[:-1]:  # 排除最后一条（刚保存的当前用户消息）
                    role_label = "用户" if msg.role == "user" else "助手"
                    history_lines.append(f"[{role_label}] {msg.content}")
                if history_lines:
                    parts.append("对话历史：\n" + "\n".join(history_lines))

        return "\n\n".join(parts)
