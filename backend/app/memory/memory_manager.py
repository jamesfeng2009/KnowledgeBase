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

from app.llm.factory import get_llm_provider
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

# P1-1: LLM 事实提取的最低重要性阈值（1-5），低于此值不入库
_MIN_IMPORTANCE = 3


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
        query: str | None = None,
    ) -> MemoryContext:
        """构建完整的记忆上下文（四级合并）。

        Args:
            user_id: 用户 ID
            session_id: 会话 ID（为 None 则跳过 Checkpoint）
            recent_messages: 最近消息列表（L1 短期窗口）
            query: 当前用户查询（传入后 L3 长期记忆使用语义检索，
                而非简单的时间排序平铺）

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

        # L3: Mem0 长期偏好 — 有 query 时做语义检索，无 query 时按时间排序
        try:
            ctx.user_facts = await self.mem0.search_facts(
                user_id=user_id,
                query=query,
                limit=10,
            )
        except Exception as e:
            logger.warning("mem0_search_failed", user_id=str(user_id), error=str(e))

        # L4: 工作记忆 — 获取当前任务相关事实（有 query 时也做语义检索）
        try:
            ctx.working_memory = await self.mem0.search_facts(
                user_id=user_id,
                query=query,
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
            has_query=query is not None,
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

    async def set_preference(
        self,
        user_id: uuid.UUID,
        key: str,
        value: str,
        fact_text: str | None = None,
    ):
        """设置用户偏好，并将变更同步写入 Graphiti 时序图谱。

        编排逻辑：
            1. 读取旧值（Mem0 当前事实）
            2. 写入新值（Mem0，内置冲突检测自动停用旧偏好）
            3. 变更事件写入 Graphiti 时间线（"什么时候变成了什么"，
               供偏好漂移分析回溯；Graphiti 失败不影响主流程）

        Args:
            user_id: 用户 ID
            key: 偏好键（如 "answer_style"）
            value: 新偏好值
            fact_text: 可选的自然语言描述（默认 "{key}: {value}"）

        Returns:
            新创建的 MemoryFact。
        """
        old_value = await self.mem0.get_preference(user_id, key)
        fact = await self.mem0.set_preference(
            user_id=user_id, key=key, value=value, fact_text=fact_text
        )

        # 同步到 Graphiti 时序图谱（best-effort，失败不阻断主流程）
        try:
            await self.graphiti.record_preference_change(
                user_id=user_id,
                key=key,
                old_value=old_value,
                new_value=value,
            )
        except Exception as e:
            logger.error(
                "preference_graphiti_sync_failed",
                user_id=str(user_id),
                key=key,
                error=str(e),
            )

        return fact

    async def extract_and_save_key_decisions(
        self,
        user_id: uuid.UUID,
        query: str,
        answer: str,
    ) -> str | None:
        """从本轮对话中提取关键决策，持久化到 working memory。

        防中间遗忘（lost in the middle）：关键决策不依赖模型从聊天记录中
        "找回"，而是显式维护在 working memory 的状态对象中。下一轮对话
        开始时，build_context 会自动注入到 prompt 的"当前任务上下文"段落。

        触发条件（启发式）：
            - 答案中包含确认性关键词（"确认"/"已选择"/"已设定"/"金额"/"日期"）
            - 或用户查询中包含决策性关键词（"选择"/"决定"/"确认"/"设定"）

        Args:
            user_id: 用户 ID
            query: 用户查询
            answer: AI 回答

        Returns:
            保存的关键决策文本（未提取到则返回 None）
        """
        # 启发式判断是否包含关键决策
        decision_keywords = [
            "确认", "已选择", "已设定", "已配置", "金额", "日期",
            "选择", "决定", "设定", "审批通过", "已批准",
        ]
        has_decision = any(kw in query or kw in answer for kw in decision_keywords)
        if not has_decision:
            return None

        # 用 LLM 提取关键决策
        try:
            llm = get_llm_provider()
        except Exception:
            return None

        prompt = (
            "从以下对话中提取关键决策或已确认的参数。\n"
            "只提取明确确认的信息（如选择的方案、确认的金额、设定的日期），不要推测。\n"
            "输出格式：一行简洁的决策描述（不超过100字）。\n"
            "如果没有明确决策，输出 NONE。\n\n"
            f"用户：{query[:200]}\n"
            f"助手：{answer[:300]}\n\n"
            "关键决策："
        )

        try:
            chunks: list[str] = []
            async for chunk in llm.chat(
                [{"role": "user", "content": prompt}],
                stream=True,
                max_tokens=100,
            ):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            decision = "".join(chunks).strip()

            if not decision or decision.upper() == "NONE":
                return None

            # 持久化到 working memory（24h 过期）
            await self.mem0.add_fact(
                user_id=user_id,
                fact_text=f"关键决策：{decision}",
                category="working",
                ttl_hours=24,
            )
            logger.info(
                "key_decision_saved",
                user_id=str(user_id),
                decision=decision[:80],
            )
            return decision
        except Exception as exc:
            logger.warning("key_decision_extraction_failed", error=str(exc))
            return None

    async def extract_and_save_facts(
        self,
        user_id: uuid.UUID,
        messages: list[dict],
    ) -> list[str]:
        """从对话中提取值得记住的事实。

        P3-F: 优先使用 LLM 提取（更准确），失败时降级为关键词启发式。
        LLM 提取通过配置项 LLM_FACT_EXTRACTION_ENABLED 控制。
        """
        # P3-F: 尝试 LLM 提取
        try:
            from app.config import get_settings

            _settings = get_settings()
            if _settings.LLM_FACT_EXTRACTION_ENABLED:
                return await self._llm_extract_facts(user_id, messages)
        except Exception as exc:
            logger.warning("llm_fact_extraction_skipped", error=str(exc))

        # 降级：关键词启发式提取
        return await self._keyword_extract_facts(user_id, messages)

    async def _llm_extract_facts(
        self,
        user_id: uuid.UUID,
        messages: list[dict],
    ) -> list[str]:
        """P3-F: LLM 驱动的事实提取。

        增加重要性评分：LLM 输出格式包含 importance (1-5)，
        仅保留 importance >= _MIN_IMPORTANCE 的事实，避免低价值噪声入库。
        增加去重：写入前检查是否已有语义相似的活跃事实。
        """
        # 构建对话文本（最近 10 条消息）
        conversation = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')[:200]}"
            for m in messages[-10:]
        )
        if len(conversation) < 50:
            return []  # 对话太短不提取

        prompt = (
            "分析以下对话，提取值得长期记住的用户偏好和事实。\n"
            "只提取明确的偏好和事实，不要推测。\n"
            "输出格式：每行一个事实，格式为 category|importance|content\n"
            "category 可选：preference（用户偏好）/ fact（事实信息）\n"
            "importance 为 1-5 的整数（5=非常重要，1=可有可无）\n"
            "如果没有值得提取的内容，输出 NONE。\n\n"
            f"对话内容：\n{conversation}\n\n"
            "提取结果："
        )

        try:
            llm = get_llm_provider()
            messages_for_llm: list = [{"role": "user", "content": prompt}]
            chunks: list[str] = []
            async for chunk in llm.chat(messages_for_llm, stream=True, max_tokens=300):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            result_text = "".join(chunks).strip()

            if not result_text or result_text.upper() == "NONE":
                return []

            extracted: list[str] = []
            for line in result_text.split("\n"):
                line = line.strip()
                if "|" not in line:
                    continue

                parts = line.split("|", 2)
                # 兼容旧格式 category|content（无 importance 字段）
                if len(parts) == 2:
                    category, content = parts
                    importance = 3  # 默认中等重要性
                elif len(parts) == 3:
                    category, importance_str, content = parts
                    try:
                        importance = int(importance_str.strip())
                    except ValueError:
                        importance = 3
                else:
                    continue

                category = category.strip().lower()
                content = content.strip()

                # 重要性过滤：低于阈值的不入库
                if importance < _MIN_IMPORTANCE:
                    logger.debug(
                        "fact_skipped_low_importance",
                        content=content[:50],
                        importance=importance,
                    )
                    continue

                if category in ("preference", "fact") and content:
                    # 去重：检查是否已有语义相似的活跃事实
                    is_dup = await self._check_duplicate(user_id, content, category)
                    if is_dup:
                        logger.debug("fact_skipped_duplicate", content=content[:50])
                        continue

                    await self.mem0.add_fact(
                        user_id=user_id,
                        fact_text=content,
                        category=category,
                    )
                    extracted.append(content)

            if extracted:
                logger.info(
                    "facts_extracted_llm",
                    user_id=str(user_id),
                    count=len(extracted),
                )

            return extracted

        except Exception as exc:
            logger.warning("fact_extraction_llm_failed", error=str(exc))
            # 降级为关键词启发式
            return await self._keyword_extract_facts(user_id, messages)

    async def _check_duplicate(
        self,
        user_id: uuid.UUID,
        content: str,
        category: str,
        similarity_threshold: float = 0.85,
    ) -> bool:
        """检查是否已有语义相似的活跃事实（去重）。

        使用 Mem0 语义检索：如果已有事实与新内容相似度 >= threshold，
        则视为重复。

        Args:
            user_id: 用户 ID
            content: 新事实内容
            category: 事实类别
            similarity_threshold: 语义相似度阈值（高于此值视为重复）

        Returns:
            True 表示存在重复，False 表示无重复。
        """
        try:
            existing = await self.mem0.search_facts(
                user_id=user_id,
                query=content,
                category=category,
                limit=3,
                similarity_threshold=similarity_threshold,
            )
            return len(existing) > 0
        except Exception as exc:
            logger.warning("dedup_check_failed", error=str(exc))
            return False  # 检查失败时不过滤，避免漏记

    async def _keyword_extract_facts(
        self,
        user_id: uuid.UUID,
        messages: list[dict],
    ) -> list[str]:
        """关键词启发式事实提取（降级策略）。"""
        extracted: list[str] = []

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
