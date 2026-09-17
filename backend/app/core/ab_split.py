"""
在线灰度 / AB 分流 — 单一职责：把「默认模型」流量按租户/用户维度
确定性地分配到实验组（treatment）或对照组（control）。

与离线 evolution gate（scripts/run_evolution.py）的关系 — 两道闸门互补：
    evolution gate 是「上线前」离线闸门：新 adapter / 新模型必须先通过
    评测指标（Reject@boundary、工作题误拒等）才允许进入实验配置；
    本模块是「上线后」在线分流：通过闸门的候选模型按流量百分比 /
    租户白名单灰度放量，观察线上指标（TTFT / 满意度 / 拒答率），
    出问题改配置即回滚，不需要发版。

设计要点：
  - 配置驱动：``AB_EXPERIMENTS`` 环境变量（JSON 数组），调整比例 /
    增删实验 / 回滚都是改配置重启，零代码变更。
  - 确定性分流（sticky bucketing）：sha256(experiment:sticky_key) % 100
    < traffic_pct → treatment。同一用户永远落在同一桶，保证同一会话
    体验一致、指标可归因（不会一半回答来自 A 模型一半来自 B）。
  - 只分流默认流量：用户显式选择的模型（resolved != control）不劫持
    — 尊重用户选择，实验只作用于「未选择模型的默认路径」。
  - 租户维度灰度：tenant_allowlist 中的租户 100% 进 treatment
    （大客户定向灰度 / 内部租户先行）；tenant_blocklist 中的租户 0%
    （对敏感租户锁死 control）。
  - 曝光埋点：每次命中实验输出 ``ab.exposure`` 日志（experiment /
    bucket / model / tenant / user 维度），供离线按桶聚合
    TTFT（sse.ttft）/ 满意度 / 拒答率指标，形成线上评测闭环。
  - 失败安全：配置 JSON 解析失败 / 字段非法 → 跳过该实验并告警，
    绝不阻断对话主链路。

遵循开闭原则：新增实验只改配置；遵循单一职责：本模块只做分流判定，
不创建 Provider、不感知 RAG。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

__all__ = ["ABExperiment", "apply_ab_split", "reset_experiments_cache"]


@dataclass(frozen=True)
class ABExperiment:
    """一个在线分流实验的配置。

    Attributes:
        name: 实验名（唯一），参与哈希计算 — 更名即等价重开实验。
        control_model: 对照组模型 ID（models.json 中的 id），通常为
            当前线上默认模型。
        treatment_model: 实验组模型 ID — 通过离线 evolution gate 的候选。
        traffic_pct: 实验组流量百分比（0-100）。
        enabled: 实验开关（False 等价 traffic_pct=0，但保留配置）。
        tenant_allowlist: 定向灰度租户 ID 列表 — 100% 进 treatment。
        tenant_blocklist: 排除租户 ID 列表 — 0% 参与（锁死 control）。
        description: 实验说明（供运维/审计阅读）。
    """

    name: str
    control_model: str
    treatment_model: str
    traffic_pct: int
    enabled: bool = True
    tenant_allowlist: tuple[str, ...] = field(default=())
    tenant_blocklist: tuple[str, ...] = field(default=())
    description: str = ""


@lru_cache(maxsize=1)
def _load_experiments() -> tuple[ABExperiment, ...]:
    """解析 AB_EXPERIMENTS 配置 → 实验元组（进程内缓存）。

    容错策略：整体 JSON 非法 → 返回空元组（分流禁用）+ error 日志；
    单个实验字段非法 → 跳过该条 + warning，不影响其他实验。
    """
    raw = get_settings().AB_EXPERIMENTS
    if not raw or not raw.strip():
        return ()

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("ab.config_parse_error", error=str(exc))
        return ()

    if not isinstance(items, list):
        log.error("ab.config_not_list", raw_type=type(items).__name__)
        return ()

    experiments: list[ABExperiment] = []
    for i, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                raise ValueError("experiment must be an object")
            name = str(item["name"]).strip()
            control = str(item["control_model"]).strip()
            treatment = str(item["treatment_model"]).strip()
            traffic = int(item["traffic_pct"])
            if not name or not control or not treatment:
                raise ValueError("name/control_model/treatment_model required")
            if not 0 <= traffic <= 100:
                raise ValueError(f"traffic_pct out of range: {traffic}")
            experiments.append(
                ABExperiment(
                    name=name,
                    control_model=control,
                    treatment_model=treatment,
                    traffic_pct=traffic,
                    enabled=bool(item.get("enabled", True)),
                    tenant_allowlist=tuple(
                        str(t) for t in item.get("tenant_allowlist", []) if t
                    ),
                    tenant_blocklist=tuple(
                        str(t) for t in item.get("tenant_blocklist", []) if t
                    ),
                    description=str(item.get("description", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("ab.config_experiment_skipped", index=i, error=str(exc))
    return tuple(experiments)


def reset_experiments_cache() -> None:
    """清空实验配置缓存 — 配置热更新 / 测试用。"""
    _load_experiments.cache_clear()


def _bucket_of(experiment_name: str, sticky_key: str) -> int:
    """确定性分桶 — 返回 0-99。

    sha256(experiment:sticky_key) 前 8 位十六进制 mod 100。
    experiment_name 参与哈希：不同实验对同一用户的分桶独立，
    避免多个实验间用户分组完全相关的耦合。
    """
    digest = hashlib.sha256(f"{experiment_name}:{sticky_key}".encode("utf-8"))
    return int(digest.hexdigest()[:8], 16) % 100


def apply_ab_split(
    resolved_model_id: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """对已解析的模型 ID 应用在线分流 — 命中实验则返回 treatment 模型。

    分流规则（按序）：
        1. 无启用的实验 / resolved 不是某实验的 control → 原样返回；
        2. tenant 在 blocklist → 原样返回（锁死 control）；
        3. tenant 在 allowlist → 直接 treatment（定向灰度）；
        4. 否则按 sticky 分桶：bucket < traffic_pct → treatment。

    Args:
        resolved_model_id: ``ModelSelectionService.resolve_model`` 的结果
            （session 级选择 > system 默认）。用户显式选择的模型 !=
            control 时不会被劫持。
        tenant_id: 租户 ID（多租户灰度维度，可空）。
        user_id: 用户 ID（sticky 分桶维度，可空；为空时退化为按租户/匿名桶）。

    Returns:
        最终使用的模型 ID（control 原样 / treatment 覆写）。
    """
    if not resolved_model_id:
        return resolved_model_id

    for exp in _load_experiments():
        if not exp.enabled:
            continue
        # 只分流默认流量 — 用户显式选择的模型不劫持
        if resolved_model_id != exp.control_model:
            continue
        # 租户排除 / 定向灰度（白名单优先于 traffic_pct，0% 也可定向放量）
        if tenant_id and tenant_id in exp.tenant_blocklist:
            continue
        if tenant_id and tenant_id in exp.tenant_allowlist:
            log.info(
                "ab.exposure",
                experiment=exp.name,
                bucket=0,
                traffic_pct=exp.traffic_pct,
                chosen_model=exp.treatment_model,
                tenant_id=tenant_id,
                user_id=user_id,
                via="allowlist",
            )
            return exp.treatment_model
        if exp.traffic_pct == 0:
            continue
        sticky_key = user_id or tenant_id or "anonymous"
        bucket = _bucket_of(exp.name, sticky_key)
        chosen = (
            exp.treatment_model if bucket < exp.traffic_pct else exp.control_model
        )
        log.info(
            "ab.exposure",
            experiment=exp.name,
            bucket=bucket,
            traffic_pct=exp.traffic_pct,
            chosen_model=chosen,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return chosen

    return resolved_model_id
