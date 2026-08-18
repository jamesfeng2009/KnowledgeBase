"""
记忆遗忘 — 激活值策略（机制一：召回时实时算）。

记忆的第三种动作是遗忘，遗忘不是删除：
- 机制一（本模块）：召回时实时算激活值 — 时间衰减管"老不老"，
  频率增益和近期增益管"活不活"，低于地板值当场跳过。
- 机制二（conflict_arbiter.py）：写入时增量整合 — 语义冲突的旧记忆退场。

设计对齐 ACT-R 认知模型：
    激活值 = 基础衰减（时间） + 频率增益（访问次数） + 近期增益（上次访问距今）
你天天用的密码十年不忘；十年没用过的旧手机号早忘光了。

纯函数约定：不写库、不缓存 — 激活值随访问频次动态变化，缓存会带来
一致性与刷新负担；现算用一点 CPU 换架构简单。

类别分组（项目 category → 课程 memory_type 语义映射）：
- preference / fact / entity：长期记忆。无 TTL 时时间不衰减（返回 1.0），
  遗忘只走冲突路径（机制二）。偏好另有滚动 TTL 管物理过期（被召回即续命）。
- working / summary / detail：情节类记忆（episodic 语义），有 TTL，
  走完整三因子衰减。
"""

import math
from datetime import datetime
from typing import Any, Protocol

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 长期记忆类别：无 TTL 时不参与时间衰减（遗忘只走冲突路径）
_EVERGREEN_CATEGORIES = frozenset({"preference", "fact", "entity"})


def _as_datetime(value: Any) -> datetime | None:
    """容错转换时间值（datetime / ISO 字符串 / None），统一为 UTC naive。

    TIMESTAMPTZ 列经 asyncpg 读回是 tz-aware（UTC），与调用方的
    datetime.utcnow()（naive）直接相减会抛 TypeError — 统一剥离 tzinfo。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except (TypeError, ValueError):
        return None


class ActivationPolicy(Protocol):
    """召回时实时算的激活值。纯函数 — 不写、不缓存。"""

    def activation(self, fact: Any, now: datetime) -> float:
        """计算一条记忆的激活值，范围 [0, 1]。

        Args:
            fact: MemoryFact（或鸭子类型 mock），需提供 category /
                created_at / expires_at / access_count / last_accessed_at。
            now: 当前时间。
        """
        ...


class DefaultActivation:
    """ACT-R 三因子：base_decay + frequency_gain + recency_gain。"""

    def __init__(
        self,
        freq_weight: float | None = None,
        recency_window_days: int | None = None,
        recency_boost: float | None = None,
        default_ttl_days: float = 7.0,
    ):
        settings = get_settings()
        self._freq_weight = (
            settings.MEMORY_ACTIVATION_FREQ_WEIGHT if freq_weight is None else freq_weight
        )
        self._recency_window_days = (
            settings.MEMORY_ACTIVATION_RECENCY_WINDOW_DAYS
            if recency_window_days is None
            else recency_window_days
        )
        self._recency_boost = (
            settings.MEMORY_ACTIVATION_RECENCY_BOOST if recency_boost is None else recency_boost
        )
        self._default_ttl_days = default_ttl_days

    def activation(self, fact: Any, now: datetime) -> float:
        category = getattr(fact, "category", None)
        expires_at = _as_datetime(getattr(fact, "expires_at", None))

        # 长期记忆无 TTL：时间不衰减，永远满权上场。
        # 偏好的遗忘只走冲突路径，时间管不了它。
        if category in _EVERGREEN_CATEGORIES and expires_at is None:
            return 1.0

        ttl_days = self._ttl_days(fact, expires_at)

        created_at = _as_datetime(getattr(fact, "created_at", None))
        if created_at is None:
            return 1.0  # 无法计算年龄，保守给满权

        age_days = max((now - created_at).total_seconds() / 86400.0, 0.0)
        base = math.exp(-age_days / max(ttl_days, 1.0))  # 时间衰减
        frequency = self._frequency(getattr(fact, "access_count", 0))  # 频率增益
        recency = self._recency(
            _as_datetime(getattr(fact, "last_accessed_at", None)), now
        )  # 近期增益
        return min(1.0, base + frequency + recency)

    def _ttl_days(self, fact: Any, expires_at: datetime | None) -> float:
        """从 expires_at - created_at 推导衰减尺度；情节类无显式 TTL 用默认尺度。

        这里的 TTL 只决定遗忘速度，不是物理过期 — 物理清理仍只认
        expires_at（cleanup_expired），无 TTL 的事实不会被删除。
        """
        if expires_at is None:
            return self._default_ttl_days
        created_at = _as_datetime(getattr(fact, "created_at", None))
        if created_at is None:
            return self._default_ttl_days
        ttl_days = (expires_at - created_at).total_seconds() / 86400.0
        return ttl_days if ttl_days > 0 else self._default_ttl_days

    def _frequency(self, access_count: int | None) -> float:
        """频率增益：log(1 + n) * w，对数递减收益 — 第 100 次访问的边际收益远小于第 1 次。"""
        n = access_count or 0
        if n <= 0:
            return 0.0
        return math.log1p(n) * self._freq_weight

    def _recency(self, last_accessed_at: datetime | None, now: datetime) -> float:
        """近期增益：窗口内被召回过额外续命，窗口内线性衰减到 0。"""
        if last_accessed_at is None or self._recency_window_days <= 0:
            return 0.0
        days_since = (now - last_accessed_at).total_seconds() / 86400.0
        if days_since < 0:
            days_since = 0.0
        if days_since >= self._recency_window_days:
            return 0.0
        remaining = 1.0 - days_since / self._recency_window_days
        return self._recency_boost * remaining


def default_activation() -> DefaultActivation:
    """工厂：按配置构造默认激活值策略。"""
    return DefaultActivation()
