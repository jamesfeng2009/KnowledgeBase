"""
检索结果时间新鲜度处理 — 单一职责：recency 加权与生效窗口过滤。

两个机制（对应"新旧规范冲突"场景）：
    1. ``filter_by_validity_window``：规范类文档支持生效/失效时间窗口
       （``effective_from`` / ``effective_to``），窗口外文档在检索层硬过滤；
       未配置窗口的文档永远有效（向后兼容）。
    2. ``apply_recency_boost``：重排分数相近（tie band 内）的文档，
       按 ``updated_at`` 新鲜度重新排序，新版本优先；分数差距超过
       tie band 的文档顺序不受影响 —— 新鲜度只做"平局裁决"，不喧宾夺主。

遵循开闭原则：通过 RECENCY_* 配置开关，不修改调用方逻辑。
遵循优雅降级：时间字段缺失/解析失败时按最旧处理，不影响召回。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

_LN2: float = math.log(2)
_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)


def _parse_ts(value: Any) -> datetime | None:
    """解析时间字段（ISO 字符串 / datetime / epoch 秒），失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def filter_by_validity_window(
    docs: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """按生效/失效时间窗口硬过滤检索结果。

    规则：
        - ``effective_from`` 存在且 > now → 尚未生效，过滤；
        - ``effective_to`` 存在且 < now → 已失效，过滤；
        - 字段缺失或解析失败 → 视为永久有效，保留（向后兼容）。
    """
    if not docs:
        return docs
    now = now or datetime.now(tz=UTC)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for doc in docs:
        eff_from = _parse_ts(doc.get("effective_from"))
        eff_to = _parse_ts(doc.get("effective_to"))
        if eff_from is not None and eff_from > now:
            dropped += 1
            continue
        if eff_to is not None and eff_to < now:
            dropped += 1
            continue
        kept.append(doc)
    if dropped:
        log.info(
            "recency.validity_filtered",
            dropped=dropped,
            kept=len(kept),
        )
    return kept


def apply_recency_boost(
    docs: list[dict[str, Any]],
    *,
    tie_band: float | None = None,
    half_life_days: float | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """对分数相近的检索结果按新鲜度做平局裁决（tie-break）。

    算法：
        1. 按 score 降序排列；
        2. 分组：与前一名分差 <= tie_band 的文档归入同一平局组；
        3. 组内按 ``updated_at`` 降序（新版本在前），组间顺序不变；
        4. 为每条结果标注 ``recency_boost``（半衰期衰减系数，可观测）。

    Args:
        docs: 重排后的检索结果（每项含 score / updated_at）。
        tie_band: 平局带宽（分数差），缺省读配置 RECENCY_TIE_BAND。
        half_life_days: 新鲜度半衰期（天），缺省读配置 RECENCY_HALF_LIFE_DAYS。
        now: 当前时间（测试注入用）。

    Returns:
        重排后的结果列表（新列表，不改入参）。
    """
    if not docs:
        return docs
    settings = get_settings()
    if tie_band is None:
        tie_band = float(getattr(settings, "RECENCY_TIE_BAND", 0.02))
    if half_life_days is None:
        half_life_days = float(getattr(settings, "RECENCY_HALF_LIFE_DAYS", 180.0))
    now = now or datetime.now(tz=UTC)

    # 标注衰减系数（可观测性：Langfuse/日志可见每条结果的新鲜度权重）
    enriched: list[dict[str, Any]] = []
    for doc in docs:
        item = dict(doc)
        ts = _parse_ts(item.get("updated_at"))
        if ts is not None and half_life_days > 0:
            age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
            item["recency_boost"] = round(
                math.exp(-_LN2 * age_days / half_life_days), 4
            )
        else:
            item["recency_boost"] = 0.0
        enriched.append(item)

    # 按 score 降序后按 tie band 分组
    sorted_docs = sorted(
        enriched, key=lambda d: float(d.get("score") or 0.0), reverse=True
    )
    groups: list[list[dict[str, Any]]] = []
    for doc in sorted_docs:
        if groups:
            prev_score = float(groups[-1][0].get("score") or 0.0)
            # epsilon 容差：0.90-0.88 浮点误差 4e-18 不应突破 tie band
            if prev_score - float(doc.get("score") or 0.0) <= tie_band + 1e-9:
                groups[-1].append(doc)
                continue
        groups.append([doc])

    # 组内按 updated_at 降序（缺失时间按最旧处理，排最后）
    out: list[dict[str, Any]] = []
    for group in groups:
        group.sort(
            key=lambda d: _parse_ts(d.get("updated_at")) or _EPOCH,
            reverse=True,
        )
        out.extend(group)
    return out
