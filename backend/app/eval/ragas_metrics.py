"""
RAGAS 兼容评测指标 — 自实现 RAGAS 四项标准指标，不依赖外部库。

RAGAS (Retrieval Augmented Generation Assessment) 是 RAG 评测的事实标准框架。
本模块遵循 RAGAS 的指标定义，使用项目自有的 LLMProvider 计算四项标准指标:

1. Faithfulness — 答案是否忠实于检索上下文（不含编造内容）
2. Answer Relevancy — 答案是否切题回答了用户问题
3. Context Precision — 检索上下文中相关文档的排名精度
4. Context Recall — 检索上下文是否覆盖了期望答案所需信息

每项指标取值 0.0 ~ 1.0，1.0 为最优。

使用方式::

    from app.eval.ragas_metrics import RagasMetrics

    metrics = RagasMetrics(llm_provider)
    result = await metrics.evaluate(
        query="公司报销流程是什么",
        answer="报销流程：1. 填写报销单...",
        contexts=["公司报销流程文档内容..."],
        expected_answer="报销流程：1. 填写报销单 2. 审批...",
    )
    # result = {"faithfulness": 0.85, "answer_relevancy": 0.90,
    #           "context_precision": 0.80, "context_recall": 0.75}
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# 默认 K 值
_DEFAULT_K = 5


class RagasMetrics:
    """RAGAS 兼容评测指标计算器。

    使用 LLMProvider 作为 Judge 计算四项标准指标。
    当 LLM 不可用时降级为基于关键词的启发式评分。

    Args:
        llm: LLMProvider 实例，用于 LLM-as-Judge 评分。
    """

    def __init__(self, llm: Any | None = None) -> None:
        self.llm = llm
        self._llm_available = llm is not None

    async def evaluate(
        self,
        query: str,
        answer: str,
        contexts: list[str],
        expected_answer: str | None = None,
    ) -> dict[str, float]:
        """计算 RAGAS 四项标准指标。

        Args:
            query: 用户问题。
            answer: RAG 系统生成的答案。
            contexts: 检索到的上下文文档列表。
            expected_answer: 期望答案（可选，用于 context_recall）。

        Returns:
            包含四项指标的字典，取值 0.0 ~ 1.0。
        """
        results: dict[str, float] = {}

        try:
            results["faithfulness"] = await self._faithfulness(answer, contexts)
        except Exception as exc:
            log.warning("ragas.faithfulness_error", error=str(exc))
            results["faithfulness"] = 0.0

        try:
            results["answer_relevancy"] = await self._answer_relevancy(query, answer)
        except Exception as exc:
            log.warning("ragas.answer_relevancy_error", error=str(exc))
            results["answer_relevancy"] = 0.0

        try:
            results["context_precision"] = self._context_precision(
                query, contexts, expected_answer
            )
        except Exception as exc:
            log.warning("ragas.context_precision_error", error=str(exc))
            results["context_precision"] = 0.0

        try:
            results["context_recall"] = await self._context_recall(
                expected_answer or "", contexts
            )
        except Exception as exc:
            log.warning("ragas.context_recall_error", error=str(exc))
            results["context_recall"] = 0.0

        return results

    # ==================================================================
    # 1. Faithfulness — 答案忠实度
    # ==================================================================

    async def _faithfulness(self, answer: str, contexts: list[str]) -> float:
        """计算答案忠实度。

        RAGAS 定义：答案中每个陈述是否能从检索上下文中找到支持。
        评分 = 可支持的陈述数 / 总陈述数。

        当 LLM 不可用时，降级为基于关键词重叠的启发式评分。
        """
        if not answer.strip():
            return 0.0

        context_text = "\n".join(contexts)
        if not context_text.strip():
            return 0.0

        if self._llm_available:
            return await self._llm_faithfulness(answer, context_text)
        return self._heuristic_faithfulness(answer, context_text)

    async def _llm_faithfulness(self, answer: str, context_text: str) -> float:
        """使用 LLM 评估答案忠实度。"""
        prompt = f"""请评估以下答案对检索上下文的忠实度。

检索上下文:
{context_text[:2000]}

答案:
{answer[:1000]}

请判断答案中的每个陈述是否可以从检索上下文中找到支持。
返回 JSON 格式: {{"score": 0.0-1.0, "supported_claims": 数量, "total_claims": 数量, "reason": "简要说明"}}
score 为 1.0 表示完全忠实，0.0 表示完全编造。"""

        messages = [{"role": "user", "content": prompt}]
        response = await self._get_llm_response(messages)
        return self._parse_score(response)

    def _heuristic_faithfulness(self, answer: str, context_text: str) -> float:
        """启发式忠实度评分（LLM 不可用时降级）。

        基于答案中句子与上下文的词重叠比例。
        """
        answer_sentences = [s.strip() for s in answer.split("。") if s.strip()]
        if not answer_sentences:
            return 0.0

        context_words = set(context_text)
        supported = 0
        for sentence in answer_sentences:
            words = set(sentence)
            overlap = len(words & context_words)
            if overlap > len(words) * 0.5:
                supported += 1

        return supported / len(answer_sentences)

    # ==================================================================
    # 2. Answer Relevancy — 答案切题度
    # ==================================================================

    async def _answer_relevancy(self, query: str, answer: str) -> float:
        """计算答案切题度。

        RAGAS 定义：答案是否切题回答了用户问题。
        通过让 LLM 从答案反推问题，与原问题计算相似度。
        """
        if not answer.strip():
            return 0.0

        if self._llm_available:
            return await self._llm_answer_relevancy(query, answer)
        return self._heuristic_answer_relevancy(query, answer)

    async def _llm_answer_relevancy(self, query: str, answer: str) -> float:
        """使用 LLM 评估答案切题度。"""
        prompt = f"""请评估以下答案对用户问题的切题程度。

