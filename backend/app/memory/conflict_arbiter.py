"""
写入时增量冲突整合 — 让语义矛盾的旧记忆退场（机制二）。

问题：向量库里的 PREFERENCE 和 EPISODIC 缺乏实体图谱的状态覆写机制，
新写入的记忆不会覆写旧的，系统也不会自动让语义矛盾的旧记忆失效 —
新旧状态在上下文里平权共存，Agent 最后靠概率猜一个答案。

核心认知：冲突是局部的。语义冲突只可能发生在高度相似的记忆之间
（"喜欢VIP权益"与"降级为基础版"冲突，因为都在说套餐取向；它与
"数据库连接池配置"永远不会冲突）。因此用检索找 Top-K 候选，
不做全库两两扫描（1000 条记忆两两比对是 50 万对，99.9% 无效）。

时机：不是离线打扫，是写入时的内联步骤 — 每写一条软记忆，先跑
冲突检测，整理和写入在同一个步骤里完成。

裁决分层（成本控制：大多数写入不触发 LLM）：
    sim >= duplicate_threshold          → 规则短路：语义等价，新记忆丢弃
    conflict_floor <= sim < duplicate   → LLM 裁决区间
    sim < conflict_floor                → 语义无关，正常写入

LLM 裁决三种结果：
    conflict   → 新胜旧退场（覆写）：旧记忆标记 superseded
    equivalent → 旧胜新丢弃（去重）：新记忆不落盘
    unrelated  → 语义互补，都保留

降级策略（fail-open）：LLM 不可用或解析失败时按 unrelated 处理，
不阻塞写入主流程。
"""

import json
import uuid
from dataclasses import dataclass, field

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 裁决动作
ACTION_WRITE = "write"      # 新记忆落盘（可携带需退场的旧记忆 ID）
ACTION_DISCARD = "discard"  # 新记忆丢弃（语义等价，无增量信息）


@dataclass
class ConsolidateVerdict:
    """一次增量整合的裁决结果。"""

    action: str
    superseded_ids: list[uuid.UUID] = field(default_factory=list)
    reason: str = ""


