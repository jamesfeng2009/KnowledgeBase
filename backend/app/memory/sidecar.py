"""副车道检索器 (Sidecar Memory Retriever) — P2-1。

核心价值：记忆召回走独立"副车道"，不污染主对话 Prompt Cache 前缀。

背景：
- 主对话（think 循环）用稳定前缀（``_THINK_SYSTEM_STABLE``）命中 Anthropic KV Cache；
- 若把记忆召回过程（含 LLM 辅助步骤）混入主对话，易变内容会破坏前缀稳定性，
  导致缓存失效、成本上升；
- 副车道把记忆召回独立出来：用轻量模型完成 LLM 辅助步骤（记忆查询改写），
  结果以独立 ``memory_context`` 注入生成阶段，绝不进入稳定前缀。

设计：
- ``SidecarMemoryRetriever.retrieve()`` 在独立通道执行 L3/L4 记忆召回；
- LLM 辅助步骤（记忆查询改写）优先用轻量模型（``MEMORY_SIDECAR_MODEL``），
  未配置时回退默认 Provider；
- 通过 ``MEMORY_SIDECAR_ENABLED`` 开关控制，默认关闭，保证零行为回归。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.llm.factory import get_llm_provider, get_llm_provider_by_model
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 记忆查询改写 prompt — 把用户口语化问题改写为适合记忆语义检索的查询。
# 目标：抓住用户长期偏好 / 工作记忆中的关键实体与意图，供向量检索召回。
_MEMORY_QUERY_REWRITE_PROMPT: str = (
    "你是记忆检索助手。请把用户的提问改写为一段适合向量检索的查询，"
    "聚焦用户的长期偏好、习惯、身份与工作记忆中的关键信息。\n"
    "只输出改写后的查询本身，不要解释，不要加引号。\n"
    "用户提问：{query}"
)


class SidecarMemoryRetriever:
    """副车道记忆检索器 — 独立通道执行记忆召回，使用轻量模型。

    与主对话隔离：本类只负责记忆召回，不触碰主对话的稳定前缀。
    LLM 辅助步骤（记忆查询改写）优先使用轻量模型，降低召回成本。
    """

    def __init__(
        self,
        llm: Any | None = None,
        model_id: str | None = None,
    ) -> None:
        """初始化副车道检索器。

        Args:
            llm: 注入的 LLM Provider（测试用）；为 None 时按 model_id 解析。
            model_id: 轻量模型 ID（models.json 中的 model_id）；为 None 时
                读取配置 MEMORY_SIDECAR_MODEL，仍为空则回退默认 Provider。
        """
        self._llm = llm
        self._model_id = model_id

    def _resolve_llm(self) -> Any:
        """解析轻量模型 Provider；未配置时回退默认 Provider。"""
        if self._llm is not None:
            return self._llm
        model_id = self._model_id
        if model_id:
            try:
                return get_llm_provider_by_model(model_id)
            except Exception as exc:
                logger.warning(
                    "sidecar.llm_resolve_fallback",
                    model_id=model_id,
                    error=str(exc),
                )
        return get_llm_provider()

    async def refine_memory_query(self, query: str) -> str:
        """用轻量模型把用户查询改写为记忆检索查询（LLM 辅助步骤）。

        改写失败时回退原查询，保证记忆召回不因 LLM 故障而中断。
        """
        if not query:
            return query
        llm = self._resolve_llm()
        prompt = _MEMORY_QUERY_REWRITE_PROMPT.format(query=query)
        try:
            chunks: list[str] = []
            async for chunk in llm.chat(
                [{"role": "user", "content": prompt}],
                stream=True,
                max_tokens=64,
            ):
                if isinstance(chunk, dict) and chunk.get("type") == "usage":
                    continue
                if isinstance(chunk, str):
                    chunks.append(chunk)
                elif isinstance(chunk, dict) and chunk.get("content"):
                    chunks.append(str(chunk["content"]))
            refined = "".join(chunks).strip()
            return refined or query
        except Exception as exc:
            logger.warning("sidecar.refine_query_failed", error=str(exc))
            return query

    async def retrieve(
        self,
        user_id: uuid.UUID,
        query: str | None = None,
        mem0: Any | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """在副车道执行 L3/L4 记忆召回。

        Args:
            user_id: 用户 ID。
            query: 当前用户查询；非空时先经轻量模型改写再语义检索。
            mem0: Mem0Manager 实例（注入以便测试）；为 None 时由调用方
                在 MemoryManager 中传入实际实例。

        Returns:
            dict: ``{"user_facts": [...], "working_memory": [...]}``
                召回结果（dict 化事实），供合并进 MemoryContext。
        """
        if mem0 is None:
            raise ValueError("SidecarMemoryRetriever.retrieve 需要 mem0 实例")

        search_query = query
        if query:
            search_query = await self.refine_memory_query(query)

        user_facts: list[dict[str, Any]] = []
        working_memory: list[dict[str, Any]] = []
        try:
            user_facts = [
                _fact_to_dict(f)
                for f in await mem0.search_facts(
                    user_id=user_id,
                    query=search_query,
                    limit=10,
                )
            ]
        except Exception as exc:
            logger.warning("sidecar.mem0_search_failed", user_id=str(user_id), error=str(exc))

        try:
            working_memory = [
                _fact_to_dict(f)
                for f in await mem0.search_facts(
                    user_id=user_id,
                    query=search_query,
                    category="working",
                    limit=5,
                )
            ]
        except Exception as exc:
            logger.warning("sidecar.working_memory_failed", user_id=str(user_id), error=str(exc))

        logger.info(
            "sidecar.memory_retrieved",
            user_id=str(user_id),
            refined=search_query != query,
            user_facts_count=len(user_facts),
            working_memory_count=len(working_memory),
        )
        return {"user_facts": user_facts, "working_memory": working_memory}


def _fact_to_dict(fact: Any) -> dict[str, Any]:
    """把 MemoryFact ORM 对象转为 dict（与 memory_manager._fact_to_dict 对齐）。"""
    if isinstance(fact, dict):
        return fact
    return {
        "id": str(getattr(fact, "id", "")),
        "category": getattr(fact, "category", ""),
        "fact_text": getattr(fact, "fact_text", ""),
        "fact_key": getattr(fact, "fact_key", ""),
        "fact_value": getattr(fact, "fact_value", ""),
    }
