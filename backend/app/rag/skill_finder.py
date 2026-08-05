"""
Find Skills 匹配引擎 — 渐进式技能加载的意图匹配层。

设计灵感来自 Claude Agent Skills 的按需加载机制 [$TRAE_REF](https://cloud.tencent.com/developer/article/2611296)：
    - Step 1: 加载轻量技能索引（仅 name + category + tags + description）；
    - Step 2: 用用户查询匹配相关技能（关键词 + 语义匹配）；
    - Step 3: 按需加载匹配技能的完整 Tool schema；
    - Step 4: 未匹配时 fallback 到全量加载（零回归保证）。

匹配算法：
    1. 查询分词 → 逐技能计算 match_score（name +10 / category +5 / tag +8 / desc +3）；
    2. 按分数降序排列，取分数 ≥ ``SKILL_MATCH_THRESHOLD`` 的前 N 个；
    3. 匹配数为 0 时返回全部工具名（fallback）。

使用方式::

    from app.rag.skill_finder import SkillFinder

    finder = SkillFinder(registry)
    matched = finder.find_relevant_skills("搜索知识库中关于 Python 的文档")
    # matched = ["knowledge_search", "document_get"]

    tools = await registry.load_tools(matched)
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.rag.skill_registry import SkillRegistry
from app.utils.logger import get_logger

log = get_logger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度 — 零向量/维度不符返回 0.0（优雅降级）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SkillFinder:
    """Find Skills 匹配引擎 — 根据用户查询匹配相关技能。

    工作流程：
        1. 查询分词（中英文混合，去除停用词）；
        2. 逐技能计算匹配分数；
        3. 按分数排序，取 Top-N（分数 ≥ 阈值）；
        4. 匹配为空时 fallback 到全量加载。

    Attributes:
        registry: SkillRegistry 实例，提供技能索引和按需加载。
        match_threshold: 匹配阈值（默认 5），低于此分数的技能不加载。
        max_skills: 单次最多加载的技能数（默认 10），防止过多工具淹没 LLM。
    """

    # 中文停用词 — 匹配时忽略
    _STOP_WORDS_CN = frozenset({
        "的", "了", "在", "是", "我", "你", "他", "她", "它", "们",
        "和", "与", "或", "及", "等", "把", "被", "让", "给", "向",
        "这", "那", "些", "个", "中", "上", "下", "里", "外", "前",
        "后", "对", "于", "从", "到", "要", "会", "能", "可", "以",
        "有", "无", "不", "没", "都", "也", "还", "又", "再", "就",
        "一", "二", "三", "请", "帮", "帮我", "一下", "需要", "想要",
    })

    # 英文停用词
    _STOP_WORDS_EN = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "must", "can", "need", "want", "please",
        "help", "me", "i", "you", "he", "she", "it", "we", "they", "this",
        "that", "these", "those", "and", "or", "but", "not", "for", "with",
        "about", "to", "of", "in", "on", "at", "by", "from", "as", "into",
    })

    def __init__(
        self,
        registry: SkillRegistry,
        match_threshold: int = 5,
        max_skills: int = 10,
        vector_weight: float = 10.0,
        vector_sim_threshold: float = 0.4,
    ) -> None:
        """初始化 Find Skills 匹配引擎。

        Args:
            registry: SkillRegistry 实例。
            match_threshold: 匹配阈值，分数低于此值的技能不加载。
            max_skills: 单次最多返回的技能数。
            vector_weight: P0-1 向量通道权重 — 余弦相似度 × 此值折算为
                与关键词分数可比的加分（默认 10，对齐 name 命中权重）。
            vector_sim_threshold: P0-1 向量通道相似度阈值 — 低于此值
                不产生加分，防止弱相关技能被语义噪声召回。
        """
        self.registry = registry
        self.match_threshold = match_threshold
        self.max_skills = max_skills
        self.vector_weight = vector_weight
        self.vector_sim_threshold = vector_sim_threshold

    def _keyword_scores(self, query: str) -> list[tuple[int, str]] | None:
        """关键词通道打分 — 返回 ``[(score, name)]``；查询无效时返回 None。

        ``None`` 表示查询为空/无有效词，调用方应直接 fallback 全量。
        """
        if not query or not query.strip():
            return None
        query_terms = self._tokenize(query)
        if not query_terms:
            return None

        query_lower = query.lower()
        scored: list[tuple[int, str]] = []
        for name in self.registry.get_all_names():
            meta = self.registry.get_metadata(name)
            if meta is None:
                continue
            scored.append((meta.match_score(query, query_lower, query_terms), name))
        return scored

    def _finalize(self, scored: list[tuple[float, str]], query: str) -> list[str]:
        """排序 + 阈值过滤 + fallback（零回归语义，双通道共用）。"""
        matched_scores = [(s, n) for s, n in scored if s >= self.match_threshold]
        if not matched_scores:
            # Fallback: 无匹配时返回全部（零回归保证）
            all_names = self.registry.get_all_names()
            log.debug(
                "skill_finder.no_match_fallback",
                query=query[:80],
                total_skills=len(all_names),
            )
            return all_names

        matched_scores.sort(key=lambda x: x[0], reverse=True)
        matched = [name for _, name in matched_scores[: self.max_skills]]
        log.info(
            "skill_finder.matched",
            query=query[:80],
            matched=matched,
            scores=[s for s, _ in matched_scores[: self.max_skills]],
            total_indexed=len(self.registry.get_all_names()),
        )
        return matched

    def find_relevant_skills(self, query: str) -> list[str]:
        """根据用户查询匹配相关技能名称（关键词通道，同步）。

        匹配流程：
            1. 查询分词（中英文）；
            2. 逐技能计算 match_score；
            3. 按分数降序排列，取分数 ≥ 阈值的前 ``max_skills`` 个；
            4. 匹配为空时返回全部技能名（fallback 保证零回归）。

        Args:
            query: 用户查询字符串。

        Returns:
            匹配到的技能名称列表。匹配为空时返回全部技能名。
        """
        scored = self._keyword_scores(query)
        if scored is None:
            return self.registry.get_all_names()
        return self._finalize(scored, query)

    async def afind_relevant_skills(
        self, query: str, embedder: Any = None
    ) -> list[str]:
        """根据用户查询匹配相关技能名称（P0-1 向量 + 关键词融合，异步）。

        双通道融合：
            1. 关键词通道：与同步版一致的 match_score；
            2. 向量通道：query 嵌入与技能描述向量余弦相似度 ×
               ``vector_weight`` 折算加分（≥ ``vector_sim_threshold`` 才加分）；
            3. 融合分数 = 关键词分数 + 向量加分，语义命中可补齐关键词盲区
               （如"报销怎么走"命中"费用审批流程"）；
            4. embedder 不可用 / 向量未预计算时退化为纯关键词通道；
            5. 无命中时 fallback 返回全部（零回归语义不变）。

        Args:
            query: 用户查询字符串。
            embedder: EmbeddingProvider 实例；None 时仅关键词通道。

        Returns:
            匹配到的技能名称列表。匹配为空时返回全部技能名。
        """
        scored = self._keyword_scores(query)
        if scored is None:
            return self.registry.get_all_names()

        embeddings = self.registry.get_all_embeddings()
        if embedder is None or not embeddings:
            return self._finalize(scored, query)

        try:
            query_vecs = await embedder.embed([query])
            query_vec = query_vecs[0] if query_vecs else None
        except Exception as exc:
            # 优雅降级：向量通道失败不影响关键词结果
            log.warning("skill_finder.vector_channel_failed", error=str(exc))
            return self._finalize(scored, query)

        if not query_vec:
            return self._finalize(scored, query)

        fused: list[tuple[float, str]] = []
        for kw_score, name in scored:
            bonus = 0.0
            skill_vec = embeddings.get(name)
            if skill_vec is not None:
                sim = _cosine(query_vec, skill_vec)
                if sim >= self.vector_sim_threshold:
                    bonus = sim * self.vector_weight
            fused.append((kw_score + bonus, name))

        return self._finalize(fused, query)

    async def find_and_load(self, query: str):
        """匹配技能并加载完整 Tool schema — 一步到位。

        Args:
            query: 用户查询字符串。

        Returns:
            匹配并加载的 Tool 列表。
        """
        names = self.find_relevant_skills(query)
        return await self.registry.load_tools(names)

    def _tokenize(self, query: str) -> list[str]:
        """查询分词 — 支持中英文混合。

        分词策略：
            1. 英文：按空格/标点分割，转小写，去停用词；
            2. 中文：按 2-4 字符滑窗提取候选词（轻量分词，不依赖 jieba）。

        Args:
            query: 原始查询字符串。

        Returns:
            分词后的词列表（已去停用词）。
        """
        terms: list[str] = []

        # 英文分词：提取连续的字母数字
        en_tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_]*", query)
        for token in en_tokens:
            token_lower = token.lower()
            if token_lower not in self._STOP_WORDS_EN and len(token_lower) >= 2:
                terms.append(token_lower)

        # 中文分词：2-4 字符滑窗（轻量方案，不引入 jieba 依赖）
        cn_chars = re.findall(r"[\u4e00-\u9fff]", query)
        if cn_chars:
            cn_text = "".join(cn_chars)
            # 2-gram 和 3-gram
            for n in (2, 3):
                for i in range(len(cn_text) - n + 1):
                    ngram = cn_text[i : i + n]
                    if ngram not in self._STOP_WORDS_CN:
                        terms.append(ngram)
            # 单字（去停用词）
            for char in cn_text:
                if char not in self._STOP_WORDS_CN and len(char) >= 1:
                    terms.append(char)

        return terms

    def get_match_report(self, query: str) -> dict:
        """生成匹配报告 — 调试用，展示每个技能的匹配分数。

        Args:
            query: 用户查询字符串。

        Returns:
            匹配报告字典，含查询分词、各技能分数、最终匹配结果。
        """
        query_terms = self._tokenize(query)
        query_lower = query.lower()

        scores: list[dict] = []
        for name in self.registry.get_all_names():
            meta = self.registry.get_metadata(name)
            if meta is None:
                continue
            score = meta.match_score(query, query_lower, query_terms)
            scores.append({
                "name": name,
                "category": meta.category,
                "tags": meta.tags,
                "score": score,
                "matched": score >= self.match_threshold,
            })
        scores.sort(key=lambda x: x["score"], reverse=True)

        matched = [s["name"] for s in scores if s["matched"]][: self.max_skills]
        return {
            "query": query,
            "terms": query_terms,
            "threshold": self.match_threshold,
            "matched": matched if matched else self.registry.get_all_names(),
            "fallback": len(matched) == 0,
            "scores": scores,
        }
