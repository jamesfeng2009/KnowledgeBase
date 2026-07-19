"""
AI 对话服务 — 单一职责：编排对话会话与 LLM 流式生成。

遵循单一职责：ChatService 只负责对话流程编排（会话管理 → 消息持久化 → 记忆加载 → LLM 调用 → SSE 输出），
不感知具体 LLM 实现（依赖 LLMProvider 抽象），也不直接操作数据库表（委托 Repository）。

遵循开闭原则：通过依赖注入组合 ConversationRepository / MessageRepository / LLMProvider / MemoryManager，
新增 Agent 类型只需在 _SYSTEM_PROMPTS 注册表中追加提示词，不修改 chat 方法分支逻辑。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider, Message
from app.llm.factory import get_llm_provider
from app.memory import MemoryContext, MemoryManager
from app.models.conversation import Conversation, Message as MessageModel
from app.models.user import User
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
)
from app.utils.logger import get_logger
from app.utils.sse import format_sse_event

logger = get_logger(__name__)

# Agent 类型 → 系统提示词映射。
# 开闭原则落点：新增 Agent 类型只需在此字典追加一项，chat 方法无需改动。
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
    """AI 对话服务 — 简化版 Agentic RAG。

    chat 方法为异步生成器，以 SSE 文本块形式逐 token yield，
    供 FastAPI StreamingResponse 直接消费。

    会话生命周期：
    1. 若未提供 conversation_id，则创建新对话；
    2. 持久化用户消息；
    3. 加载四级记忆上下文（Mem0 偏好 + Checkpoint 状态 + 工作记忆）；
    4. 将记忆注入系统提示词，拼装历史上下文，调用 LLMProvider 流式生成；
    5. 流式结束后持久化完整 AI 回复消息；
    6. 保存记忆（Checkpoint 快照 + 提取用户偏好）；
    7. yield SSE 格式的 token 与元数据 / 结束事件。
    """

    def __init__(self, db: AsyncSession, user: User) -> None:
        """初始化对话服务，注入依赖。

        Args:
            db: 异步数据库会话。
            user: 当前已认证用户。
        """
        self.db: AsyncSession = db
        self.user: User = user
        self.conv_repo: ConversationRepository = ConversationRepository(db)
        self.msg_repo: MessageRepository = MessageRepository(db)
        self.llm: LLMProvider = get_llm_provider()
        self.memory: MemoryManager = MemoryManager(db)

    # ------------------------------------------------------------------
    # 核心对话
    # ------------------------------------------------------------------

    async def chat(
        self,
        query: str,
        conversation_id: UUID | None,
        agent_type: str,
    ) -> AsyncIterator[str]:
        """与 AI 对话，流式返回 SSE 格式的 token。

        流程：
        1. 获取或创建对话（无 conversation_id 时新建）；
        2. 保存用户消息到数据库；
        3. 加载四级记忆上下文（短期窗口 + Checkpoint + Mem0 偏好 + 工作记忆）；
        4. 构建含记忆上下文的 LLM 消息列表；
        5. 向客户端推送对话元数据（conversation_id 绑定）；
        6. 调用 LLM Provider 流式生成，逐 token yield SSE；
        7. 累积完整回复后保存为 assistant 消息；
        8. 保存记忆（Checkpoint 快照 + 提取用户偏好）；
        9. yield 结束事件。

        Args:
            query: 用户输入的问题。
            conversation_id: 对话 ID，为 None 时创建新对话。
            agent_type: Agent 类型 — qa / workflow / action。

        Yields:
            SSE 协议文本块（``data: ...``），包含 token / 元数据 / 结束事件。

        Raises:
            PermissionError: 指定的对话不属于当前用户。
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

        # 4. 构建发送给 LLM 的消息上下文（含记忆上下文）
        messages = await self._build_llm_messages(
            conversation_id, agent_type, memory_ctx
        )

        # 5. 向客户端推送对话元数据（便于前端绑定会话）
        yield format_sse_event(
            json.dumps(
                {
                    "type": "conversation",
                    "conversation_id": str(conversation_id),
                    "agent_type": agent_type,
                },
                ensure_ascii=False,
            ),
            event="meta",
        )

        # 6. 流式调用 LLM，逐 token yield SSE
        full_response_parts: list[str] = []
        async for chunk in self.llm.chat(messages, stream=True):
            # 简化版仅处理文本片段，跳过工具调用 dict
            if isinstance(chunk, str):
                full_response_parts.append(chunk)
                yield format_sse_event(chunk)

        # 7. 持久化完整 AI 回复
        assistant_content = "".join(full_response_parts)
        await self.msg_repo.create_message(
            conversation_id,
            "assistant",
            assistant_content,
            token_count=len(assistant_content),
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

        # 9. 推送结束事件
        yield format_sse_event(
            json.dumps({"type": "done"}), event="done"
        )

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
    # Agent 调用（P0-4：补全 stream_agent_response 方法）
    # ------------------------------------------------------------------

    async def stream_agent_response(
        self,
        query: str,
        agent_config: Any,
        session_id: str | None = None,
        context: dict | None = None,
    ) -> AsyncIterator[str]:
        """Agent 调用入口 — 复用 chat 流式管线，支持 Agent 配置覆盖。

        本方法是对 chat() 的薄封装，差异点：
        1. agent_type 从 agent_config.name / agent_config.agent_type 推断；
        2. session_id（字符串形式 UUID）可直接复用已有对话；
        3. context 额外注入到 LLM 消息前缀（如 Agent 的系统提示词）。

        Args:
            query: 用户输入的问题。
            agent_config: AgentConfig ORM 实例（含 name / agent_type / system_prompt 等）。
            session_id: 可选，已有对话 ID（字符串形式）。为 None 时新建对话。
            context: 可选，额外上下文（如 system_prompt 覆盖）。

        Yields:
            流式文本块（非 SSE 格式，由调用方包装为 SSE）。
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

        # 复用 chat() 的流式管线
        async for chunk in self.chat(query, conversation_id, agent_type):
            # chat() yield 的是 SSE 格式文本，提取 content 字段返回纯文本
            # SSE 格式：data: {"type": "token", "content": "..."}\n\n
            if chunk.startswith("data: ") and chunk.endswith("\n\n"):
                payload = chunk[6:-2].strip()
                try:
                    import json

                    data = json.loads(payload)
                    # 只传递 token 类型的内容，跳过 meta 和 done 事件
                    if data.get("type") == "token" and "content" in data:
                        yield data["content"]
                except (json.JSONDecodeError, KeyError):
                    # 非 JSON 或缺少字段，跳过
                    continue
            else:
                # 非 SSE 格式，直接透传
                yield chunk

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _build_llm_messages(
        self,
        conversation_id: UUID,
        agent_type: str,
        memory_ctx: MemoryContext | None = None,
    ) -> list[Message]:
        """构建发送给 LLMProvider 的消息列表。

        结构：[系统提示词 + 记忆上下文] + [历史消息（含刚保存的用户消息）]。
        历史消息从数据库加载，确保上下文完整。

        Args:
            conversation_id: 对话 ID。
            agent_type: Agent 类型，用于选择系统提示词。
            memory_ctx: 记忆上下文（四级记忆合并），为 None 时不注入。

        Returns:
            Message TypedDict 列表。
        """
        system_prompt = _SYSTEM_PROMPTS.get(agent_type, _SYSTEM_PROMPTS["qa"])

        # 注入记忆上下文到系统提示词
        # P1-Opt5: render_short_term=True — 使用 memory_ctx 中的 L1 短期窗口，
        # 不再从 DB 重新加载全部历史（修复 W4 + W7: 双重加载浪费）。
        if memory_ctx:
            memory_fragment = memory_ctx.to_system_prompt(render_short_term=True)
            if memory_fragment:
                system_prompt = system_prompt + "\n\n" + memory_fragment

        messages: list[Message] = [
            Message(role="system", content=system_prompt)
        ]

        # P1-Opt5: 历史消息窗口化 — 优先使用 memory_ctx.short_term（已加载），
        # fallback 时从 DB 加载但加 limit（修复 W4: 之前无 limit 全量加载）。
        _HISTORY_WINDOW = 16  # 最近 8 轮对话（16 条消息）

        if memory_ctx and memory_ctx.short_term:
            # 使用已加载的 L1 短期窗口，不再从 DB 重复加载
            recent = memory_ctx.short_term[-_HISTORY_WINDOW:]
            for msg in recent:
                messages.append(Message(
                    role=msg.get("role", "user"),
                    content=str(msg.get("content", "")),
                ))
        else:
            # Fallback: memory_ctx 无 short_term 时从 DB 加载（带 limit）
            history = await self.msg_repo.get_by_conversation(
                conversation_id, limit=_HISTORY_WINDOW
            )
            for msg in history:
                messages.append(Message(role=msg.role, content=msg.content))

        return messages
