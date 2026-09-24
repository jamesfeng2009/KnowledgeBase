"""
评测结果的显著性检验 — 单一职责：回答「这个指标差是真的更好，还是抽样噪声」。

为什么需要这一层：
    离线评测与每日回归给出的都是**点估计**（avg_recall 0.71 vs 0.72）。
    100 条用例上 1 个点的差距，绝大多数情况下完全在噪声范围内；把它当成
    改进去 ship，等于用掷骰子决定线上行为。JD 里那句「有把握地 ship
    prompt 与编排变更」，缺的就是这一步。

三种检验，按数据形态选：
    1. :func:`paired_bootstrap` —— 连续指标（recall/MRR/judge 分）的**配对**
       自助检验。配对是关键：同一批用例在两个系统上的得分高度相关，
       按独立两样本处理会把系统间差异淹没在用例难度差异里。
    2. :func:`mcnemar_test` —— 通过/失败这类**配对二值**结果。只看不一致的
       两个格子（A过B挂、A挂B过），对样本量效率远高于比较通过率。
    3. :func:`two_proportion_test` —— **独立**两组的比率（线上两桶的转化率）。
       与 McNemar 的区别：这里没有配对关系，只有两个桶。

判定门禁（:func:`judge_experiment`）刻意要求同时满足两件事：
    - 统计显著（p < alpha）：差异不太可能由噪声解释；
    - 效应够大（点差 ≥ min_effect）：差异值得为之上线。
    只看 p 值会出现「n 够大所以 0.1% 也显著」的荒谬结论；只看均值差会出现
    「+5% 但置信区间跨 0」的误判。两个都不过才放行。

实现约束：纯标准库（无 numpy/scipy），确定性（显式 seed），
零外部服务依赖 —— 与本项目「评测不能依赖外部可用性」的既有约束一致。
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

__all__ = [
    "BootstrapResult",
    "ProportionResult",
    "SignificanceVerdict",
    "judge_experiment",
    "mcnemar_test",
    "paired_bootstrap",
    "two_proportion_test",
    "wilson_interval",
]

#: 自助重采样默认次数 —— 2000 次已能把 p 值分辨到 0.0005 量级，
#: 再多次只是线性增加 CPU（千级用例上仍是毫秒级）。
_DEFAULT_RESAMPLES = 2000

#: 默认显著性水平
DEFAULT_ALPHA = 0.05

#: n 超过该值时二项精确概率的组合数计算成本过高，改用正态近似
_EXACT_BINOMIAL_MAX_N = 1000


# ======================================================================
# 结果数据类
# ======================================================================


@dataclass(frozen=True)
class BootstrapResult:
    """配对自助检验结果。

    Attributes:
        n: 参与配对的样本数（两臂都有值的用例数）。
        mean_a / mean_b: 两臂均值。
        delta: 观测差异（mean_b - mean_a），即「换了 B 之后涨了多少」。
        ci_low / ci_high: delta 的置信区间端点（百分位法）。
        p_value: 双侧 p 值（零假设：delta 真实值为 0）。
        alpha: 本次使用的显著性水平。
        resamples: 实际执行的重采样次数。
        method: 检验名（写入报告，便于事后追溯口径）。
    """

    n: int
    mean_a: float
    mean_b: float
    delta: float
    ci_low: float
    ci_high: float
    p_value: float
    alpha: float = DEFAULT_ALPHA
    resamples: int = _DEFAULT_RESAMPLES
    method: str = "paired_bootstrap"

    @property
    def significant(self) -> bool:
        """是否统计显著（p < alpha，或 CI 不含 0 的等价判据）。"""
        return self.p_value < self.alpha

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "n": self.n,
            "mean_a": round(self.mean_a, 4),
            "mean_b": round(self.mean_b, 4),
            "delta": round(self.delta, 4),
            "ci_low": round(self.ci_low, 4),
            "ci_high": round(self.ci_high, 4),
            "p_value": round(self.p_value, 6),
            "alpha": self.alpha,
            "resamples": self.resamples,
            "significant": self.significant,
        }


@dataclass(frozen=True)
class ProportionResult:
    """两独立样本比率检验结果（含两比例的差值置信区间）。

    Attributes:
        rate_a / rate_b: 两组比率。
        delta: rate_b - rate_a。
        z: 合并方差下的 z 统计量。
        p_value: 双侧 p 值。
        ci_low / ci_high: delta 的置信区间（未合并方差，Wald）。
        n_a / n_b: 两组样本量。
        method: 检验名。
    """

    rate_a: float
    rate_b: float
    delta: float
    z: float
    p_value: float
    ci_low: float
    ci_high: float
    n_a: int
    n_b: int
    method: str = "two_proportion_z"

    @property
    def significant(self) -> bool:
        """差异是否在默认水平上显著。"""
        return self.p_value < DEFAULT_ALPHA

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "n_a": self.n_a,
            "n_b": self.n_b,
            "rate_a": round(self.rate_a, 4),
            "rate_b": round(self.rate_b, 4),
            "delta": round(self.delta, 4),
            "z": round(self.z, 4),
            "p_value": round(self.p_value, 6),
            "ci_low": round(self.ci_low, 4),
            "ci_high": round(self.ci_high, 4),
            "significant": self.p_value < DEFAULT_ALPHA,
        }


@dataclass(frozen=True)
class SignificanceVerdict:
    """实验判定结论（门禁产物）。

    Attributes:
        decision: ship / hold / rollback —— 见 :func:`judge_experiment` 规则。
        reasons: 逐条不通过原因（空表示无阻碍）。
        metric: 被考察的指标名。
        delta / p_value / ci_low / ci_high: 关键数字快照。
        min_effect: 本次要求的最小效应。
        enough_evidence: 样本量与显著性是否足以支撑结论。
        extra: 附加上下文（如各桶样本数）。
    """

    decision: str
    reasons: list[str] = field(default_factory=list)
    metric: str = ""
    delta: float = 0.0
    p_value: float = 1.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    min_effect: float = 0.0
    enough_evidence: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "metric": self.metric,
            "delta": round(self.delta, 4),
            "p_value": round(self.p_value, 6),
            "ci_low": round(self.ci_low, 4),
            "ci_high": round(self.ci_high, 4),
            "min_effect": self.min_effect,
            "enough_evidence": self.enough_evidence,
            "extra": dict(self.extra),
        }


# ======================================================================
# 分布工具（纯标准库）
# ======================================================================


def _normal_cdf(x: float) -> float:
    """标准正态分布 CDF（用 math.erf，无需 scipy）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """线性插值百分位数（q ∈ [0,1]，输入需已升序）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return float(sorted_values[lo]) * (1 - frac) + float(sorted_values[hi]) * frac


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ======================================================================
# 配对自助检验（连续指标）
# ======================================================================


def paired_bootstrap(
    scores_a: Sequence[float],
    scores_b: Sequence[float],
    *,
    resamples: int = _DEFAULT_RESAMPLES,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 20260923,
) -> BootstrapResult:
    """配对自助法检验两组**同用例**得分的差异是否显著。

    做法：对 n 个用例的成对差值 d_i = b_i - a_i 有放回重采样，统计均值差；
    把重采样分布平移 -observed_delta 得到零假设分布，用双侧比例计 p 值。
    平移是必要的：直接数「重采样 delta ≤ 0」会把观测到的偏差本身当成
    零假设的一部分，导致 p 值系统性偏大（保守到失真）。

    Args:
        scores_a: 对照臂得分，按用例顺序。
        scores_b: 实验臂得分，与 scores_a **同长度同顺序**（配对前提）。
        resamples: 重采样次数。
        alpha: 显著性水平。
        seed: 随机种子 —— 评测报告必须可复现，默认固定。

    Returns:
        :class:`BootstrapResult`；配对样本不足 2 条时返回 p=1.0 的空结论。

    Raises:
        ValueError: 两臂长度不等（说明上游配对逻辑出错，不能静默截断）。
    """
    if len(scores_a) != len(scores_b):
        raise ValueError(
            f"paired test needs equal lengths, got {len(scores_a)} vs {len(scores_b)}"
        )
    n = len(scores_a)
    mean_a, mean_b = _mean(scores_a), _mean(scores_b)
    if n < 2:
        return BootstrapResult(
            n=n,
            mean_a=mean_a,
            mean_b=mean_b,
            delta=mean_b - mean_a,
            ci_low=0.0,
            ci_high=0.0,
            p_value=1.0,
            alpha=alpha,
            resamples=0,
        )

    diffs = [float(b) - float(a) for a, b in zip(scores_a, scores_b, strict=True)]
    observed = _mean(diffs)
    rng = random.Random(seed)
    count = max(1, int(resamples))

    # 零假设分布：把每次重采样的均值差减去观测值（等价于强制 delta=0）
    null_deltas: list[float] = []
    for _ in range(count):
        sampled = _mean([diffs[rng.randrange(n)] for _ in range(n)])
        null_deltas.append(sampled - observed)

    # 置信区间用未平移的重采样分布
    ci_values = sorted(null_deltas[i] + observed for i in range(count))
    q = alpha / 2.0
    ci_low = _percentile(ci_values, q)
    ci_high = _percentile(ci_values, 1.0 - q)

    null_sorted = sorted(null_deltas)
    # 加 1 拉普拉斯平滑：重采样次数有限时 p 值不应为 0
    tail = sum(1 for x in null_sorted if abs(x) >= abs(observed))
    p_value = min(1.0, (tail + 1) / (count + 1))

    return BootstrapResult(
        n=n,
        mean_a=mean_a,
        mean_b=mean_b,
        delta=observed,
        ci_low=ci_low,
        ci_high=ci_high,
        p_value=p_value,
        alpha=alpha,
        resamples=count,
    )


# ======================================================================
# McNemar（配对二值）
# ======================================================================


def mcnemar_test(
    passed_a: Sequence[bool],
    passed_b: Sequence[bool],
    *,
    alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """McNemar 检验 —— 配对通过/失败结果的显著性。

    只使用**不一致**的两格：b = A过B挂 的数量，c = A挂B过 的数量。
    零假设下 b 与 c 服从 Binomial(b+c, 0.5)，双侧精确二项检验。
    对「同一批用例、两个系统」这种设计，这是最合适的检验：
    一致的两格（都过/都挂）不含任何区分两臂的信息。

    Args:
        passed_a: 对照臂逐用例是否通过（与 passed_b 等长同序）。
        passed_b: 实验臂逐用例是否通过。
        alpha: 显著性水平。

    Returns:
        ``{"n", "b", "c", "p_value", "significant", "method", "approx"}``；
        无不一致样本（b+c=0）时 p=1.0。
    """
    if len(passed_a) != len(passed_b):
        raise ValueError(
            f"paired test needs equal lengths, got {len(passed_a)} vs {len(passed_b)}"
        )
    b = sum(1 for x, y in zip(passed_a, passed_b, strict=True) if x and not y)
    c = sum(1 for x, y in zip(passed_a, passed_b, strict=True) if y and not x)
    discordant = b + c
    approx = discordant > _EXACT_BINOMIAL_MAX_N
    if discordant == 0:
        p_value = 1.0
    elif approx:
        # 正态近似 + 连续性校正（大样本下精确二项的组合数成本不可接受）
        z = (abs(b - c) - 1.0) / math.sqrt(discordant)
        p_value = min(1.0, 2.0 * (1.0 - _normal_cdf(max(0.0, z))))
    else:
        k = min(b, c)
        tail = sum(math.comb(discordant, i) for i in range(k + 1))
        p_value = min(1.0, 2.0 * tail / (2.0**discordant))
    return {
        "method": "mcnemar",
        "n": len(passed_a),
        "b": b,
        "c": c,
        "p_value": round(p_value, 6),
        "alpha": alpha,
        "significant": p_value < alpha,
        "exact": not approx,
    }


# ======================================================================
# 两独立样本比率 + Wilson 区间（线上分桶结果用）
# ======================================================================


def wilson_interval(
    successes: int, n: int, *, confidence: float = 0.95
) -> tuple[float, float]:
    """比率的 Wilson 置信区间。

    不用 Wald（p ± z·se）：小样本或比率接近 0/1 时 Wald 会给出越界甚至
    负数的区间，而「fill rate 0/12」正是这种场景。
    """
    if n <= 0:
        return (0.0, 0.0)
    z = _z_for_confidence(confidence)
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def _z_for_confidence(confidence: float) -> float:
    """双侧置信度对应的 z 分位（常见值查表 + 其余走数值反解）。"""
    table = {0.80: 1.2816, 0.90: 1.6449, 0.95: 1.9600, 0.99: 2.5758}
    key = round(confidence, 2)
    if key in table:
        return table[key]
    target = 1.0 - (1.0 - confidence) / 2.0
    lo, hi = 0.0, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _normal_cdf(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def two_proportion_test(
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
    *,
    alpha: float = DEFAULT_ALPHA,
) -> ProportionResult:
    """两独立样本比率差异的 z 检验（合并方差）+ 差值置信区间。

    适用：线上两个实验桶各自的转化率/达成率对比 —— 两组之间没有配对关系，
    与 McNemar（同一批用例跑两遍）互斥使用。
    """
    if n_a <= 0 or n_b <= 0:
        rate_a = successes_a / n_a if n_a else 0.0
        rate_b = successes_b / n_b if n_b else 0.0
        return ProportionResult(
            rate_a=rate_a,
            rate_b=rate_b,
            delta=rate_b - rate_a,
            z=0.0,
            p_value=1.0,
            ci_low=0.0,
            ci_high=0.0,
            n_a=n_a,
            n_b=n_b,
        )
    rate_a = successes_a / n_a
    rate_b = successes_b / n_b
    pooled = (successes_a + successes_b) / (n_a + n_b)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    z = (rate_b - rate_a) / se_pooled if se_pooled > 0 else 0.0
    p_value = 2.0 * (1.0 - _normal_cdf(abs(z))) if se_pooled > 0 else 1.0

    # 差值区间用未合并方差（假设检验用合并、区间估计用未合并是标准做法）
    se_unpooled = math.sqrt(
        rate_a * (1 - rate_a) / n_a + rate_b * (1 - rate_b) / n_b
    )
    zc = _z_for_confidence(1 - alpha)
    delta = rate_b - rate_a
    return ProportionResult(
        rate_a=rate_a,
        rate_b=rate_b,
        delta=delta,
        z=z,
        p_value=min(1.0, max(0.0, p_value)),
        ci_low=delta - zc * se_unpooled,
        ci_high=delta + zc * se_unpooled,
        n_a=n_a,
        n_b=n_b,
    )


# ======================================================================
# 配对取数 + 门禁
# ======================================================================


def extract_metric_pairs(
    control_cases: Sequence[dict[str, Any]],
    treatment_cases: Sequence[dict[str, Any]],
    metric: str,
) -> tuple[list[float], list[float], int]:
    """按 case_id（缺省回退 query）对齐两臂的逐用例指标。

    只保留**两臂都有该指标**的用例 —— 单侧缺失的用例不能参与配对，
    否则等于偷偷换了评测集。

    Args:
        control_cases: 对照臂 case 结果 dict 列表（EvalCaseResult.to_dict 形态）。
        treatment_cases: 实验臂 case 结果 dict 列表。
        metric: 指标字段名，如 ``recall_at_5`` / ``mrr`` / ``ndcg_at_5``；
            也支持 ``judge_scores.total`` 这种一层嵌套路径，以及 ``passed``
            这类布尔字段（按 0/1 取值，均值即通过率）。

    Returns:
        ``(scores_a, scores_b, dropped)`` —— dropped 为因单侧缺失或指标不可用
        被剔除的用例数（报告里必须披露，否则读者会以为全量参与）。
    """

    def _key(case: dict[str, Any]) -> str:
        return str(case.get("case_id") or case.get("query") or "")

    def _value(case: dict[str, Any]) -> float | None:
        node: Any = case
        for part in metric.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        if isinstance(node, bool):
            return 1.0 if node else 0.0
        if not isinstance(node, (int, float)):
            return None
        return float(node)

    b_index = {_key(c): c for c in treatment_cases}
    scores_a: list[float] = []
    scores_b: list[float] = []
    dropped = 0
    for ca in control_cases:
        cb = b_index.get(_key(ca))
        if cb is None:
            dropped += 1
            continue
        va, vb = _value(ca), _value(cb)
        if va is None or vb is None:
            dropped += 1
            continue
        scores_a.append(va)
        scores_b.append(vb)
    return scores_a, scores_b, dropped


def extract_passed_pairs(
    control_cases: Sequence[dict[str, Any]],
    treatment_cases: Sequence[dict[str, Any]],
) -> tuple[list[bool], list[bool]]:
    """按 case_id 对齐两臂的逐用例通过标记（供 McNemar 使用）。"""

    def _key(case: dict[str, Any]) -> str:
        return str(case.get("case_id") or case.get("query") or "")

    b_index = {_key(c): c for c in treatment_cases}
    pa: list[bool] = []
    pb: list[bool] = []
    for ca in control_cases:
        cb = b_index.get(_key(ca))
        if cb is None:
            continue
        pa.append(bool(ca.get("passed")))
        pb.append(bool(cb.get("passed")))
    return pa, pb


def judge_experiment(
    *,
    metric: str,
    delta: float,
    p_value: float,
    n: int,
    min_effect: float,
    alpha: float = DEFAULT_ALPHA,
    min_samples: int = 30,
    ci_low: float | None = None,
    ci_high: float | None = None,
    guardrail_violations: Sequence[str] = (),
) -> SignificanceVerdict:
    """实验门禁 —— 把统计结论翻译成 ship / hold / rollback 决策。

    判定规则（按严格程度排序，任一不通过即不能 ship）：
        1. **红线优先**：guardrail_violations 非空 → rollback（哪怕指标大涨）。
           拒答率、安全约束这类指标恶化时，主指标赢多少都不该上线。
        2. **显著变差优先识别**：delta < 0 且显著 → rollback。这类结论是
           「证据表明更差」，不能和「证据不足」混在同一条理由里措辞。
        3. **证据不足**：n < min_samples 或 p ≥ alpha → hold（不是"无效"，
           是"还没证据"，两者必须区分，否则会被误读为实验失败）。
        4. **效应过小**：delta < min_effect → hold（显著但太小，不值得变更成本）。
        5. 以上都过 → ship。

    只看 p 值会出现「n 够大所以 0.1% 也显著」的荒谬结论；只看均值差会出现
    「+5% 但置信区间跨 0」的误判 —— 所以显著性与效应量两项都要过。

    Args:
        metric: 指标名。
        delta: 实验臂相对对照臂的差异（正数表示更好）。
        p_value: 双侧 p 值。
        n: 参与配对的样本数。
        min_effect: 最小可接受效应（业务上值得上线的最小提升）。
        alpha: 显著性水平。
        min_samples: 最小样本量 —— 低于此值任何 p 值都不可信。
        ci_low / ci_high: 差异置信区间（可选，仅用于报告展示）。
        guardrail_violations: 红线指标恶化清单（非空即回滚）。

    Returns:
        :class:`SignificanceVerdict`。
    """
    reasons: list[str] = []
    for item in guardrail_violations:
        reasons.append(f"guardrail violated: {item}")
    if reasons:
        return SignificanceVerdict(
            decision="rollback",
            reasons=reasons,
            metric=metric,
            delta=delta,
            p_value=p_value,
            ci_low=ci_low or 0.0,
            ci_high=ci_high or 0.0,
            min_effect=min_effect,
            enough_evidence=n >= min_samples,
            extra={"n": n},
        )

    significant = p_value < alpha
    if delta < 0 and significant:
        # 方向明确为负且显著 —— 这是「证据表明更差」，与「证据不足」必须分开措辞
        return SignificanceVerdict(
            decision="rollback",
            reasons=[f"{metric} significantly worse: delta={delta:.4f} (p={p_value:.4f})"],
            metric=metric,
            delta=delta,
            p_value=p_value,
            ci_low=ci_low or 0.0,
            ci_high=ci_high or 0.0,
            min_effect=min_effect,
            enough_evidence=n >= min_samples,
            extra={"n": n, "alpha": alpha},
        )

    reasons = []
    if n < min_samples:
        reasons.append(f"insufficient samples: n={n} < min_samples={min_samples}")
    if not significant:
        reasons.append(f"not significant at alpha={alpha}: p={p_value:.4f}")
    if delta < min_effect:
        reasons.append(f"effect too small: delta={delta:.4f} < min_effect={min_effect:.4f}")

    decision = "hold" if reasons else "ship"

    return SignificanceVerdict(
        decision=decision,
        reasons=reasons,
        metric=metric,
        delta=delta,
        p_value=p_value,
        ci_low=ci_low or 0.0,
        ci_high=ci_high or 0.0,
        min_effect=min_effect,
        enough_evidence=n >= min_samples and significant,
        extra={"n": n, "alpha": alpha},
    )
