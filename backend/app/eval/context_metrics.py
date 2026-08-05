"""
上下文质量四类分数 — 评测.md §7.3 / §9.2。

    | 分数       | 含义                     | 低分调试动作           |
    |------------|--------------------------|------------------------|
    | Recall     | 必要信息是否进入 Context | 补检索和必读规则       |
    | Precision  | 无关信息是否受控         | 改过滤和摘要           |
    | Freshness  | 是否使用当前版本         | 检查版本与缓存         |
    | Robustness | 压缩后是否稳定           | 查 compact 和子任务隔离 |

计算输入：
    - ContextTraceRecord（from_spans 聚合的实际上下文证据）；
    - case 级期望 context_expect（p4_context.jsonl 七类样本的判定字段）：
        required_files        — 必须进入 Context 的引用（必读文件样本）
        distractor_files      — 不应进入 Context 的干扰引用（干扰文件样本）
        forbidden_files       — 严禁进入 Context 的引用（比 distractor 更硬）
        stale_refs            — 旧版本引用，进入则 Freshness 不合格
        required_after_compact — 压缩后必须保留的约束引用（Compact 样本）

无对应期望字段时该维度默认满分（视为不适用），全部期望为空时返回 None。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.eval.context_trace import ContextTraceRecord


@dataclass
class ContextMetrics:
    """上下文质量四类分数（§9.2）。"""

    recall: float = 1.0
    precision: float = 1.0
    freshness: float = 1.0
    robustness: float = 1.0

    def to_dict(self) -> dict[str, float]:
        return {
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "freshness": round(self.freshness, 4),
            "robustness": round(self.robustness, 4),
        }


def _ratio_hit(hit: int, total: int) -> float:
    """命中率：total 为 0 时返回 1.0（不适用维度按满分处理）。"""
    if total <= 0:
        return 1.0
    return hit / total


def compute_context_metrics(
    record: ContextTraceRecord,
    expect: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """计算上下文四类分数。

    Args:
        record: 上下文选择证据记录。
        expect: case 级期望（见模块 docstring 字段说明）。

    Returns:
        含四类分数与失败明细的字典；expect 为空或无任何已知字段时返回 None。

        返回结构::

            {
                "recall": float, "precision": float,
                "freshness": float, "robustness": float,
                "missing_required": [...],       # 未进入 Context 的必读引用
                "included_distractors": [...],   # 误入 Context 的干扰引用
                "included_stale": [...],         # 误入 Context 的旧版本引用
                "lost_after_compact": [...],     # 压缩后丢失的约束引用
                "passed": bool,                  # 四类分数全部满分
            }
    """
    if not expect:
        return None

    required = [str(r) for r in expect.get("required_files") or [] if r]
    distractors = [str(d) for d in expect.get("distractor_files") or [] if d]
    forbidden = [str(f) for f in expect.get("forbidden_files") or [] if f]
    stale = [str(s) for s in expect.get("stale_refs") or [] if s]
    required_after_compact = [
        str(r) for r in expect.get("required_after_compact") or [] if r
    ]

    if not any([required, distractors, forbidden, stale, required_after_compact]):
        return None

    included = set(record.context_included_refs)

    # Recall：必读引用进入率
    missing_required = [r for r in required if r not in included]
    recall = _ratio_hit(len(required) - len(missing_required), len(required))

    # Precision：干扰/严禁引用控制率（forbidden 与 distractor 同等计入）
    noise = distractors + forbidden
    included_distractors = [d for d in noise if d in included]
    precision = _ratio_hit(len(noise) - len(included_distractors), len(noise))

    # Freshness：旧版本引用控制率
    included_stale = [s for s in stale if s in included]
    freshness = _ratio_hit(len(stale) - len(included_stale), len(stale))

    # Robustness：压缩后约束保留率（无压缩事件时视为不适用）
    # P2-8: preserved_refs 由 engine 的 context.compact span 写入（压缩后仍
    # 出现在上下文消息中的文档引用）。区分两种情况：
    #   - compaction_event 含 preserved_refs 键：按实际保留集判定（空集=全丢，robustness=0）
    #   - compaction_event 缺 preserved_refs 键：engine 未插桩，无法判定，
    #     标记 robustness_unknown 并保持 1.0（避免误报全部丢失的假回归）
    lost_after_compact: list[str] = []
    robustness_unknown = False
    if record.compaction_events and required_after_compact:
        preserved: set[str] = set()
        has_preserved_refs_key = False
        for event in record.compaction_events:
            if "preserved_refs" in event:
                has_preserved_refs_key = True
                for ref in event.get("preserved_refs") or []:
                    preserved.add(str(ref))
        if not has_preserved_refs_key:
            # engine 未写 preserved_refs —— 无法评估压缩保留率，显式标记未知
            robustness_unknown = True
            robustness = 1.0
        else:
            lost_after_compact = [
                r for r in required_after_compact if r not in preserved
            ]
            robustness = _ratio_hit(
                len(required_after_compact) - len(lost_after_compact),
                len(required_after_compact),
            )
    else:
        robustness = 1.0

    metrics = ContextMetrics(
        recall=recall,
        precision=precision,
        freshness=freshness,
        robustness=robustness,
    )
    passed = (
        not missing_required
        and not included_distractors
        and not included_stale
        and not lost_after_compact
    )

    result: dict[str, Any] = {
        **metrics.to_dict(),
        "missing_required": missing_required,
        "included_distractors": included_distractors,
        "included_stale": included_stale,
        "lost_after_compact": lost_after_compact,
        "passed": passed,
    }
    if robustness_unknown:
        result["robustness_unknown"] = True
    return result
