"""
LLM-as-Judge 自动评测服务 — 使用独立 Judge 模型对 RAG 生成答案进行质量评分。

三个评分维度（每项 0-5 分）：
    1. 引用准确性（citation_accuracy）— 答案中的事实是否能从引用文档中找到
    2. 完整性（completeness）— 答案是否完整回答了用户问题
    3. 幻觉率（hallucination_inverse）— 答案是否包含引用文档中不存在的信息
       （分数越高表示幻觉越少，5 分 = 无幻觉）

总分 = (引用准确性 + 完整性 + 幻觉率) / 3

评测流程：
    1. 从基准数据集（benchmark）中取测试用例
    2. 用 RAG 引擎生成答案
    3. 用 Judge LLM 对 (question, answer, contexts) 打分
    4. 汇总评测报告

设计要点：
    - Judge LLM 与 RAG LLM 可以是同一个 Provider（SaaS）或不同模型（私有）
    - 评测不可用时降级为跳过（不影响主流程）
    - 支持批量评测和单条评测
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Judge 系统提示词
_JUDGE_SYSTEM_PROMPT = """你是企业知识库答案质量评测专家。请根据以下信息对答案进行评分。

## 评分对象
- 用户问题：{question}
- 生成答案：{answer}
- 引用文档（上下文）：
{contexts}

## 评分维度（每项 0-5 分，整数）

1. **citation_accuracy**（引用准确性）：答案中的事实陈述是否能在引用文档中找到？
   - 5 分：所有事实均可溯源
   - 3 分：大部分事实可溯源，少数模糊
   - 0 分：事实与引用文档矛盾

2. **completeness**（完整性）：答案是否完整回答了用户问题？
   - 5 分：完全覆盖问题所有方面
   - 3 分：部分覆盖，有遗漏
   - 0 分：未回答问题

3. **hallucination_inverse**（无幻觉度）：答案是否包含引用文档中不存在的信息？
   - 5 分：无幻觉，所有内容均有出处
   - 3 分：少量推测性表述但无实质错误
   - 0 分：大量编造信息

## 输出格式（严格 JSON）

```json
{{
    "citation_accuracy": 4,
    "completeness": 5,
    "hallucination_inverse": 4,
    "total_score": 4.33,
    "reasoning": "简要说明评分理由（100 字以内）"
}}
```

只输出 JSON，不要其他内容。"""


@dataclass
class EvalResult:
    """单条评测结果。"""

    question: str
    answer: str
    citation_accuracy: int = 0
    completeness: int = 0
    hallucination_inverse: int = 0
    total_score: float = 0.0
    reasoning: str = ""
    error: str | None = None

    @property
    def passed(self) -> bool:
        """总分 >= 3.0 视为通过。"""
        return self.total_score >= 3.0 and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question[:100],
            "answer": self.answer[:100],
            "citation_accuracy": self.citation_accuracy,
            "completeness": self.completeness,
            "hallucination_inverse": self.hallucination_inverse,
            "total_score": self.total_score,
            "reasoning": self.reasoning,
            "passed": self.passed,
            "error": self.error,
        }


@dataclass
class EvalReport:
    """评测报告 — 批量评测汇总。"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    avg_score: float = 0.0
    avg_citation_accuracy: float = 0.0
    avg_completeness: float = 0.0
    avg_hallucination_inverse: float = 0.0
    results: list[EvalResult] = field(default_factory=list)
    evaluated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.passed / self.total, 2) if self.total else 0,
            "avg_score": round(self.avg_score, 2),
            "avg_citation_accuracy": round(self.avg_citation_accuracy, 2),
            "avg_completeness": round(self.avg_completeness, 2),
            "avg_hallucination_inverse": round(self.avg_hallucination_inverse, 2),
            "results": [r.to_dict() for r in self.results],
            "evaluated_at": self.evaluated_at,
        }


