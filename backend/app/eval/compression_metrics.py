"""
压缩信息损耗评估 — P1-6：量化 ContextBudgetManager 压缩前后的信息保留程度。

两个互补的评估维度：

1. **关键实体保留率（entity retention rate）— 纯规则，零成本**
   从压缩前消息中抽取关键实体（数值/日期/编号/引号术语/英文标识符/中文专有名词），
   统计压缩后消息中仍保留的比例。实体是答案事实性的骨架，
   保留率 < 阈值说明压缩丢失了关键事实指针。

2. **LLM Judge 一致性（consistency）— 双跑对比**
   同一 query 分别在未压缩 / 压缩上下文下生成两份答案，
   由 Judge LLM 对比关键结论一致性（0-5 分），
   并复用 LLMJudgeService 分别评估两份答案的 faithfulness，比较衰减幅度。

使用方式::

    from app.eval.compression_metrics import (
        compute_entity_retention_from_snapshot,
        CompressionJudge,
    )

    # 维度一：从 ContextBudgetManager 快照直接计算实体保留率
    snapshot = budget_manager.get_last_snapshot()
    report = compute_entity_retention_from_snapshot(snapshot)

    # 维度二：双跑一致性评估（需要 LLM）
    judge = CompressionJudge()
    result = await judge.evaluate_consistency(question, answer_full, answer_compressed)

设计要点：
    - 实体保留率为纯函数，可纳入 eval-regression 流水线做硬门禁；
    - LLM Judge 维度与主流程解耦，Judge 不可用时优雅降级（error 字段）；
    - 不修改 ContextBudgetManager 的压缩逻辑，只消费其暴露的快照。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# ======================================================================
# 维度一：关键实体保留率（规则抽取）
# ======================================================================

#: 数值表达式：金额、百分比、日期、编号、版本号等（答案事实性的核心骨架）
_NUMBER_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:%|％|元|万元|亿元|万|亿|天|个工作日|小时|次|条|篇|人|份))"
    r"|(?:\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)"
    r"|(?:\d+\.\d+\.\d+)"
    r"|(?:第?\d+(?:号|条|款|章|节))"
)

#: 引号包裹的术语（《制度名》、“条款”、“文件名”）
_QUOTED_PATTERN = re.compile(r"《[^》]{2,30}》|“[^”]{2,30}”|\"[^\"]{2,30}\"")

#: 英文标识符：全大写缩写（API、SLA）、含数字的型号（GPT-4、BGE-M3）
_ENGLISH_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_-]{1,15}\b|\b[A-Za-z]+-[A-Za-z0-9]+\b")

#: 中文专有名词后缀词典 — 以这些后缀结尾的 2-10 字词视为关键实体
_CJK_SUFFIXES = (
    "流程", "制度", "政策", "规定", "规范", "标准", "方案", "报告",
    "部门", "系统", "平台", "项目", "预算", "费用", "发票", "合同",
    "权限", "密码", "账号", "指标", "模型", "接口", "数据库", "服务器",
)
_CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff]{2,10}?(?:" + "|".join(_CJK_SUFFIXES) + r")"
)

#: 实体最小长度（过滤过短的噪声匹配）
_MIN_ENTITY_LEN = 2

#: 实体保留率通过阈值（eval-regression 门禁）
ENTITY_RETENTION_THRESHOLD = 0.90

#: 一致性评分阈值：>= 4 一致，2.5-4 轻微损耗，< 2.5 严重损耗
CONSISTENCY_THRESHOLD_OK = 4.0
CONSISTENCY_THRESHOLD_MAJOR_LOSS = 2.5


def extract_key_entities(text: str) -> set[str]:
    """从文本中抽取关键实体集合（规则法，零成本）。

    抽取四类实体：
        1. 数值表达式（金额/百分比/日期/编号/版本号）
        2. 引号包裹的术语（《》、“”、""）
        3. 英文标识符（缩写、型号）
        4. 中文专有名词（以常见机构/制度/系统后缀结尾的词）

    Args:
        text: 原始文本。

    Returns:
        去重后的实体集合（已归一化空白）。
    """
    if not text:
        return set()

    entities: set[str] = set()
    for pattern in (_NUMBER_PATTERN, _QUOTED_PATTERN, _ENGLISH_PATTERN, _CJK_PATTERN):
        for match in pattern.findall(text):
            entity = match.strip()
            if len(entity) >= _MIN_ENTITY_LEN:
                entities.add(entity)
    return entities


def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    """将消息列表拼接为纯文本（跳过 system prompt — 压缩不改变它）。"""
    return "\n".join(
        m.get("content", "") for m in messages if m.get("role") != "system"
    )


@dataclass
class EntityRetentionReport:
    """关键实体保留率报告。"""

    total_entities: int = 0
    retained_entities: int = 0
    retention_rate: float = 1.0
    retained: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """保留率 >= ENTITY_RETENTION_THRESHOLD 视为通过。"""
        return self.retention_rate >= ENTITY_RETENTION_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_entities": self.total_entities,
            "retained_entities": self.retained_entities,
            "retention_rate": round(self.retention_rate, 4),
            "threshold": ENTITY_RETENTION_THRESHOLD,
            "passed": self.passed,
            "missing": self.missing[:20],
        }


def compute_entity_retention(
    before_messages: list[dict[str, Any]],
    after_messages: list[dict[str, Any]],
) -> EntityRetentionReport:
    """计算压缩前后消息列表的关键实体保留率。

    以压缩前的实体集合为基准，统计压缩后文本中仍出现的比例。
    压缩前无实体时视为完全保留（retention_rate = 1.0）。

    Args:
        before_messages: 压缩前消息列表。
        after_messages: 压缩后消息列表。

    Returns:
        EntityRetentionReport。
    """
    before_entities = extract_key_entities(_messages_to_text(before_messages))
    report = EntityRetentionReport(total_entities=len(before_entities))
    if not before_entities:
        return report

    after_text = _messages_to_text(after_messages)
    for entity in sorted(before_entities):
        if entity in after_text:
            report.retained.append(entity)
        else:
            report.missing.append(entity)

    report.retained_entities = len(report.retained)
    report.retention_rate = report.retained_entities / report.total_entities

    log.info(
        "compression_metrics.entity_retention",
        total=report.total_entities,
        retained=report.retained_entities,
        rate=round(report.retention_rate, 4),
        passed=report.passed,
    )
    return report


def compute_entity_retention_from_snapshot(
    snapshot: dict[str, Any] | None,
) -> EntityRetentionReport | None:
    """从 ContextBudgetManager.get_last_snapshot() 的快照计算实体保留率。

    Args:
        snapshot: 含 ``before`` / ``after`` 消息列表的快照；None 或结构不符时返回 None。

    Returns:
        EntityRetentionReport 或 None。
    """
    if not snapshot:
        return None
    before = snapshot.get("before")
    after = snapshot.get("after")
    if not isinstance(before, list) or not isinstance(after, list):
        return None
    return compute_entity_retention(before, after)


# ======================================================================
# 维度二：LLM Judge 一致性（双跑对比）
# ======================================================================

#: 关键结论一致性 Judge 提示词
_CONSISTENCY_PROMPT = """你是答案一致性评测专家。同一问题在两份不同上下文（完整版 vs 压缩版）下生成了两份答案，请评估它们的关键结论是否一致。

