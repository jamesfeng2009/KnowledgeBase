"""
决策 → 下游结果归因 — 单一职责：把「agent 当时怎么决定」和「后来业务
结果如何」join 起来，按实验臂算出真实差异，并挡住两类常见误判。

为什么单独一层：
    离线评测能回答「答案质量好不好」，回答不了「这个 ranking 决策有没有
    让班次被填上」。后者才是业务结果，而它散落在多个系统里（outreach 记录
    在 A、排班确认在 B、离职在 C），没人 join 过就没有可信的因果结论。
    本模块做三件事：join → 分桶算率 → 显著性判定（复用 app.eval.significance）。

挡住的两类误判（都是真实业务里最常见的翻车方式）：
    1. **匹配率不披露**：决策与结果 join 不上时，如果只报「匹配上的样本」
       的达成率，等于用一批自选样本下结论。join 不上往往本身就有信号
       （比如被拒的候选根本没进入后续系统），所以匹配率必须出现在报告里。
    2. **混杂因素**：两桶的样本构成不同（例如 treatment 恰好分到更多急单），
       合并算出的差异可能全是构成差异，甚至与分层后的方向相反（辛普森
       悖论）。因此除合并比率外，另给按分层维度的 Mantel-Haenszel 校正
       估计，两者不一致时以校正后为准并在报告里标出来。

负对照（A/A）：
    :func:`aa_negative_control` 把同一臂随机劈成两半做同样的检验。它**应该**
    得不出显著差异；如果得出了，说明分析管道本身在制造效应（分流有偏、
    单位错配、重复计数）。上线任何结果指标前应先过这一关。

数据形态：本模块只吃 dict / dataclass，不碰 DB —— 归因分析要能在离线
样本、合成数据、CI 里跑，绑死 ORM 就没法验证。生产接入由调用方把
``ab.exposure`` 日志与各业务系统事件映射成 :class:`DecisionEvent` /
:class:`OutcomeEvent`。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.eval.significance import (
    judge_experiment,
    two_proportion_test,
    wilson_interval,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

__all__ = [
    "AttributedDecision",
    "DecisionEvent",
    "OutcomeEvent",
    "aa_negative_control",
    "join_decisions_outcomes",
    "outcome_report",
]

#: 二值结果指标 → 语义
BINARY_METRICS = ("filled", "retained_30d", "responded")


# ======================================================================
# 事件模型
# ======================================================================


@dataclass(frozen=True)
class DecisionEvent:
    """一次 agent 决策（归因的左表）。

    Attributes:
        decision_id: 决策唯一 ID —— 必须与 :attr:`OutcomeEvent.decision_id`
            同源，这是跨系统 join 的唯一桥梁。
        experiment: 实验名（未参与实验时为空，只进基线统计）。
        arm: control / treatment / ""。
        unit_id: 被决策的业务对象（候选人 / 班次 / 工单）。
        strata: 分层维度（用于混杂校正），如 ``{"urgency": "high"}``。
        correlation_id: 关联到 ``ab.exposure`` 日志的键（会话/请求 ID）。
        model / prompt_variant: 决策时生效的变体，供按变体切片。
        ts: 决策时间（ISO 字符串，仅用于报告排序）。
    """

    decision_id: str
    experiment: str = ""
    arm: str = ""
    unit_id: str = ""
    strata: dict[str, str] = field(default_factory=dict)
    correlation_id: str = ""
    model: str = ""
    prompt_variant: str = ""
    ts: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionEvent:
        strata_raw = data.get("strata")
        strata = (
            {str(k): str(v) for k, v in strata_raw.items()}
            if isinstance(strata_raw, dict)
            else {}
        )
        return cls(
            decision_id=str(data.get("decision_id", "") or ""),
            experiment=str(data.get("experiment", "") or ""),
            arm=str(data.get("arm", "") or ""),
            unit_id=str(data.get("unit_id", "") or ""),
            strata=strata,
            correlation_id=str(data.get("correlation_id", "") or ""),
            model=str(data.get("model", "") or ""),
            prompt_variant=str(data.get("prompt_variant", "") or ""),
            ts=str(data.get("ts", "") or ""),
        )


@dataclass(frozen=True)
class OutcomeEvent:
    """一次决策的下游业务结果（归因的右表）。

    Attributes:
        decision_id: 对应决策 ID。
        filled: 班次是否被填上（核心北极星，None 表示该指标不适用）。
        responded: 候选人是否回应了 outreach（更早、样本量更大的中间指标）。
        retained_30d: 上岗 30 天后是否仍在职（质量而非速度的度量）。
        utilization: 排班利用率（0-1），None 表示未上岗无意义。
        filled_latency_hours: 从决策到填单耗时（小时）。
        source_system: 结果来自哪个系统（报告里披露口径用）。
    """

    decision_id: str
    filled: bool | None = None
    responded: bool | None = None
    retained_30d: bool | None = None
    utilization: float | None = None
    filled_latency_hours: float | None = None
    source_system: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutcomeEvent:
        return cls(
            decision_id=str(data.get("decision_id", "") or ""),
            filled=_as_bool_or_none(data.get("filled")),
            responded=_as_bool_or_none(data.get("responded")),
            retained_30d=_as_bool_or_none(data.get("retained_30d")),
            utilization=_as_float_or_none(data.get("utilization")),
            filled_latency_hours=_as_float_or_none(data.get("filled_latency_hours")),
            source_system=str(data.get("source_system", "") or ""),
        )


def _as_bool_or_none(raw: Any) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "yes", "1", "y"):
            return True
        if low in ("false", "no", "0", "n"):
            return False
    return None


def _as_float_or_none(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AttributedDecision:
    """决策与其结果的连接视图。

    Attributes:
        decision: 左侧决策。
        outcome: 右侧结果；None 表示 join 不上（会计入 match rate）。
    """

    decision: DecisionEvent
    outcome: OutcomeEvent | None = None

    @property
    def matched(self) -> bool:
        return self.outcome is not None


# ======================================================================
# join
# ======================================================================


def join_decisions_outcomes(
    decisions: Iterable[dict[str, Any] | DecisionEvent],
    outcomes: Iterable[dict[str, Any] | OutcomeEvent],
) -> tuple[list[AttributedDecision], dict[str, Any]]:
    """按 decision_id 连接决策与结果，并给出匹配率诊断。

    重复 decision_id 的处理：结果侧保留**第一条**并计入
    ``duplicate_outcomes``。同一决策出现多条结果通常意味着上游按
    「触达次数」而非「决策」发了事件，直接 join 会放大样本量、
    把方差算小 —— 所以宁可丢弃并暴露，也不静默双计。

    Returns:
        ``(rows, diagnostics)``，diagnostics 含 ``n_decisions``、
        ``n_outcomes``、``matched``、``unmatched``、``match_rate``、
        ``duplicate_outcomes``、``orphan_outcomes``（有结果但查无决策）。
    """
    dec_list = [_as_decision(d) for d in decisions]
    out_map: dict[str, OutcomeEvent] = {}
    duplicates = 0
    for raw in outcomes:
        o = _as_outcome(raw)
        if not o.decision_id:
            continue
        if o.decision_id in out_map:
            duplicates += 1
            continue
        out_map[o.decision_id] = o

    seen_ids: set[str] = set()
    rows: list[AttributedDecision] = []
    matched = 0
    for d in dec_list:
        seen_ids.add(d.decision_id)
        o = out_map.get(d.decision_id)
        if o is not None:
            matched += 1
        rows.append(AttributedDecision(decision=d, outcome=o))

    orphans = sum(1 for oid in out_map if oid not in seen_ids)
    n_dec = len(dec_list)
    diagnostics = {
        "n_decisions": n_dec,
        "n_outcomes": len(out_map),
        "matched": matched,
        "unmatched": n_dec - matched,
        "match_rate": round(matched / n_dec, 4) if n_dec else 0.0,
        "duplicate_outcomes": duplicates,
        "orphan_outcomes": orphans,
    }
    if n_dec and diagnostics["match_rate"] < 0.8:
        # 匹配率过低时结论只能算局部样本观察，必须显式警告而不是继续算 p 值
        log.warning(
            "outcome_attribution.low_match_rate",
            match_rate=diagnostics["match_rate"],
            unmatched=diagnostics["unmatched"],
        )
    return rows, diagnostics


def _as_decision(raw: Any) -> DecisionEvent:
    return raw if isinstance(raw, DecisionEvent) else DecisionEvent.from_dict(raw)


def _as_outcome(raw: Any) -> OutcomeEvent:
    return raw if isinstance(raw, OutcomeEvent) else OutcomeEvent.from_dict(raw)


# ======================================================================
# 分桶统计
# ======================================================================


def _metric_value(outcome: OutcomeEvent, metric: str) -> bool | float | None:
    value = getattr(outcome, metric, None)
    return value  # type: ignore[return-value]


def arm_summary(
    rows: Sequence[AttributedDecision], metric: str
) -> dict[str, dict[str, Any]]:
    """按实验臂汇总某个结果指标（比率 + Wilson 区间 + 均值类指标）。

    只统计 join 上的行 —— 未匹配的行没有结果可言，但它们的数量必须
    出现在 diagnostics 里，不能假装不存在。
    """
    buckets: dict[str, list[AttributedDecision]] = defaultdict(list)
    for r in rows:
        if r.outcome is None:
            continue
        buckets[r.decision.arm or "(none)"].append(r)

    summary: dict[str, dict[str, Any]] = {}
    for arm, items in sorted(buckets.items()):
        n = len(items)
        successes = 0
        counted = 0
        util: list[float] = []
        latency: list[float] = []
        for it in items:
            value = _metric_value(it.outcome, metric)  # type: ignore[arg-type]
            if isinstance(value, bool):
                counted += 1
                successes += int(value)
            elif value is not None:
                counted += 1
                successes += float(value)  # type: ignore[assignment]
            if it.outcome.utilization is not None:  # type: ignore[union-attr]
                util.append(it.outcome.utilization)  # type: ignore[union-attr]
            if it.outcome.filled_latency_hours is not None:  # type: ignore[union-attr]
                latency.append(it.outcome.filled_latency_hours)  # type: ignore[union-attr]
        rate = successes / counted if counted else 0.0
        if metric in BINARY_METRICS:
            lo, hi = wilson_interval(int(successes), counted)
        else:
            lo, hi = (rate, rate)
        summary[arm] = {
            "n": n,
            "metric": metric,
            "binary": metric in BINARY_METRICS,
            "successes": successes,
            "counted": counted,
            "rate": round(rate, 4),
            "ci_low": round(lo, 4),
            "ci_high": round(hi, 4),
            "avg_utilization": round(sum(util) / len(util), 4) if util else None,
            "avg_filled_latency_hours": (
                round(sum(latency) / len(latency), 2) if latency else None
            ),
        }
    return summary


def mantel_haenszel(
    rows: Sequence[AttributedDecision], metric: str, strata_keys: Sequence[str]
) -> dict[str, Any]:
    """分层校正后的两臂比率差（Mantel-Haenszel 加权）。

    每层内分别算两臂差，再按层权重（该层两臂样本量之调和平均的近似，
    标准 MH 权重 ``n1s*n2s/Ns``）加权合并。与合并比率一致说明构成不影响
    结论；不一致说明存在混杂，此时**以分层校正后为准**。
    """
    strata: dict[tuple[str, ...], dict[str, list[AttributedDecision]]] = defaultdict(
        lambda: {"control": [], "treatment": []}  # type: ignore[dict-item]
    )
    for r in rows:
        if r.outcome is None or r.decision.arm not in ("control", "treatment"):
            continue
        if _metric_value(r.outcome, metric) is None:  # type: ignore[arg-type]
            continue
        key = tuple(r.decision.strata.get(k, "") for k in strata_keys)
        strata[key][r.decision.arm].append(r)  # type: ignore[index]

    num = 0.0
    den = 0.0
    per_stratum: list[dict[str, Any]] = []
    for key, arms in sorted(strata.items()):
        c, t = arms["control"], arms["treatment"]  # type: ignore[index]
        n_c, n_t = len(c), len(t)
        if n_c == 0 or n_t == 0:
            continue
        x_c = _successes(c, metric)
        x_t = _successes(t, metric)
        p_c, p_t = x_c / n_c, x_t / n_t
        weight = n_c * n_t / (n_c + n_t)
        num += weight * (p_t - p_c)
        den += weight
        per_stratum.append(
            {
                "strata": list(key),
                "n_control": n_c,
                "n_treatment": n_t,
                "rate_control": round(p_c, 4),
                "rate_treatment": round(p_t, 4),
                "delta": round(p_t - p_c, 4),
            }
        )

    adjusted = num / den if den > 0 else 0.0
    return {
        "method": "mantel_haenszel",
        "strata_keys": list(strata_keys),
        "adjusted_delta": round(adjusted, 4),
        "weight_total": round(den, 2),
        "n_strata_used": len(per_stratum),
        "per_stratum": per_stratum,
    }


def _successes(items: Sequence[AttributedDecision], metric: str) -> float:
    total = 0.0
    for it in items:
        value = _metric_value(it.outcome, metric)  # type: ignore[arg-type]
        if isinstance(value, bool):
            total += int(value)
        elif value is not None:
            total += float(value)
    return total


def _counts(items: Sequence[AttributedDecision], metric: str) -> tuple[int, int]:
    """返回 (成功数取整, 有效样本数) —— 仅对二值指标有意义。"""
    successes = 0
    counted = 0
    for it in items:
        value = _metric_value(it.outcome, metric)  # type: ignore[arg-type]
        if isinstance(value, bool):
            counted += 1
            successes += int(value)
    return successes, counted


# ======================================================================
# 报告
# ======================================================================


def outcome_report(
    decisions: Iterable[dict[str, Any] | DecisionEvent],
    outcomes: Iterable[dict[str, Any] | OutcomeEvent],
    *,
    metric: str = "filled",
    strata_keys: Sequence[str] = (),
    min_effect: float = 0.02,
    alpha: float = 0.05,
    min_samples: int = 100,
    guardrail_metrics: Sequence[str] = (),
) -> dict[str, Any]:
    """完整结果归因报告：join 诊断 + 分桶指标 + 显著性 + 混杂校正 + 门禁。

    Args:
        decisions / outcomes: 两张事件表（dict 或 dataclass 均可）。
        metric: 北极星指标字段名（``filled`` / ``responded`` / ``utilization``…）。
        strata_keys: 分层维度（如 ``("urgency",)``）。为空则跳过混杂校正。
        min_effect: 值得上线的最小绝对提升（比率指标上是百分点）。
        alpha: 显著性水平。
        min_samples: 每臂最小样本量（低于则判 hold）。
        guardrail_metrics: 红线指标 —— 任一指标在 treatment 上明显恶化即回滚。

    Returns:
        可直接 JSON 序列化的报告 dict。
    """
    rows, diagnostics = join_decisions_outcomes(decisions, outcomes)
    summary = arm_summary(rows, metric)
    control = summary.get("control")
    treatment = summary.get("treatment")

    report: dict[str, Any] = {
        "metric": metric,
        "diagnostics": diagnostics,
        "arms": summary,
        "significance": None,
        "confounding": None,
        "guardrails": [],
        "verdict": None,
        "warnings": [],
    }

    if diagnostics["match_rate"] < 0.8:
        report["warnings"].append(
            f"match_rate={diagnostics['match_rate']} < 0.8："
            "结论只覆盖可匹配的样本，缺失本身可能有偏"
        )
    if diagnostics["duplicate_outcomes"]:
        report["warnings"].append(
            f"duplicate_outcomes={diagnostics['duplicate_outcomes']}："
            "同一 decision_id 多条结果已按首条去重，需核对上游事件粒度"
        )

    if not control or not treatment:
        report["warnings"].append("两臂样本不全，无法比较")
        return report

    binary = metric in BINARY_METRICS
    if binary:
        rows_c = [r for r in rows if r.outcome and r.decision.arm == "control"]
        rows_t = [
            r for r in rows if r.outcome and r.decision.arm == "treatment"
        ]
        x_c, n_c = _counts(rows_c, metric)
        x_t, n_t = _counts(rows_t, metric)
        test = two_proportion_test(x_c, n_c, x_t, n_t, alpha=alpha).__dict__
        delta = float(test["rate_b"]) - float(test["rate_a"])
        p_value = float(test["p_value"])
        ci_low, ci_high = float(test["ci_low"]), float(test["ci_high"])
    else:
        # 连续指标（utilization / latency）：两独立样本均值差的自助检验
        vals_c = _numeric_values(rows, metric, "control")
        vals_t = _numeric_values(rows, metric, "treatment")
        delta, p_value, ci_low, ci_high = _unpaired_bootstrap(
            vals_c, vals_t, alpha=alpha
        )
        test = {
            "method": "unpaired_bootstrap",
            "n_a": len(vals_c),
            "n_b": len(vals_t),
            "rate_a": round(sum(vals_c) / len(vals_c), 4) if vals_c else 0.0,
            "rate_b": round(sum(vals_t) / len(vals_t), 4) if vals_t else 0.0,
        }
    report["significance"] = {
        **test,
        "delta": round(delta, 4),
        "p_value": round(p_value, 6),
        "ci_low": round(ci_low, 4),
        "ci_high": round(ci_high, 4),
        "significant": p_value < alpha,
    }

    if strata_keys and binary:
        mh = mantel_haenszel(rows, metric, strata_keys)
        report["confounding"] = mh
        pooled = report["significance"]["delta"]
        if mh["n_strata_used"] and _sign_flips(pooled, mh["adjusted_delta"]):
            report["warnings"].append(
                f"合并差 {pooled:+.4f} 与分层校正差 {mh['adjusted_delta']:+.4f} "
                "方向相反：存在强混杂，结论以校正后为准"
            )

    for guard in guardrail_metrics:
        g = _guardrail_check(rows, guard, alpha=alpha)
        if g is not None:
            report["guardrails"].append(g)

    violations = [
        f"{g['metric']}: {g['rate_treatment']} vs {g['rate_control']} (p={g['p_value']})"
        for g in report["guardrails"]
        if g["worse"]
    ]
    effective_delta = delta
    if (
        report["confounding"]
        and report["confounding"]["n_strata_used"]
        and binary
    ):
        effective_delta = report["confounding"]["adjusted_delta"]

    verdict = judge_experiment(
        metric=metric,
        delta=effective_delta,
        p_value=p_value,
        n=min(int(control["n"]), int(treatment["n"])),
        min_effect=min_effect,
        alpha=alpha,
        min_samples=min_samples,
        ci_low=ci_low,
        ci_high=ci_high,
        guardrail_violations=violations,
    )
    report["verdict"] = verdict.to_dict()
    return report


def _numeric_values(
    rows: Sequence[AttributedDecision], metric: str, arm: str
) -> list[float]:
    out: list[float] = []
    for r in rows:
        if r.outcome is None or r.decision.arm != arm:
            continue
        value = _metric_value(r.outcome, metric)  # type: ignore[arg-type]
        if isinstance(value, bool):
            out.append(float(value))
        elif value is not None:
            out.append(float(value))
    return out


def _unpaired_bootstrap(
    a: Sequence[float], b: Sequence[float], *, alpha: float, seed: int = 20260923
) -> tuple[float, float, float, float]:
    """两独立样本均值差的自助检验（连续结果指标用）。

    不能直接用 paired_bootstrap：两臂是不同人群，没有配对关系，
    按配对处理会把「同一用例」的假设强加到无关样本上。
    """
    if not a or not b:
        return (0.0, 1.0, 0.0, 0.0)
    observed = (sum(b) / len(b)) - (sum(a) / len(a))
    rng = random.Random(seed)
    count = 2000
    boot: list[float] = []
    for _ in range(count):
        ma = sum(a[rng.randrange(len(a))] for _ in range(len(a))) / len(a)
        mb = sum(b[rng.randrange(len(b))] for _ in range(len(b))) / len(b)
        boot.append(mb - ma)
    boot_sorted = sorted(boot)
    q = alpha / 2.0
    lo = _percentile(boot_sorted, q)
    hi = _percentile(boot_sorted, 1.0 - q)
    # 平移至零假设下再算双侧尾概率
    tail = sum(1 for x in boot_sorted if abs(x - observed) >= abs(observed))
    p_value = min(1.0, (tail + 1) / (count + 1))
    return (observed, p_value, lo, hi)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return float(sorted_values[lo]) * (1 - frac) + float(sorted_values[hi]) * frac


def _sign_flips(a: float, b: float, *, eps: float = 1e-9) -> bool:
    return (a > eps and b < -eps) or (a < -eps and b > eps)


def _guardrail_check(
    rows: Sequence[AttributedDecision], metric: str, *, alpha: float
) -> dict[str, Any] | None:
    """红线指标：treatment 是否**显著更差**（更差才计入回滚理由）。"""
    c = [r for r in rows if r.outcome and r.decision.arm == "control"]
    t = [r for r in rows if r.outcome and r.decision.arm == "treatment"]
    x_c, n_c = _counts(c, metric)
    x_t, n_t = _counts(t, metric)
    if n_c == 0 or n_t == 0:
        return None
    test = two_proportion_test(x_c, n_c, x_t, n_t, alpha=alpha)
    return {
        "metric": metric,
        "rate_control": round(test.rate_a, 4),
        "rate_treatment": round(test.rate_b, 4),
        "delta": round(test.delta, 4),
        "p_value": round(test.p_value, 6),
        "worse": bool(test.delta < 0 and test.p_value < alpha),
    }


# ======================================================================
# A/A 负对照
# ======================================================================


def aa_negative_control(
    decisions: Iterable[dict[str, Any] | DecisionEvent],
    outcomes: Iterable[dict[str, Any] | OutcomeEvent],
    *,
    arm: str = "control",
    metric: str = "filled",
    seed: int = 20260923,
    alpha: float = 0.05,
    splits: int = 20,
) -> dict[str, Any]:
    """A/A 负对照 —— 把同一臂随机劈两半多次，检验应当查不出差异。

    为什么要劈多次：单次劈半的 p 值本身是随机的，一次「通过」说明不了
    什么（真实显著率 5% 时也有约 5% 概率误报）。多次劈半看**显著率**
    是否接近 alpha，才真正检验了方差估计是否可信 —— 单位错配、同一候选
    被重复计入、分桶与结果间存在非实验关联，都会让显著率明显高于 alpha。

    用途：任何结果指标上线前先过这一关。显著率失控说明分析或分流管道
    在自造效应，此时真实实验的结论一律不可信。

    Args:
        decisions: 决策事件（只用指定 arm 的行）。
        outcomes: 结果事件。
        arm: 取哪一臂做劈半（默认对照臂 —— 它没被任何干预影响）。
        metric: 二值指标名。
        seed: 随机种子（报告可复现）。
        alpha: 显著性水平。
        splits: 劈半次数。

    Returns:
        ``{"n_a","n_b","significant_splits","significant_rate","median_p",
        "passed","note",...}``；``passed=True`` 表示显著率未明显超出 alpha。
    """
    rows, _diag = join_decisions_outcomes(decisions, outcomes)
    pool = [
        _metric_value(r.outcome, metric)  # type: ignore[arg-type]
        for r in rows
        if r.outcome is not None and r.decision.arm == arm
    ]
    values = [v for v in pool if isinstance(v, bool)]
    if len(values) < 40 or splits < 1:
        return {
            "method": "aa_negative_control",
            "arm": arm,
            "metric": metric,
            "passed": False,
            "reason": f"样本不足（{len(values)} 条）或 splits<1，无法执行劈半",
            "splits": 0,
            "n": len(values),
        }

    rng = random.Random(seed)
    p_values: list[float] = []
    significant = 0
    for _ in range(splits):
        sa = ca = sb = cb = 0
        for v in values:
            if rng.random() < 0.5:
                ca += 1
                sa += int(v)
            else:
                cb += 1
                sb += int(v)
        if ca == 0 or cb == 0:
            continue
        test = two_proportion_test(sa, ca, sb, cb, alpha=alpha)
        p_values.append(test.p_value)
        if test.p_value < alpha:
            significant += 1

    if not p_values:
        return {
            "method": "aa_negative_control",
            "arm": arm,
            "metric": metric,
            "passed": False,
            "reason": "所有劈半都出现空组",
            "splits": splits,
        }
    rate = significant / len(p_values)
    ordered = sorted(p_values)
    median_p = ordered[len(ordered) // 2]
    # 容差放宽到 2×alpha：splits 只有几十次时显著率本身就是粗的
    passed = rate <= 2 * alpha
    return {
        "method": "aa_negative_control",
        "arm": arm,
        "metric": metric,
        "splits": len(p_values),
        "n_per_split": len(values),
        "significant_splits": significant,
        "significant_rate": round(rate, 4),
        "median_p": round(median_p, 4),
        "alpha": alpha,
        "passed": bool(passed),
        "note": (
            "A/A 的显著率应接近 alpha；明显更高说明方差被低估"
            "（单位错配 / 重复计数 / 分流有偏），真实实验结论不可信"
        ),
    }