class LLMJudgeService:
    """LLM-as-Judge 自动评测服务。

    使用独立 LLM 对 RAG 生成答案从引用准确性、完整性、幻觉率三维度打分。
    """

    def __init__(
        self,
        judge_llm: LLMProvider | None = None,
    ) -> None:
        self._judge_llm = judge_llm

    @property
    def judge_llm(self) -> LLMProvider:
        """延迟获取 Judge LLM — 默认复用 RAG 的 LLM Provider。"""
        if self._judge_llm is None:
            from app.llm.factory import get_llm_provider

            self._judge_llm = get_llm_provider()
        return self._judge_llm

    async def evaluate_single(
        self,
        question: str,
        answer: str,
        contexts: list[str],
    ) -> EvalResult:
        """评测单条问答。

        Args:
            question: 用户问题。
            answer: RAG 生成的答案。
            contexts: 引用文档内容列表。

        Returns:
            EvalResult 评测结果。
        """
        contexts_text = "\n".join(
            f"[文档{i + 1}] {ctx[:500]}"
            for i, ctx in enumerate(contexts[:5])
        )

        prompt = _JUDGE_SYSTEM_PROMPT.format(
            question=question,
            answer=answer,
            contexts=contexts_text or "（无引用文档）",
        )

        messages: list[Message] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请评分并输出 JSON。"},
        ]

        try:
            response_text = ""
            async for chunk in self.judge_llm.chat(messages, stream=False):
                if isinstance(chunk, str):
                    response_text += chunk

            return self._parse_judge_response(question, answer, response_text)
        except Exception as exc:
            logger.error("judge.evaluate_error", error=str(exc))
            return EvalResult(
                question=question,
                answer=answer,
                error=str(exc),
            )

    async def evaluate_batch(
        self,
        test_cases: list[dict[str, Any]],
    ) -> EvalReport:
        """批量评测。

        Args:
            test_cases: 测试用例列表，每项包含 question, answer, contexts。

        Returns:
            EvalReport 评测报告。
        """
        report = EvalReport()
        report.evaluated_at = datetime.utcnow().isoformat()

        for case in test_cases:
            result = await self.evaluate_single(
                question=case.get("question", ""),
                answer=case.get("answer", ""),
                contexts=case.get("contexts", []),
            )
            report.results.append(result)
            report.total += 1
            if result.passed:
                report.passed += 1
            else:
                report.failed += 1

        # 计算平均分
        if report.total > 0:
            valid_results = [r for r in report.results if r.error is None]
            n = len(valid_results)
            if n > 0:
                report.avg_score = sum(r.total_score for r in valid_results) / n
                report.avg_citation_accuracy = (
                    sum(r.citation_accuracy for r in valid_results) / n
                )
                report.avg_completeness = (
                    sum(r.completeness for r in valid_results) / n
                )
                report.avg_hallucination_inverse = (
                    sum(r.hallucination_inverse for r in valid_results) / n
                )

        logger.info(
            "judge.batch_complete",
            total=report.total,
            passed=report.passed,
            avg_score=round(report.avg_score, 2),
        )
        return report

    def _parse_judge_response(
        self,
        question: str,
        answer: str,
        response_text: str,
    ) -> EvalResult:
        """解析 Judge LLM 的 JSON 响应。"""
        # 提取 JSON（可能被包裹在 markdown 代码块中）
        json_str = self._extract_json(response_text)
        if not json_str:
            return EvalResult(
                question=question,
                answer=answer,
                error=f"Judge 响应无法解析为 JSON: {response_text[:200]}",
            )

        try:
            data = json.loads(json_str)
            citation = int(data.get("citation_accuracy", 0))
            completeness = int(data.get("completeness", 0))
            hallucination = int(data.get("hallucination_inverse", 0))

            # 计算总分
            if data.get("total_score") is not None:
                total = float(data["total_score"])
            else:
                total = round((citation + completeness + hallucination) / 3, 2)

            return EvalResult(
                question=question,
                answer=answer,
                citation_accuracy=citation,
                completeness=completeness,
                hallucination_inverse=hallucination,
                total_score=total,
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            return EvalResult(
                question=question,
                answer=answer,
                error=f"JSON 解析失败: {exc}",
            )

    @staticmethod
    def _extract_json(text: str) -> str | None:
        """从文本中提取 JSON 字符串。

        处理三种情况：
        1. 纯 JSON 文本
        2. markdown 代码块包裹 ```json ... ```
        3. 普通代码块包裹 ``` ... ```
        """
        text = text.strip()

        # 尝试直接解析
        if text.startswith("{"):
            return text

        # 尝试提取代码块
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return text[start:end].strip()

        if "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            return text[start:end].strip()

        # 尝试找到第一个 { 到最后一个 }
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            return text[first_brace : last_brace + 1]

        return None
