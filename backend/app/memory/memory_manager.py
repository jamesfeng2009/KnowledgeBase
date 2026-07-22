"""
四级记忆编排器 — 单一职责：协调四级记忆源的读写。

记忆层级（从快到慢）：
  L1 短期窗口    — 当前对话最近 N 条消息（Message 表）
  L2 Checkpoint  — LangGraph 会话状态（Agent Loop 中间状态）
  L3 Mem0        — 跨会话长期偏好和事实
  L4 工作记忆    — 当前任务相关的实体和关系

遵循开闭原则：新增记忆源只需扩展 MemoryManager，不修改调用方。
遵循单一职责：编排器只做协调，具体存储委托给各管理器。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.checkpoint import CheckpointManager
from app.memory.graphiti_manager import GraphitiManager
from app.memory.mem0_manager import Mem0Manager
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 短期窗口大小：保留最近 N 条消息作为上下文
SHORT_TERM_WINDOW_SIZE = 20

# P1-Opt5: L1 短期窗口注入 LLM 的最大消息条数（每条截断到 _SHORT_TERM_MSG_MAX_CHARS）
_SHORT_TERM_INJECT_SIZE = 8  # 最近 4 轮对话（8 条消息）
_SHORT_TERM_MSG_MAX_CHARS = 200  # 每条消息截断到 200 字符

# P1-Opt5: L3 用户偏好注入 top-N（从全量 10 条缩减到 top-3，省 ~200 tok）
_L3_INJECT_TOP_N = 3


class MemoryContext:
    """聚合后的记忆上下文 — 传递给 Agent Loop 的完整记忆。"""

    def __init__(self):
        self.short_term: list[dict] = []       # L1: 最近消息
        self.checkpoint: dict | None = None      # L2: 会话状态
        self.user_facts: list[dict] = []        # L3: 用户偏好和事实
        self.working_memory: list[dict] = []    # L4: 工作记忆

    def to_system_prompt(self, render_short_term: bool = False) -> str:
        """将记忆上下文转换为 system prompt 片段。

        P1-Opt5: 新增 render_short_term 参数，为 True 时渲染 L1 短期窗口
        （修复 W7: 之前 L1 加载后不渲染，ChatService 另从 DB 双重加载）。
        L3 用户偏好从全量 top-10 缩减到 top-3，省 ~200 tok。

        Args:
            render_short_term: 是否渲染 L1 短期窗口到 system prompt。
                ChatService 传 True（使用 memory_ctx 中的 short_term，
                不再从 DB 重新加载）；AgenticRAGEngine 传 False（由
                Agent Loop 自己管理对话历史）。

        Returns:
            拼接后的 system prompt 片段。
        """
        parts = []

        # P1-Opt5: L1 短期窗口 — 修复 W7（之前加载后不渲染）
        if render_short_term and self.short_term:
            recent = self.short_term[-_SHORT_TERM_INJECT_SIZE:]
            parts.append("=== 近期对话 ===")
            for msg in recent:
                role = msg.get("role", "user")
                content = str(msg.get("content", ""))[:_SHORT_TERM_MSG_MAX_CHARS]
                parts.append(f"{role}: {content}")

        # P1-Opt5: L3 用户偏好 — top-3 而非 top-10
        if self.user_facts:
            prefs = [f["fact_text"] for f in self.user_facts if f.get("category") == "preference"]
            if prefs:
                parts.append("用户偏好：\n" + "\n".join(f"  - {p}" for p in prefs[:_L3_INJECT_TOP_N]))

            summaries = [f["fact_text"] for f in self.user_facts if f.get("category") == "summary"]
            if summaries:
                parts.append("历史摘要：\n" + "\n".join(f"  - {s}" for s in summaries[:_L3_INJECT_TOP_N]))

        # L4: 工作记忆
        if self.working_memory:
            working = [f["fact_text"] for f in self.working_memory]
            parts.append("当前任务上下文：\n" + "\n".join(f"  - {w}" for w in working))

        # L2: Checkpoint 恢复
        if self.checkpoint:
            iteration = self.checkpoint.get("iteration", 0)
            retrieved_count = len(self.checkpoint.get("retrieved_docs", []))
            parts.append(f"（从上次中断处恢复：已迭代 {iteration} 次，已检索 {retrieved_count} 条文档）")

        return "\n\n".join(parts) if parts else ""

    def to_dict(self) -> dict:
        return {
            "short_term": self.short_term,
            "checkpoint": self.checkpoint,
            "user_facts": self.user_facts,
            "working_memory": self.working_memory,
        }


class MemoryManager:
    """四级记忆编排器 — 协调所有记忆源的读写。

    使用方式：
        memory = MemoryManager(db)
        ctx = await memory.build_context(user_id, session_id, messages)
        system_prompt = ctx.to_system_prompt()

        # 对话结束后保存
        await memory.save_session(user_id, session_id, agent_state, summary)
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.mem0 = Mem0Manager(db)
        self.graphiti = GraphitiManager(db)
        self.checkpoint = CheckpointManager(db)

    async def build_context(
        self,
        user_id: uuid.UUID,
        session_id: str | None = None,
        recent_messages: list[dict] | None = None,
    ) -> MemoryContext:
        """构建完整的记忆上下文（四级合并）。

        Args:
            user_id: 用户 ID
            session_id: 会话 ID（为 None 则跳过 Checkpoint）
            recent_messages: 最近消息列表（L1 短期窗口）

        Returns:
            MemoryContext 对象，包含所有层级的记忆
        """
        ctx = MemoryContext()

        # L1: 短期窗口 — 取最近 N 条消息
        if recent_messages:
            ctx.short_term = recent_messages[-SHORT_TERM_WINDOW_SIZE:]

        # L2: Checkpoint — 恢复会话状态
        if session_id:
            try:
                ctx.checkpoint = await self.checkpoint.load_checkpoint(session_id)
            except Exception as e:
                logger.warning("checkpoint_load_failed", session_id=session_id, error=str(e))

        # L3: Mem0 长期偏好 — 获取用户偏好和历史摘要
        try:
            ctx.user_facts = await self.mem0.search_facts(
                user_id=user_id,
                limit=10,
            )
        except Exception as e:
            logger.warning("mem0_search_failed", user_id=str(user_id), error=str(e))

        # L4: 工作记忆 — 获取当前任务相关事实
        try:
            ctx.working_memory = await self.mem0.search_facts(
                user_id=user_id,
                category="working",
                limit=5,
            )
        except Exception as e:
            logger.warning("working_memory_load_failed", error=str(e))

        logger.info(
            "memory_context_built",
            user_id=str(user_id),
            short_term_count=len(ctx.short_term),
            has_checkpoint=ctx.checkpoint is not None,
            user_facts_count=len(ctx.user_facts),
            working_memory_count=len(ctx.working_memory),
        )
        return ctx

    async def save_session(
        self,
        user_id: uuid.UUID,
        session_id: str,
        agent_state: dict,
        summary: str | None = None,
    ) -> None:
        """对话结束后保存记忆。

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            agent_state: Agent Loop 的最终状态
            summary: 对话摘要（可选，保存到 Mem0）
        """
        # L2: 保存 Checkpoint
        try:
            iteration = agent_state.get("iteration", 0)
            await self.checkpoint.save_checkpoint(session_id, agent_state, iteration)
        except Exception as e:
            logger.error("checkpoint_save_failed", session_id=session_id, error=str(e))

        # L3: 保存对话摘要到 Mem0（跨会话记忆）
        if summary:
            try:
                await self.mem0.add_fact(
                    user_id=user_id,
                    fact_text=summary,
                    category="summary",
                    ttl_hours=168,  # 7 天过期
                )
            except Exception as e:
                logger.error("summary_save_failed", error=str(e))

        logger.info("session_memory_saved", session_id=session_id, user_id=str(user_id))

    async def extract_and_save_facts(
        self,
        user_id: uuid.UUID,
        messages: list[dict],
    ) -> list[str]:
        """从对话中提取值得记住的事实（简化版）。

        生产环境应使用 LLM 提取，这里实现关键词启发式提取。
        """
        extracted = []

        for msg in messages:
            content = msg.get("content", "").lower()

            # 偏好检测：包含"我喜欢"/"请用"/"偏好"等
            for keyword in ["我喜欢", "我偏好", "请用", "请使用", "我希望"]:
                if keyword in content:
                    fact = content[content.index(keyword):content.index(keyword) + 100]
                    await self.mem0.add_fact(
                        user_id=user_id,
                        fact_text=fact,
                        category="preference",
                    )
                    extracted.append(fact)
                    break

        if extracted:
            logger.info("facts_extracted", user_id=str(user_id), count=len(extracted))

        return extracted

    async def update_working_memory(
        self,
        user_id: uuid.UUID,
        key: str,
        value: str,
        description: str | None = None,
    ) -> None:
        """更新工作记忆（当前任务相关的事实）。

        例如：用户正在处理报销单 BG2024001，记录下来供后续对话使用。
        """
        await self.mem0.add_fact(
            user_id=user_id,
            fact_text=description or f"{key}: {value}",
            category="working",
            fact_key=key,
            fact_value=value,
            ttl_hours=24,  # 工作记忆 24h 过期
        )

    async def clear_working_memory(self, user_id: uuid.UUID) -> int:
        """清除用户的工作记忆（任务完成后调用）。"""
        facts = await self.mem0.search_facts(
            user_id=user_id,
            category="working",
            limit=100,
        )
        count = 0
        for fact in facts:
            await self.mem0.deactivate_fact(fact.id)
            count += 1
        logger.info("working_memory_cleared", user_id=str(user_id), count=count)
        return count
