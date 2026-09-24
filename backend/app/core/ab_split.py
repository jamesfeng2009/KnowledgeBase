"""
在线灰度 / AB 分流 — 单一职责：把「默认流量」按租户/用户维度确定性地
分配到实验组（treatment）或对照组（control），并给出该组生效的变体。

分流维度（一次实验可同时覆盖，互不冲突）：
    - model：用哪个模型（历史上唯一的维度，``apply_ab_split`` 仍保留）；
    - prompt_variant：生成层指引走哪个变体文件（app.rag.prompt_variants）；
    - max_iterations：Agent Loop 迭代上限（编排层旋钮）。
  后两者通过 ``assign_ab_arm`` 返回的 :class:`ABAssignment` 交由
  app.core.ab_context 传递到消费点，不改 engine / generator 的函数签名。

与离线 evolution gate（scripts/run_evolution.py）的关系 — 两道闸门互补：
    evolution gate 是「上线前」离线闸门：新 adapter / 新模型必须先通过
    评测指标（Reject@boundary、工作题误拒等）才允许进入实验配置；
    本模块是「上线后」在线分流：通过闸门的候选按流量百分比 /
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
    bucket / arm / 变体 / tenant / user 维度），供离线按桶聚合
    TTFT（sse.ttft）/ 满意度 / 拒答率指标，形成线上评测闭环。
    显著性与结果归因见 app.eval.significance 与
    app.services.outcome_attribution。
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
from app.core.ab_context import ABAssignment
from app.utils.logger import get_logger

log = get_logger(__name__)

__all__ = [
    "ABExperiment",
    "ABOverrides",
    "apply_ab_split",
    "assign_ab_arm",
    "reset_experiments_cache",
]


@dataclass(frozen=True)
class ABOverrides:
    """一个实验臂（arm）生效的变体集合。

    全部字段可空 —— 空即「本实验不动这个维度」，沿用调用方/引擎默认。
    这样模型实验和 prompt 实验可以共用一套分流代码，而不必为每种
    被试因素写一个平行实现。

    Attributes:
        model: 模型 ID（models.json 中的 id）。
        prompt_variant: 生成层指引变体名（app/rag/prompts/variants/ 下的文件名）。
        max_iterations: Agent Loop 迭代上限（None 表示沿用引擎默认）。
    """

    model: str = ""
    prompt_variant: str = ""
    max_iterations: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ABOverrides:
        """从配置字典构造，容忍缺字段；非法取值抛 ValueError 交由上层跳过。"""
        max_iter_raw = data.get("max_iterations")
        max_iterations: int | None = None
        if max_iter_raw is not None and str(max_iter_raw).strip() != "":
            try:
                max_iterations = int(str(max_iter_raw))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"max_iterations not an int: {max_iter_raw}") from exc
        return cls(
            model=str(data.get("model", "") or "").strip(),
            prompt_variant=str(data.get("prompt_variant", "") or "").strip(),
            max_iterations=max_iterations,
        )


@dataclass(frozen=True)
class ABExperiment:
    """一个在线分流实验的配置。

    Attributes:
        name: 实验名（唯一），参与哈希计算 — 更名即等价重开实验。
        control_model: 对照组模型 ID（models.json 中的 id），通常为
            当前线上默认模型。同时也是 control 臂生效的模型。
        treatment_model: 实验组模型 ID — 通过离线 evolution gate 的候选。
            纯 prompt/编排实验可将其设为与 control_model 相同（只分变体不分模型）。
        traffic_pct: 实验组流量百分比（0-100）。
        enabled: 实验开关（False 等价 traffic_pct=0，但保留配置）。
        tenant_allowlist: 定向灰度租户 ID 列表 — 100% 进 treatment。
        tenant_blocklist: 排除租户 ID 列表 — 0% 参与（锁死 control）。
        description: 实验说明（供运维/审计阅读）。
        control_overrides: control 臂生效的变体（model 缺省即 control_model）。
        treatment_overrides: treatment 臂生效的变体（model 缺省即 treatment_model）。
    """

    name: str
    control_model: str
    treatment_model: str
    traffic_pct: int
    enabled: bool = True
    tenant_allowlist: tuple[str, ...] = field(default=())
    tenant_blocklist: tuple[str, ...] = field(default=())
    description: str = ""
    control_overrides: ABOverrides = field(default_factory=ABOverrides)
    treatment_overrides: ABOverrides = field(default_factory=ABOverrides)

    def arm_overrides(self, arm: str) -> ABOverrides:
        """取指定臂的变体（未知臂回落 control）。"""
        return self.treatment_overrides if arm == "treatment" else self.control_overrides


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
            control_overrides = ABOverrides.from_dict(
                _arm_config(item.get("control"), model=control)
            )
            treatment_overrides = ABOverrides.from_dict(
                _arm_config(item.get("treatment"), model=treatment)
            )
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
                    control_overrides=control_overrides,
                    treatment_overrides=treatment_overrides,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("ab.config_experiment_skipped", index=i, error=str(exc))
    return tuple(experiments)


def _arm_config(raw: object, *, model: str) -> dict[str, object]:
    """归一化单臂配置 — 缺省时只带 model（等价于历史行为）。

    配置里写 ``"treatment": {"model": "x", "prompt_variant": "v2"}``，
    也兼容完全不写 control/treatment 块（只用旧的 control_model /
    treatment_model 两个字段）。
    """
    if isinstance(raw, dict):
        merged: dict[str, object] = {"model": model}
        merged.update(raw)
        return merged
    return {"model": model}


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


def assign_ab_arm(
    resolved_model_id: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    correlation_id: str | None = None,
) -> ABAssignment:
    """对一次请求做实验分流 — 返回命中臂及其生效变体。

    分流规则（按序）：
        1. 无启用的实验 / resolved 不是某实验的 control → 未命中；
        2. tenant 在 blocklist → 未命中（锁死 control）；
        3. tenant 在 allowlist → 直接 treatment（定向灰度，bucket=-1）；
        4. 否则按 sticky 分桶：bucket < traffic_pct → treatment。

    命中 control 时也返回 ABAssignment（arm="control"）—— 归因需要
    **两侧都有分组标记**，否则无法构造对照桶，只能看到 treatment 一侧。

    Args:
        resolved_model_id: ``ModelSelectionService.resolve_model`` 的结果
            （session 级选择 > system 默认）。用户显式选择的模型 !=
            control 时不会被劫持。
        tenant_id: 租户 ID（多租户灰度维度，可空）。
        user_id: 用户 ID（sticky 分桶维度，可空；为空时退化为按租户/匿名桶）。
        correlation_id: 本次请求/决策的关联键（可空）。写进曝光日志，
            供离线把「分流曝光」与「下游结果」join 起来做归因
            （见 app.services.outcome_attribution）——没有它就只能看到
            两桶各是多少，无法把某次结果归到某次曝光。

    Returns:
        :class:`ABAssignment`；未命中任何实验时 ``hit`` 为 False、
        ``model`` 原样回传 resolved_model_id。
    """
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
                bucket=-1,
                arm="treatment",
                traffic_pct=exp.traffic_pct,
                tenant_id=tenant_id,
                user_id=user_id,
                via="allowlist",
                **_exposure_fields(exp.treatment_overrides, correlation_id),
            )
            return _assignment(
                exp,
                "treatment",
                bucket=-1,
                via="allowlist",
                correlation_id=correlation_id,
            )
        if exp.traffic_pct == 0:
            continue
        sticky_key = user_id or tenant_id or "anonymous"
        bucket = _bucket_of(exp.name, sticky_key)
        arm = "treatment" if bucket < exp.traffic_pct else "control"
        overrides = exp.arm_overrides(arm)
        log.info(
            "ab.exposure",
            experiment=exp.name,
            bucket=bucket,
            arm=arm,
            traffic_pct=exp.traffic_pct,
            tenant_id=tenant_id,
            user_id=user_id,
            via="traffic",
            **_exposure_fields(overrides, correlation_id),
        )
        return _assignment(
            exp, arm, bucket=bucket, via="traffic", correlation_id=correlation_id
        )

    return ABAssignment(model=resolved_model_id)


def _exposure_fields(
    overrides: ABOverrides, correlation_id: str | None
) -> dict[str, object]:
    """曝光埋点附带字段 — 本臂生效的变体 + 结果归因用的关联键。"""
    return {
        "chosen_model": overrides.model,
        "prompt_variant": overrides.prompt_variant,
        "max_iterations": overrides.max_iterations,
        "correlation_id": correlation_id,
    }


def _assignment(
    exp: ABExperiment,
    arm: str,
    *,
    bucket: int,
    via: str,
    correlation_id: str | None = None,
) -> ABAssignment:
    """由实验配置 + 命中臂构造 ABAssignment（model 缺省回落 control_model）。"""
    overrides = exp.arm_overrides(arm)
    model = overrides.model or (
        exp.treatment_model if arm == "treatment" else exp.control_model
    )
    return ABAssignment(
        experiment=exp.name,
        arm=arm,
        bucket=bucket,
        model=model,
        prompt_variant=overrides.prompt_variant,
        max_iterations=overrides.max_iterations,
        via=via,
        correlation_id=correlation_id or "",
    )


def apply_ab_split(
    resolved_model_id: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """模型维度的分流便捷入口 — 只关心「最终用哪个模型」时用它。

    等价于 ``assign_ab_arm(...).model``；需要 prompt / 编排变体时请直接
    使用 :func:`assign_ab_arm`（并配合 app.core.ab_context.ab_scope 传递）。

    Args:
        resolved_model_id: ``ModelSelectionService.resolve_model`` 的结果。
        tenant_id: 租户 ID（多租户灰度维度，可空）。
        user_id: 用户 ID（sticky 分桶维度，可空）。

    Returns:
        最终使用的模型 ID（未命中实验时原样返回入参）。
    """
    return assign_ab_arm(
        resolved_model_id, tenant_id=tenant_id, user_id=user_id
    ).model
