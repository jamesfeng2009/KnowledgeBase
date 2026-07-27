"""
RAG 质量守卫 — 双层自适应评估闭环。

借鉴 CorrectiveRAG 思路，但不引入 RAGAS 全量评估（RAGAS 适合离线批量评测，
不适合每次查询都跑）。采用务实方案：

    检索层（零 LLM 调用）：
        重排完成后检查 mean(rerank_score)，低于阈值时扩展 top_k 重排。
        纯数学计算，不增加延迟和成本。

    生成层（复用已有 LLMJudgeService）：
        将现有 _reflect() 从内联简单 prompt 升级为调用 LLMJudgeService，
        返回结构化 EvalResult。faithfulness 低于阈值时拦截并重生成答案
        （check_and_regenerate），而非仅标记 low_confidence。

设计要点：
    - 检索层重试上限 1 次，避免无限循环；
    - 生成层低置信度时重生成（最多 1 次），避免无限循环；
    - 所有守卫可通过 RAG_QUALITY_GUARD_ENABLED 总开关关闭；
    - LLMJudgeService 不可用时降级为跳过。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class RetrievalQualityResult:
    """检索质量检查结果。

    Attributes:
        mean_score: 重排分数均值。
        passed: 是否通过质量阈值。
        retry_attempted: 是否已尝试扩展重排。
        doc_count: 检查的文档数。
    """

    mean_score: float
    passed: bool
    retry_attempted: bool = False
    doc_count: int = 0


class QualityGuard:
    """双层质量守卫 — 检索层零 LLM 调用，生成层复用 LLMJudgeService。

    通过构造注入 AgenticRAGEngine，可替换为 Mock 进行测试。
    """

    def __init__(self) -> None:
        self._judge_service: Any = None  # 延迟初始化 LLMJudgeService
        # P2: 基于查询频率的动态阈值调节器（懒初始化）
        self._freq_threshold: Any = None

    @property
    def _settings(self) -> Any:
        """每次访问时获取最新配置（支持运行时热更新和测试 mock）。"""
        return get_settings()

    @property
    def enabled(self) -> bool:
        """质量守卫总开关。"""
        return getattr(self._settings, "RAG_QUALITY_GUARD_ENABLED", True)

    # ------------------------------------------------------------------
    # 检索质量守卫 — 零 LLM 调用
    # ------------------------------------------------------------------

    def check_retrieval_quality(
        self,
        reranked_docs: list[dict[str, Any]],
        threshold_override: float | None = None,
    ) -> RetrievalQualityResult:
        """检查重排分数均值，判断是否需要扩展重排。

        纯数学计算：mean([doc["score"] for doc in reranked_docs])。
        不调用任何外部服务，零延迟。

        Args:
            reranked_docs: 重排后的文档列表，每个 doc 含 "score" 字段。
            threshold_override: 动态阈值（P2 频率自适应）。传入时优先使用，
                否则回退到静态配置 RAG_RETRIEVAL_SCORE_THRESHOLD。

        Returns:
            RetrievalQualityResult 检查结果。
        """
        if not reranked_docs:
            return RetrievalQualityResult(
                mean_score=0.0, passed=False, doc_count=0
            )

        scores = [
            float(doc.get("score", 0.0))
            for doc in reranked_docs
            if doc.get("score") is not None
        ]

        if not scores:
            return RetrievalQualityResult(
                mean_score=0.0, passed=False, doc_count=len(reranked_docs)
            )

        mean_score = sum(scores) / len(scores)
        threshold = (
            threshold_override
            if threshold_override is not None
            else getattr(self._settings, "RAG_RETRIEVAL_SCORE_THRESHOLD", 0.3)
        )

        result = RetrievalQualityResult(
            mean_score=round(mean_score, 4),
            passed=mean_score >= threshold,
            doc_count=len(reranked_docs),
        )

        log.info(
            "quality_guard.retrieval_check",
            mean_score=result.mean_score,
            threshold=threshold,
            passed=result.passed,
            doc_count=result.doc_count,
        )

        return result

    def should_retry_retrieval(
        self,
        check_result: RetrievalQualityResult,
        retry_count: int,
    ) -> bool:
        """判断是否应该重试检索（扩展重排 top_k）。

        条件：质量未通过 + 未超过重试上限 + 守卫已启用。

        Args:
            check_result: check_retrieval_quality 的返回值。
            retry_count: 当前重试次数。

        Returns:
            是否应该重试。
        """
        if not self.enabled:
            return False

        max_retries = getattr(self._settings, "RAG_RETRIEVAL_MAX_RETRIES", 1)
        if retry_count >= max_retries:
            return False

        should = not check_result.passed and check_result.doc_count > 0

        if should:
            log.info(
                "quality_guard.retrieval_retry",
                retry_count=retry_count,
                max_retries=max_retries,
                mean_score=check_result.mean_score,
            )

        return should

    def get_expanded_top_k(self) -> int:
        """获取扩展后的 rerank top_k。"""
        base = getattr(self._settings, "RAG_RERANK_TOP_K", 5)
        expand = getattr(self._settings, "RAG_RETRIEVAL_EXPAND_TOP_K", 10)
        return base + expand

    # ------------------------------------------------------------------
    # P2: 动态匹配阈值 — 基于查询频率自适应调节
    # ------------------------------------------------------------------

    def _get_freq_threshold(self) -> Any | None:
        """懒初始化 FrequencyBasedThreshold — 不可用时返回 None。"""
        if self._freq_threshold is not None:
            return self._freq_threshold
        try:
            from app.rag.frequency_threshold import FrequencyBasedThreshold

            self._freq_threshold = FrequencyBasedThreshold()
            return self._freq_threshold
        except Exception as exc:
            log.warning("quality_guard.freq_threshold_init_error", error=str(exc))
            return None

    async def record_query_frequency(self, query: str) -> None:
        """记录一次查询频次（用于动态阈值计算）。

        优雅降级：FrequencyBasedThreshold 不可用或 Redis 异常时静默跳过，
        不影响检索主流程。应在每次用户查询的首次检索迭代调用。
        """
        fbt = self._get_freq_threshold()
        if fbt is None:
            return
        try:
            await fbt.record_query(query)
        except Exception as exc:
            log.debug("quality_guard.record_freq_error", error=str(exc))

    async def get_dynamic_threshold(self, query: str) -> float | None:
        """获取查询的动态匹配阈值。

        Returns:
            动态阈值（float）；动态阈值关闭或不可用时返回 None，
            调用方应回退到静态阈值。
        """
        fbt = self._get_freq_threshold()
        if fbt is None:
            return None
        try:
            if not fbt.enabled:
                return None
            return await fbt.get_threshold(query)
        except Exception as exc:
            log.debug("quality_guard.dynamic_threshold_error", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # 生成质量守卫 — 复用 LLMJudgeService
    # ------------------------------------------------------------------

    async def check_generation_quality(
        self,
        query: str,
        answer: str,
        contexts: list[str],
    ) -> Any | None:
        """调用 LLMJudgeService 评估生成质量。

        复用已有的 LLMJudgeService.evaluate_single()，不新增 LLM 调用。
        原 _reflect() 就调一次 LLM，现在改为调 Judge，调用次数不变。

        Args:
            query: 用户问题。
            answer: RAG 生成的答案。
            contexts: 引用文档内容列表。

        Returns:
            EvalResult 评测结果，或 None（Judge 不可用时）。
        """
        if not self.enabled or not answer:
            return None

        try:
            judge = self._get_judge_service()
            if judge is None:
                return None

            result = await judge.evaluate_single(
                question=query, answer=answer, contexts=contexts
            )

            threshold = getattr(
                self._settings, "RAG_FAITHFULNESS_THRESHOLD", 3.0
            )
            low_confidence = result.hallucination_inverse < threshold

            log.info(
                "quality_guard.generation_check",
                citation=result.citation_accuracy,
                completeness=result.completeness,
                faithfulness=result.hallucination_inverse,
                total=result.total_score,
                low_confidence=low_confidence,
                passed=result.passed,
            )

            return result

        except Exception as exc:
            log.warning("quality_guard.generation_error", error=str(exc))
            return None

    def is_low_confidence(self, eval_result: Any | None) -> bool:
        """判断评测结果是否为低置信度。

        Args:
            eval_result: EvalResult 实例或 None。

        Returns:
            True 如果 faithfulness 低于阈值。
        """
        if eval_result is None or getattr(eval_result, "error", None):
            return False

        threshold = getattr(
            self._settings, "RAG_FAITHFULNESS_THRESHOLD", 3.0
        )
        return eval_result.hallucination_inverse < threshold

    # ------------------------------------------------------------------
    # 忠实度拦截 — 低置信度时重生成答案
    # ------------------------------------------------------------------

    async def check_and_regenerate(
        self,
        query: str,
        answer: str,
        contexts: list[str],
        generator: Any = None,
    ) -> tuple[str, Any | None]:
        """检查生成质量，低置信度时重生成答案。

        流程：
            1. 调用 check_generation_quality 评估答案忠实度；
            2. 如果 is_low_confidence 为 True 且 generator 可用：
               - 使用增强 prompt（强调禁止编造）重新生成答案；
               - 对新答案再次评估；
               - 返回新答案和新评测结果；
            3. 如果不可重生成（generator 不可用或守卫关闭）：
               - 返回原答案和原评测结果。

        Args:
            query: 用户问题。
            answer: 原始答案文本。
            contexts: 引用文档内容列表。
            generator: 答案生成器实例（需支持 async generate 方法），
                       为 None 时跳过重生成。

        Returns:
            tuple[str, EvalResult | None]: (最终答案, 最终评测结果)。
        """
        # 原始评估
        eval_result = await self.check_generation_quality(
            query=query, answer=answer, contexts=contexts,
        )

        # 检查是否需要重生成
        if not self.is_low_confidence(eval_result):
            return answer, eval_result

        log.warning(
            "quality_guard.low_confidence_detected",
            faithfulness=getattr(eval_result, "hallucination_inverse", None),
            threshold=getattr(self._settings, "RAG_FAITHFULNESS_THRESHOLD", 3.0),
            action="regenerating",
        )

        # 无法重生成时返回原答案
        if generator is None:
            log.warning("quality_guard.no_generator_skip_regen")
            return answer, eval_result

        # 重生成：使用增强 prompt 强调禁止编造
        try:
            new_answer = await self._regenerate_with_strict_prompt(
                generator, query, contexts,
            )
            if not new_answer or not new_answer.strip():
                log.warning("quality_guard.regen_empty_fallback")
                return answer, eval_result

            # 对新答案重新评估
            new_eval = await self.check_generation_quality(
                query=query, answer=new_answer, contexts=contexts,
            )

            log.info(
                "quality_guard.regenerated",
                original_faithfulness=getattr(eval_result, "hallucination_inverse", None),
                new_faithfulness=getattr(new_eval, "hallucination_inverse", None) if new_eval else None,
                improved=(
                    new_eval is not None
                    and getattr(new_eval, "hallucination_inverse", 0)
                    > getattr(eval_result, "hallucination_inverse", 0)
                ),
            )
            return new_answer, new_eval
        except Exception as exc:
            log.warning("quality_guard.regen_failed", error=str(exc))
            return answer, eval_result

    async def _regenerate_with_strict_prompt(
        self,
        generator: Any,
        query: str,
        contexts: list[str],
    ) -> str:
        """使用增强 prompt 重新生成答案。

        增强 prompt 在原有基础上额外强调：
            - 必须严格基于上下文回答，禁止编造信息；
            - 必须使用 [n] 引用标注来源。
        """
        # 组装增强上下文
        context_text = "\n---\n".join(
            ctx[:1000] for ctx in contexts[:5] if ctx
        )

        strict_prompt = (
            "你是企业知识库助手。请严格基于以下上下文回答用户问题。\n"
            "重要规则：\n"
            "1. 禁止编造未在上下文中出现的事实；\n"
            "2. 如果上下文不足以完整回答，请明确说明哪些部分缺乏依据；\n"
            "3. 在引用知识库内容时，必须使用 [n] 标注引用来源"
            "（n 从 1 开始，对应下方来源编号）。\n\n"
            f"=== 知识库来源 ===\n{context_text}"
        )

        # 调用生成器
        answer_parts: list[str] = []
        async for token in generator.generate(
            query=query,
            retrieved_docs=[],  # 已在 prompt 中注入上下文
            tool_results=[],
            memory_context=strict_prompt,
        ):
            answer_parts.append(token)

        return "".join(answer_parts)

    def _get_judge_service(self) -> Any | None:
        """延迟获取 LLMJudgeService 实例。

        不可用时返回 None，调用方降级处理。
        """
        if self._judge_service is not None:
            return self._judge_service

        try:
            from app.observability.llm_judge import LLMJudgeService

            self._judge_service = LLMJudgeService()
            return self._judge_service
        except ImportError:
            log.warning("quality_guard.judge_not_available")
            return None
        except Exception as exc:
            log.warning("quality_guard.judge_init_error", error=str(exc))
            return None
