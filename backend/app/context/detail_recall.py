"""
对话历史细节召回 — 近 K 轮原文直传，K 轮之前走检索召回。

对应附件第 12/13 讲核心"摘要后细节找回"：当滚动摘要压缩旧消息后，旧细节
会丢失，仅凭摘要无法回答用户对旧细节的追问。本模块把压缩掉的旧消息落库
（category="detail"，向量化），供后续按当前查询召回，形成
"整体摘要 + 章节摘要 + 关键事实 + 细节召回"的闭环。

设计要点：
    - :meth:`persist` 把压缩掉的旧消息段落写入记忆（向量化），供跨轮召回；
    - :meth:`recall` 按当前查询从已落库的旧消息中召回最相关的细节片段，
      注入到 memory_context，实现"压缩后按需找回细节"；
    - 优雅降级：LLM / 记忆不可用时返回空，不阻断主流程。

使用方式（在 chat_service 构建 memory_context 时接入）：:

    recall = DetailRecall(mem0)
    # 压缩发生后：把旧消息段落落库
    await recall.persist(user_id, old_messages)
    # 每次提问：召回相关旧细节
    details = await recall.recall(user_id, query, limit=3)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.memory.mem0_manager import Mem0Manager
from app.utils.logger import get_logger

log = get_logger(__name__)

# 一次持久化合并的旧消息最大条数（避免单条记忆过长）
_PERSIST_MAX_MSGS: int = 6
# 单条旧消息截断字符数
_MSG_MAX_CHARS: int = 200
# 召回结果上限
_DEFAULT_RECALL_LIMIT: int = 3
# 细节记忆 TTL（小时）— 比整段摘要短，细节易过期
_DETAIL_TTL_HOURS: int = 72


@dataclass(frozen=True)
class RecalledDetail:
    """召回的细节片段。

    Attributes:
        content: 细节文本（可直接注入 memory_context）。
        similarity: 与查询的相似度/相关分数。
        source: 来源标识（原消息轮次或类别）。
    """

    content: str
    similarity: float = 0.0
    source: str = "detail"


class DetailRecall:
    """对话历史细节召回器 — 压缩后按需找回旧细节。

    使用方式::

        recall = DetailRecall(mem0)
        await recall.persist(user_id, old_messages)
        details = await recall.recall(user_id, query)
    """

    def __init__(
        self,
        mem0: Mem0Manager | None = None,
        limit: int = _DEFAULT_RECALL_LIMIT,
    ) -> None:
        """初始化细节召回器。

        Args:
            mem0: Mem0Manager 实例，为 None 时懒加载。
            limit: 默认召回条数上限。
        """
        self._mem0 = mem0
        self._limit = limit

    async def persist(
        self,
        user_id: UUID,
        old_messages: list[dict[str, str]],
    ) -> bool:
        """把压缩掉的旧消息段落落库（向量化），供后续按需召回。

        Args:
            user_id: 用户 ID。
            old_messages: 被摘要压缩掉的旧消息（按时间正序）。

        Returns:
            True 表示成功落库；False 表示无内容或失败。
        """
        if not old_messages:
            return False

        mem0 = await self._get_mem0()
        if mem0 is None:
            return False

        # 合并为一段紧凑文本（保留角色标记，便于还原语义）
        lines = [
            f"[{'用户' if m.get('role') == 'user' else '助手'}] "
            f"{(m.get('content') or '')[:_MSG_MAX_CHARS]}"
            for m in old_messages[-_PERSIST_MAX_MSGS:]
        ]
        if not any(lines):
            return False
        detail_text = "\n".join(lines)

        try:
            await mem0.add_fact(
                user_id=user_id,
                fact_text=detail_text,
                category="detail",
                ttl_hours=_DETAIL_TTL_HOURS,
            )
            log.info(
                "detail_recall.persisted",
                user_id=str(user_id),
                messages=len(lines),
                chars=len(detail_text),
            )
            return True
        except Exception as exc:
            log.warning("detail_recall.persist_failed", error=str(exc))
            return False

    async def recall(
        self,
        user_id: UUID,
        query: str,
        limit: int | None = None,
    ) -> list[RecalledDetail]:
        """按当前查询从已落库的旧消息中召回相关细节。

        Args:
            user_id: 用户 ID。
            query: 当前用户查询（消解后）。
            limit: 召回条数上限（默认用构造时 limit）。

        Returns:
            召回的细节片段列表（按相关度降序）。
        """
        if not query:
            return []

        mem0 = await self._get_mem0()
        if mem0 is None:
            return []

        top_k = limit if limit is not None else self._limit
        try:
            facts = await mem0.search_facts(
                user_id=user_id,
                query=query,
                category="detail",
                limit=top_k,
            )
            results: list[RecalledDetail] = []
            for f in facts:
                text = f.get("fact_text", "") if isinstance(f, dict) else getattr(f, "fact_text", "")
                if not text:
                    continue
                results.append(
                    RecalledDetail(
                        content=text,
                        similarity=float(f.get("similarity", 0.0)) if isinstance(f, dict) else 0.0,
                    )
                )

            if results:
                log.info(
                    "detail_recall.recalled",
                    user_id=str(user_id),
                    query_len=len(query),
                    count=len(results),
                )
            return results[:top_k]
        except Exception as exc:
            log.warning("detail_recall.recall_failed", error=str(exc))
            return []

    async def _get_mem0(self) -> Mem0Manager | None:
        """懒加载 Mem0Manager。"""
        if self._mem0 is not None:
            return self._mem0
        try:
            from app.memory.mem0_manager import Mem0Manager
            from app.models.database import async_session_maker

            async with async_session_maker() as session:
                self._mem0 = Mem0Manager(session)
        except Exception as exc:
            log.debug("detail_recall.mem0_unavailable", error=str(exc))
        return self._mem0