class MemoryConflictArbiter:
    """写入时增量冲突整合器 — 检索 Top-K 相似旧记忆，LLM 裁决。"""

    def __init__(self, mem0, llm_factory=None):
        self._mem0 = mem0
        if llm_factory is None:
            from app.llm.factory import get_llm_provider

            llm_factory = get_llm_provider
        self._llm_factory = llm_factory

    async def consolidate(
        self,
        user_id: uuid.UUID,
        fact_text: str,
        category: str,
    ) -> ConsolidateVerdict:
        """写入一条软记忆前的增量整合。

        Args:
            user_id: 用户 ID。
            fact_text: 待写入的新记忆文本。
            category: 新记忆类别。

        Returns:
            ConsolidateVerdict — 调用方按 action 决定写不写；
            superseded_ids 在新记忆落盘后由 mark_superseded 回填。
        """
        settings = get_settings()
        if not settings.MEMORY_CONSOLIDATION_ENABLED:
            return ConsolidateVerdict(ACTION_WRITE, reason="consolidation_disabled")

        candidates = await self._mem0.search_similar_with_scores(
            user_id=user_id,
            fact_text=fact_text,
            limit=settings.MEMORY_CONSOLIDATION_TOP_K,
            similarity_threshold=settings.MEMORY_CONSOLIDATION_CONFLICT_FLOOR,
        )
        if not candidates:
            return ConsolidateVerdict(ACTION_WRITE, reason="no_similar_candidates")

        # 规则短路：极高相似 → 语义等价，新记忆无增量信息，直接丢弃（省 LLM）
        if candidates[0][1] >= settings.MEMORY_CONSOLIDATION_DUPLICATE_THRESHOLD:
            logger.debug(
                "consolidate_equivalent_shortcircuit",
                new_fact=fact_text[:50],
                similarity=candidates[0][1],
            )
            return ConsolidateVerdict(
                ACTION_DISCARD, reason="equivalent_shortcircuit"
            )

        if not settings.MEMORY_CONSOLIDATION_LLM_ENABLED:
            return ConsolidateVerdict(ACTION_WRITE, reason="llm_arbitration_disabled")

        verdicts = await self._llm_arbitrate(fact_text, category, candidates)
        if not verdicts:
            # fail-open：LLM 失败不阻塞写入，也不断言冲突
            return ConsolidateVerdict(ACTION_WRITE, reason="llm_unavailable")

        # 等价优先于冲突：新记忆无增量信息时不落盘（保守，避免引入新矛盾）
        if "equivalent" in verdicts.values():
            return ConsolidateVerdict(
                ACTION_DISCARD,
                reason="llm_equivalent",
            )

        superseded_ids = [
            fid for fid, verdict in verdicts.items() if verdict == "conflict"
        ]
        if superseded_ids:
            logger.info(
                "consolidate_conflicts_found",
                user_id=str(user_id),
                new_fact=fact_text[:50],
                superseded_count=len(superseded_ids),
            )
            return ConsolidateVerdict(
                ACTION_WRITE,
                superseded_ids=superseded_ids,
                reason="llm_conflict",
            )

        return ConsolidateVerdict(ACTION_WRITE, reason="llm_unrelated")

    async def _llm_arbitrate(
        self,
        new_text: str,
        new_category: str,
        candidates: list,
    ) -> dict[uuid.UUID, str]:
        """LLM 裁决新记忆与每条候选旧记忆的关系。

        Returns:
            {fact_id: "conflict" | "equivalent" | "unrelated"}；
            LLM 不可用或解析失败时返回 {}（调用方 fail-open）。
        """
        prompt = self._build_prompt(new_text, new_category, candidates)
        try:
            llm = self._llm_factory()
            chunks: list[str] = []
            async for chunk in llm.chat(
                [{"role": "user", "content": prompt}],
                stream=True,
                max_tokens=300,
            ):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            response = "".join(chunks).strip()
        except Exception as e:
            logger.warning("conflict_arbitration_llm_failed", error=str(e))
            return {}

        parsed = self._parse_verdicts(response, candidates)
        if parsed is None:
            logger.warning(
                "conflict_arbitration_parse_failed", response=response[:200]
            )
            return {}
        return parsed

    def _build_prompt(self, new_text: str, new_category: str, candidates: list) -> str:
        """构建裁决 prompt：候选编号 → JSON 判定。"""
        lines = []
        for idx, (fact, sim) in enumerate(candidates, start=1):
            lines.append(f"{idx}. [{fact.category}] {fact.fact_text}")
        old_list = "\n".join(lines)
        return (
            "判断新记忆与每条旧记忆的语义关系。\n"
            "判定选项：\n"
            "- conflict：新旧语义矛盾，新信息取代旧信息（如：旧'喜欢VIP权益' vs 新'降级为基础版'）\n"
            "- equivalent：语义等价，新记忆无增量信息\n"
            "- unrelated：语义无关或互补，可共存\n\n"
            f"旧记忆列表：\n{old_list}\n\n"
            f"新记忆：[{new_category}] {new_text}\n\n"
            '输出格式：JSON 对象，key 为编号，value 为判定。例如 {"1": "conflict", "2": "unrelated"}\n'
            "只输出 JSON，不要额外解释。"
        )

    def _parse_verdicts(
        self, response: str, candidates: list
    ) -> dict[uuid.UUID, str] | None:
        """解析 LLM 裁决 JSON（容错 markdown 代码块包裹）。"""
        text = response.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None

        verdicts: dict[uuid.UUID, str] = {}
        for idx, (fact, _sim) in enumerate(candidates, start=1):
            value = data.get(str(idx))
            if value in ("conflict", "equivalent", "unrelated"):
                verdicts[fact.id] = value
        return verdicts
