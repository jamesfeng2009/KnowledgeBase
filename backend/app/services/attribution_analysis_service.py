"""结果归因分析服务 — 把 outcome_attribution 的纯函数能力接进可复用链路。

设计边界（重要）：
    本服务**不内置任何业务数据，也不访问业务表**。决策事件（decisions）与
    结果事件（outcomes）必须由调用方显式注入 —— 因为项目当前**没有** A/B
    曝光日志表、也没有 OA 驳回原因事件流（`tickets` 只有 IT 工单五分类，
    `outcome_attribution.py` 的 demo 明确用合成数据）。数据源没接好之前，
    本服务对「本季度报销驳回主要原因」这类问题的诚实回答是 ``no_data``，
    而不是编一个看似合理的归因。

    这样做的依据是 P1 场景识别的第一原则：先问「数据在哪」，没有事实锚点
    就不要生成结论。本服务的价值是把「数据接好之后」的分析链路提前建好，
    并保证结论一定经过 A/A 负对照门禁 + 可点回原始 decision_id。

提供的能力：
    1. ``analyze()`` — 注入事件 → A/A 负对照门禁 → outcome_report → 结构化报告
       （含 evidence_decision_ids 事实锚点）。空输入返回 ``no_data``。
    2. ``promote_to_candidate()`` — 仅当报告 ``trustworthy=True`` 且调用方
       显式要求沉淀时，写入 knowledge_candidates 走既有审批流；负对照不过
       或数据不足一律拒绝，不污染正式知识库。

用法::

    svc = AttributionAnalysisService()
    result = svc.analyze(
        decisions=[{"decision_id": "d1", "arm": "control", ...}],
        outcomes=[{"decision_id": "d1", "filled": True, ...}],
        metric="filled",
    )
    if result["trustworthy"]:
        await svc.promote_to_candidate(pool, result, user_id=...)
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from app.services.outcome_attribution import (
    aa_negative_control,
    outcome_report,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 归因结论沉淀到候选池使用的 source_type。
#: 注意：knowledge_candidates.recount_from_events 的 _SIGNAL_WEIGHTS
#: 只识别 chat_feedback / qa_accepted，本类型**不计入支持度** —— 这是刻意的：
#: 一次分析结果不代表用户采纳，不能靠它把结论推过晋升门槛。
ATTRIBUTION_SOURCE_TYPE = "attribution_analysis"

#: A/A 负对照默认参数：control 臂、filled 指标、固定种子保证报告可复现。
_DEFAULT_AA_ARM = "control"
_DEFAULT_AA_METRIC = "filled"


class AttributionAnalysisService:
    """结果归因分析编排服务（无状态，事件由调用方注入）。"""

    def analyze(
        self,
        decisions: Iterable[dict[str, Any]],
        outcomes: Iterable[dict[str, Any]],
        *,
        metric: str = "filled",
        strata_keys: Sequence[str] = (),
        min_effect: float = 0.02,
        alpha: float = 0.05,
        min_samples: int = 100,
        guardrail_metrics: Sequence[str] = (),
        aa_arm: str = _DEFAULT_AA_ARM,
        aa_metric: str = _DEFAULT_AA_METRIC,
        run_negative_control: bool = True,
    ) -> dict[str, Any]:
        """归因分析 + 负对照门禁。

        Args:
            decisions / outcomes: 调用方注入的事件 dict 列表（绝不内置数据）。
            metric: 北极星指标字段名（filled/responded/utilization…）。
            strata_keys: 分层维度（用于 Mantel-Haenszel 混杂校正）。
            min_effect / alpha / min_samples / guardrail_metrics: 门禁参数。
            aa_arm / aa_metric: A/A 负对照取哪一臂、哪个二值指标。
            run_negative_control: 是否跑负对照。默认 True；关闭时报告会
                明确标注 ``trustworthy=False``，因为缺了管道自检这一关。

        Returns:
            结构化 dict：
            - ``status``: ``ok`` | ``no_data``
            - ``trustworthy``: 是否可据以出结论（负对照通过 + 匹配率达标）
            - ``report``: outcome_report 结果（含 verdict / diagnostics）
            - ``negative_control``: A/A 结果（含 passed / reason）
            - ``evidence_decision_ids``: 可点回原始记录的 decision_id 列表
            - ``warnings``: 给上层的诚实提示
        """
        dec_list = list(decisions)
        out_list = list(outcomes)

        # 数据门禁：没有任何事件 → 明确返回 no_data，绝不编造归因。
        if not dec_list or not out_list:
            msg = (
                "未接入决策/结果事件数据源，无法归因。"
                "项目当前没有 A/B 曝光日志表或 OA 驳回原因事件流；"
                "需先由业务系统注入 decisions/outcomes 事件后才能分析。"
            )
            log.info("attribution.no_data", metric=metric)
            return {
                "status": "no_data",
                "trustworthy": False,
                "report": None,
                "negative_control": None,
                "evidence_decision_ids": [],
                "warnings": [msg],
            }

        # 先跑负对照：分析管道本身会自造效应时，真实结论一律不可信。
        negative_control: dict[str, Any] | None = None
        if run_negative_control:
            negative_control = aa_negative_control(
                dec_list,
                out_list,
                arm=aa_arm,
                metric=aa_metric,
                alpha=alpha,
            )
        else:
            negative_control = {
                "method": "aa_negative_control",
                "passed": False,
                "reason": "调用方关闭了负对照，缺少管道自检，结论不可信",
            }

        report = outcome_report(
            dec_list,
            out_list,
            metric=metric,
            strata_keys=strata_keys,
            min_effect=min_effect,
            alpha=alpha,
            min_samples=min_samples,
            guardrail_metrics=guardrail_metrics,
        )

        diagnostics = report.get("diagnostics") or {}
        match_rate = float(diagnostics.get("match_rate") or 0.0)
        aa_passed = bool(negative_control.get("passed"))

        warnings: list[str] = []
        if not aa_passed:
            warnings.append(
                "A/A 负对照未通过：分析或分流管道在自造效应，"
                "本次归因结论不可用于决策。"
            )
        if match_rate < 0.8:
            warnings.append(
                f"决策/结果匹配率 {match_rate:.2%} 低于 80%，"
                "结论只覆盖可匹配样本，可能存在选择偏差。"
            )

        trustworthy = aa_passed and match_rate >= 0.8
        # 事实锚点：只暴露真正 join 上结果事件的 decision_id，
        # 保证结论的每条证据都能点回原始记录。
        evidence_ids = [
            str(d.get("decision_id"))
            for d in dec_list
            if d.get("decision_id")
        ]
        # 用 matched 数量收敛锚点规模（避免把 unmatched 的决策也当证据）。
        matched_n = int(diagnostics.get("matched") or 0)
        if matched_n and matched_n < len(evidence_ids):
            evidence_ids = evidence_ids[:matched_n]

        log.info(
            "attribution.analyzed",
            metric=metric,
            trustworthy=trustworthy,
            matched=matched_n,
            aa_passed=aa_passed,
        )
        return {
            "status": "ok",
            "trustworthy": trustworthy,
            "report": report,
            "negative_control": negative_control,
            "evidence_decision_ids": evidence_ids,
            "warnings": warnings,
        }

    async def promote_to_candidate(
        self,
        pool: Any,
        result: dict[str, Any],
        *,
        user_id: Any | None = None,
        message_id: Any | None = None,
    ) -> dict[str, Any]:
        """把可信归因结论沉淀到 knowledge_candidates（走既有审批流）。

        硬门禁（任一不满足即拒绝，不污染正式知识库）：
            - ``status == ok``
            - ``trustworthy == True``（负对照通过 + 匹配率达标）
            - 存在至少一个事实锚点 decision_id

        复用 ``KnowledgeCandidatePool.upsert_from_signal`` 的幂等归簇；
        ``source_type=attribution_analysis`` 不计入支持度，结论不会靠一次
        分析就被推过晋升门槛，仍需后续用户反馈 + 审批。
        """
        if result.get("status") != "ok":
            return {
                "status": "skipped",
                "reason": "no_data",
                "detail": "无事件数据，不沉淀结论",
            }
        if not result.get("trustworthy"):
            return {
                "status": "skipped",
                "reason": "untrustworthy",
                "detail": "负对照未通过或匹配率不足，不沉淀结论",
            }
        evidence_ids = result.get("evidence_decision_ids") or []
        if not evidence_ids:
            return {
                "status": "skipped",
                "reason": "no_evidence",
                "detail": "无可点回原始记录的 decision_id，不沉淀结论",
            }

        report = result.get("report") or {}
        verdict = (report.get("verdict") or {}).get("verdict") or "unknown"
        metric = report.get("metric") or "unknown"
        question_hint = f"归因分析结论（{metric}，判定={verdict}）"
        answer_draft = self._render_answer_draft(result)

        # 用第一个事实锚点做幂等键：同一批事件重复分析不会灌多条候选。
        return await pool.upsert_from_signal(
            source_type=ATTRIBUTION_SOURCE_TYPE,
            source_id=evidence_ids[0],
            user_id=user_id,
            question_hint=question_hint,
            message_id=message_id,
            answer_draft=answer_draft,
        )

    @staticmethod
    def _render_answer_draft(result: dict[str, Any]) -> str:
        """把结构化报告渲染成可读的结论草稿（保留事实锚点，不夸大）。"""
        report = result.get("report") or {}
        verdict = report.get("verdict") or {}
        significance = report.get("significance") or {}
        diagnostics = report.get("diagnostics") or {}
        evidence = result.get("evidence_decision_ids") or []
        parts = [
            f"指标 {report.get('metric', 'unknown')}："
            f"决策 {verdict.get('verdict', 'unknown')}，"
            f"效应 {significance.get('delta', 'n/a')}，"
            f"p={significance.get('p_value', 'n/a')}，"
            f"匹配率 {diagnostics.get('match_rate', 'n/a')}。",
            f"A/A 负对照：{'通过' if (result.get('negative_control') or {}).get('passed') else '未通过'}。",
        ]
        if evidence:
            shown = ", ".join(str(x) for x in evidence[:5])
            suffix = " 等" if len(evidence) > 5 else ""
            parts.append(f"事实锚点（decision_id）：{shown}{suffix}，共 {len(evidence)} 条。")
        for w in result.get("warnings") or []:
            parts.append(f"注意：{w}")
        return "\n".join(parts)
