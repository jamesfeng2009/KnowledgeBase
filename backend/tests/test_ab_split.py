"""
在线灰度/AB 分流测试 — app/core/ab_split.py。

覆盖：
    - 无实验 / 空配置 → 原样返回
    - traffic_pct=0/100 边界
    - 确定性 sticky 分桶（同一用户同一结果）
    - 流量百分比分布合理性（50% 配置 ≈ 50% 命中）
    - 用户显式选择的模型不被劫持（resolved != control）
    - 租户 allowlist（定向灰度）/ blocklist（锁死 control）
    - 非法配置容错（JSON 坏 / 字段缺失 / 越界 traffic_pct）
    - enabled=False / 多实验共存
"""

import pytest

from app.core import ab_split
from app.core.ab_split import apply_ab_split, reset_experiments_cache

CONTROL = "qwen-dpo-v3-7b"
TREATMENT = "qwen-dpo-v4-7b"


def _set_experiments(monkeypatch, experiments: list[dict]) -> None:
    """monkeypatch ab_split.get_settings 返回含 AB_EXPERIMENTS 的假 settings。

    Pydantic v2 字段不是类属性，不能 monkeypatch Settings 类本身；
    ab_split 顶部 ``from app.config import get_settings`` 已绑定到模块，
    直接替换该引用最稳。
    """
    import json
    from types import SimpleNamespace

    fake_settings = SimpleNamespace(
        AB_EXPERIMENTS=json.dumps(experiments, ensure_ascii=False)
    )
    monkeypatch.setattr(ab_split, "get_settings", lambda: fake_settings)
    reset_experiments_cache()


@pytest.fixture(autouse=True)
def _restore_cache():
    """每条用例后清空 lru_cache，避免污染其他测试。"""
    yield
    reset_experiments_cache()


class TestBasicSplit:
    """基础分流行为。"""

    def test_no_experiments_returns_resolved(self, monkeypatch):
        _set_experiments(monkeypatch, [])
        assert apply_ab_split(CONTROL, user_id="u1") == CONTROL

    def test_empty_model_returns_empty(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 100}],
        )
        assert apply_ab_split("", user_id="u1") == ""

    def test_traffic_100_all_treatment(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 100}],
        )
        for i in range(20):
            assert apply_ab_split(CONTROL, user_id=f"u{i}") == TREATMENT

    def test_traffic_0_no_treatment(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 0}],
        )
        for i in range(20):
            assert apply_ab_split(CONTROL, user_id=f"u{i}") == CONTROL


class TestStickyBucketing:
    """确定性分桶。"""

    def test_same_user_same_bucket(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 50}],
        )
        first = apply_ab_split(CONTROL, user_id="user-42")
        for _ in range(10):
            assert apply_ab_split(CONTROL, user_id="user-42") == first

    def test_distribution_sanity(self, monkeypatch):
        """50% 配置在 500 用户上命中比例应落在 40%-60%。"""
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 50}],
        )
        hits = sum(
            apply_ab_split(CONTROL, user_id=f"u{i}") == TREATMENT
            for i in range(500)
        )
        assert 200 <= hits <= 300, f"50% 配置命中 {hits}/500，分布异常"


class TestExplicitChoice:
    """用户显式选择不被劫持。"""

    def test_non_control_model_untouched(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 100}],
        )
        # 用户显式选了另一个模型 → 即使 traffic 100% 也不劫持
        assert apply_ab_split("claude-haiku-4", user_id="u1") == "claude-haiku-4"


class TestTenantGray:
    """租户维度灰度。"""

    def test_allowlist_forces_treatment(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 0,
              "tenant_allowlist": ["tenant-A"]}],
        )
        # traffic 0% 但租户在白名单 → 100% treatment
        assert apply_ab_split(CONTROL, tenant_id="tenant-A", user_id="u1") == TREATMENT
        # 不在白名单 → control
        assert apply_ab_split(CONTROL, tenant_id="tenant-B", user_id="u2") == CONTROL

    def test_blocklist_locks_control(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 100,
              "tenant_blocklist": ["tenant-SAFE"]}],
        )
        # traffic 100% 但租户在黑名单 → 锁死 control
        assert apply_ab_split(CONTROL, tenant_id="tenant-SAFE", user_id="u1") == CONTROL
        assert apply_ab_split(CONTROL, tenant_id="tenant-OK", user_id="u2") == TREATMENT


class TestConfigFaultTolerance:
    """非法配置容错 — 绝不阻断主链路。"""

    def test_broken_json_disables_split(self, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(
            ab_split,
            "get_settings",
            lambda: SimpleNamespace(AB_EXPERIMENTS="{broken json"),
        )
        reset_experiments_cache()
        assert apply_ab_split(CONTROL, user_id="u1") == CONTROL

    def test_invalid_experiment_skipped(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [
                {"name": "", "control_model": CONTROL,
                 "treatment_model": TREATMENT, "traffic_pct": 100},  # 名字缺失 → 跳过
                {"name": "e2", "control_model": CONTROL,
                 "treatment_model": TREATMENT, "traffic_pct": 500},  # 越界 → 跳过
                {"name": "e3", "control_model": CONTROL,
                 "treatment_model": TREATMENT, "traffic_pct": 100},  # 合法
            ],
        )
        assert apply_ab_split(CONTROL, user_id="u1") == TREATMENT

    def test_disabled_experiment_ignored(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [{"name": "e1", "control_model": CONTROL,
              "treatment_model": TREATMENT, "traffic_pct": 100,
              "enabled": False}],
        )
        assert apply_ab_split(CONTROL, user_id="u1") == CONTROL