## 评估对象
- 用户问题：{question}
- 答案 A（完整上下文）：{answer_a}
- 答案 B（压缩上下文）：{answer_b}

## 评估标准
对比两份答案的**关键结论**（核心观点、关键数字、操作步骤、结论性判断）：
- 5 分：关键结论完全一致，仅措辞差异
- 4 分：主要结论一致，次要细节有遗漏但不影响决策
- 3 分：核心结论一致，但缺失部分关键数字或步骤
- 2 分：部分结论不一致，可能误导用户
- 0-1 分：关键结论矛盾或答案 B 严重缺失核心内容

## 输出格式（严格 JSON）

```json
{{
    "consistency_score": 4,
    "key_diffs": ["答案B缺少金额上限 5000 元"],
    "reasoning": "简要说明（100 字以内）"
}}
```

只输出 JSON，不要其他内容。"""


@dataclass
class ConsistencyResult:
    """双跑一致性评估结果。"""

    consistency_score: float = 0.0
    verdict: str = "unknown"  # consistent / minor_loss / major_loss
    key_diffs: list[str] = field(default_factory=list)
    reasoning: str = ""
    error: str | None = None

    @property
    def passed(self) -> bool:
        """一致性 >= CONSISTENCY_THRESHOLD_OK 视为通过。"""
        return self.error is None and self.consistency_score >= CONSISTENCY_THRESHOLD_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "consistency_score": self.consistency_score,
            "verdict": self.verdict,
            "key_diffs": self.key_diffs,
            "reasoning": self.reasoning,
            "passed": self.passed,
            "error": self.error,
        }


@dataclass
class DualFaithfulnessReport:
    """双跑 faithfulness 对比报告 — 复用 LLMJudgeService 三维度评分。"""

    score_uncompressed: float = 0.0
    score_compressed: float = 0.0
    degradation: float = 0.0  # score_uncompressed - score_compressed
    error: str | None = None

    @property
    def passed(self) -> bool:
        """衰减 <= 0.5 分（0-5 分制）视为通过。"""
        return self.error is None and self.degradation <= 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "score_uncompressed": round(self.score_uncompressed, 2),
            "score_compressed": round(self.score_compressed, 2),
            "degradation": round(self.degradation, 2),
            "passed": self.passed,
            "error": self.error,
        }


class CompressionJudge:
    """压缩信息损耗 LLM 评估器。

    双跑对比两种模式：
        - evaluate_consistency：对比两份答案的关键结论一致性（专用 prompt）；
        - evaluate_dual_faithfulness：复用 LLMJudgeService 分别评估两份答案的
          生成质量，比较衰减幅度。

    Args:
        judge_llm: Judge LLM Provider；None 时延迟复用 RAG 的 LLM Provider。
    """

    def __init__(self, judge_llm: Any = None) -> None:
        self._judge_llm = judge_llm

    @property
    def judge_llm(self) -> Any:
        if self._judge_llm is None:
            from app.llm.factory import get_llm_provider

            self._judge_llm = get_llm_provider()
        return self._judge_llm

    async def evaluate_consistency(
        self,
        question: str,
        answer_uncompressed: str,
        answer_compressed: str,
    ) -> ConsistencyResult:
        """评估两份答案的关键结论一致性。

        Args:
            question: 用户问题。
            answer_uncompressed: 完整上下文下生成的答案。
            answer_compressed: 压缩上下文下生成的答案。

        Returns:
            ConsistencyResult（Judge 不可用时 error 非 None）。
        """
        import json

        from app.llm.base import Message
        from app.observability.llm_judge import LLMJudgeService

        prompt = _CONSISTENCY_PROMPT.format(
            question=question,
            answer_a=answer_uncompressed[:2000],
            answer_b=answer_compressed[:2000],
        )
        messages: list[Message] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请评估并输出 JSON。"},
        ]

        try:
            response_text = ""
            async for chunk in self.judge_llm.chat(messages, stream=False):
                if isinstance(chunk, str):
                    response_text += chunk

            json_str = LLMJudgeService._extract_json(response_text)
            if not json_str:
                return ConsistencyResult(
                    error=f"Judge 响应无法解析为 JSON: {response_text[:200]}"
                )
            data = json.loads(json_str)
            score = float(data.get("consistency_score", 0))
            return ConsistencyResult(
                consistency_score=score,
                verdict=self._score_to_verdict(score),
                key_diffs=list(data.get("key_diffs", [])),
                reasoning=data.get("reasoning", ""),
            )
        except Exception as exc:
            log.error("compression_metrics.consistency_error", error=str(exc))
            return ConsistencyResult(error=str(exc))

    async def evaluate_dual_faithfulness(
        self,
        question: str,
        answer_uncompressed: str,
        contexts_uncompressed: list[str],
        answer_compressed: str,
        contexts_compressed: list[str],
    ) -> DualFaithfulnessReport:
        """分别评估两份答案的生成质量并比较衰减幅度。

        复用 LLMJudgeService.evaluate_single（引用准确性 + 完整性 + 无幻觉度）。

        Args:
            question: 用户问题。
            answer_uncompressed: 完整上下文下生成的答案。
            contexts_uncompressed: 完整上下文。
            answer_compressed: 压缩上下文下生成的答案。
            contexts_compressed: 压缩上下文。

        Returns:
            DualFaithfulnessReport。
        """
        from app.observability.llm_judge import LLMJudgeService

        judge = LLMJudgeService(judge_llm=self.judge_llm)
        try:
            result_full = await judge.evaluate_single(
                question, answer_uncompressed, contexts_uncompressed
            )
            result_compressed = await judge.evaluate_single(
                question, answer_compressed, contexts_compressed
            )
            if result_full.error:
                return DualFaithfulnessReport(error=f"完整上下文评测失败: {result_full.error}")
            if result_compressed.error:
                return DualFaithfulnessReport(error=f"压缩上下文评测失败: {result_compressed.error}")

            return DualFaithfulnessReport(
                score_uncompressed=result_full.total_score,
                score_compressed=result_compressed.total_score,
                degradation=result_full.total_score - result_compressed.total_score,
            )
        except Exception as exc:
            log.error("compression_metrics.dual_faithfulness_error", error=str(exc))
            return DualFaithfulnessReport(error=str(exc))

    @staticmethod
    def _score_to_verdict(score: float) -> str:
        """将一致性评分映射为判定结论。"""
        if score >= CONSISTENCY_THRESHOLD_OK:
            return "consistent"
        if score >= CONSISTENCY_THRESHOLD_MAJOR_LOSS:
            return "minor_loss"
        return "major_loss"