用户问题: {query}

答案: {answer[:1000]}

请判断答案是否直接回答了用户问题，是否包含无关内容。
返回 JSON 格式: {{"score": 0.0-1.0, "reason": "简要说明"}}
score 为 1.0 表示完全切题，0.0 表示完全跑题。"""

        messages = [{"role": "user", "content": prompt}]
        response = await self._get_llm_response(messages)
        return self._parse_score(response)

    def _heuristic_answer_relevancy(self, query: str, answer: str) -> float:
        """启发式切题度评分（LLM 不可用时降级）。

        基于问题和答案的词重叠比例。
        """
        query_words = set(query)
        answer_words = set(answer)
        if not query_words:
            return 0.0
        overlap = len(query_words & answer_words)
        return min(overlap / len(query_words), 1.0)

    # ==================================================================
    # 3. Context Precision — 上下文精度
    # ==================================================================

    def _context_precision(
        self,
        query: str,
        contexts: list[str],
        expected_answer: str | None,
    ) -> float:
        """计算上下文精度。

        RAGAS 定义：相关文档是否排在更前面。
        使用 DCG (Discounted Cumulative Gain) 方式计算。

        当有 expected_answer 时，通过关键词匹配判断每个上下文是否相关。
        无 expected_answer 时降级为均匀评分。
        """
        if not contexts:
            return 0.0

        if not expected_answer:
            # 无期望答案时，假设所有上下文同等相关
            return 1.0

        # 基于关键词匹配判断每个上下文的相关性
        expected_keywords = set(expected_answer)
        relevance_scores: list[float] = []
        for ctx in contexts:
            ctx_words = set(ctx)
            overlap = len(expected_keywords & ctx_words)
            # 归一化到 0-1
            score = min(overlap / max(len(expected_keywords), 1), 1.0)
            relevance_scores.append(score)

        # DCG@K
        import math
        dcg = sum(
            rel / math.log2(i + 2) for i, rel in enumerate(relevance_scores[:_DEFAULT_K])
        )
        # IDCG (理想 DCG = 降序排列后的 DCG)
        ideal = sorted(relevance_scores, reverse=True)
        idcg = sum(
            rel / math.log2(i + 2) for i, rel in enumerate(ideal[:_DEFAULT_K])
        )
        if idcg == 0:
            return 0.0
        return dcg / idcg

    # ==================================================================
    # 4. Context Recall — 上下文召回率
    # ==================================================================

    async def _context_recall(
        self, expected_answer: str, contexts: list[str]
    ) -> float:
        """计算上下文召回率。

        RAGAS 定义：检索上下文是否覆盖了期望答案中的信息。
        通过让 LLM 判断期望答案中的每个陈述是否在上下文中可找到。

        当 LLM 不可用时降级为关键词召回率。
        """
        if not expected_answer.strip():
            return 1.0  # 无期望答案时视为满分
        if not contexts:
            return 0.0

        context_text = "\n".join(contexts)

        if self._llm_available:
            return await self._llm_context_recall(expected_answer, context_text)
        return self._heuristic_context_recall(expected_answer, context_text)

    async def _llm_context_recall(
        self, expected_answer: str, context_text: str
    ) -> float:
        """使用 LLM 评估上下文召回率。"""
        prompt = f"""请评估检索上下文是否覆盖了期望答案中的信息。

检索上下文:
{context_text[:2000]}

期望答案:
{expected_answer[:1000]}

请判断期望答案中的每个关键信息点是否可以在检索上下文中找到。
返回 JSON 格式: {{"score": 0.0-1.0, "covered_points": 数量, "total_points": 数量}}
score 为 1.0 表示全部覆盖，0.0 表示完全未覆盖。"""

        messages = [{"role": "user", "content": prompt}]
        response = await self._get_llm_response(messages)
        return self._parse_score(response)

    def _heuristic_context_recall(
        self, expected_answer: str, context_text: str
    ) -> float:
        """启发式召回率评分（LLM 不可用时降级）。"""
        expected_sentences = [s.strip() for s in expected_answer.split("。") if s.strip()]
        if not expected_sentences:
            return 1.0

        context_words = set(context_text)
        covered = 0
        for sentence in expected_sentences:
            words = set(sentence)
            overlap = len(words & context_words)
            if overlap > len(words) * 0.5:
                covered += 1

        return covered / len(expected_sentences)

    # ==================================================================
    # 辅助方法
    # ==================================================================

    async def _get_llm_response(self, messages: list[dict]) -> str:
        """调用 LLM 获取响应文本。"""
        if not self.llm:
            return ""
        chunks: list[str] = []
        async for chunk in self.llm.chat(messages=messages, stream=True):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)

    @staticmethod
    def _parse_score(response: str) -> float:
        """从 LLM 响应中解析评分。

        支持:
        1. JSON 格式 {"score": 0.85}
        2. markdown 代码块包裹的 JSON
        3. 直接数字
        """
        if not response:
            return 0.0

        # 尝试从 markdown 代码块中提取
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
        if code_block:
            response = code_block.group(1)

        # 尝试 JSON 解析
        try:
            data = json.loads(response.strip())
            if isinstance(data, dict) and "score" in data:
                score = float(data["score"])
                return max(0.0, min(1.0, score))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # 尝试直接提取数字
        match = re.search(r"(\d+\.?\d*)", response)
        if match:
            score = float(match.group(1))
            if score > 1.0:
                score = score / 100.0  # 假设是百分制
            return max(0.0, min(1.0, score))

        return 0.0
