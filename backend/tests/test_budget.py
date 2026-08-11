"""
P0-3 四轴硬预算测试 —— 验证 HardBudget / RunBudget / check_budget 行为。

测试覆盖：
    - HardBudget 默认值与不可变性
    - RunBudget 累积（input/output/cache_read/cost）
    - cache-read token 从计费基数扣除（鼓励 prompt cache）
    - billable_tokens 不为负（兜底）
    - check_budget 四轴顺序检查（turns > seconds > tokens > cost）
    - BudgetExceeded 携带结构化 context（axis/value/limit/run_id）
    - get_budget_status 查询不抛异常
    - reset() 重置后从零开始
    - 配置从 Settings 读取（AGENT_BUDGET_* 环境变量）
"""

from __future__ import annotations

import time

import pytest

from app.config import get_settings
from app.core.budget import (
    HardBudget,
    RunBudget,
    check_budget,
    get_budget_status,
)
from app.core.exceptions import BudgetExceeded


# ------------------------------------------------------------------
# HardBudget 不可变性测试
# ------------------------------------------------------------------

class TestHardBudget:
    """HardBudget 值对象测试。"""

    def test_default_values(self):
        """默认值符合保守默认（宁可早死，不要烧钱）。"""
        b = HardBudget()
        assert b.max_turns == 5
        assert b.max_seconds == 300.0
        assert b.max_tokens == 200_000
        assert b.max_cost_usd == 1.0

    def test_frozen_immutable(self):
        """HardBudget 不可变 —— frozen dataclass。"""
        b = HardBudget()
        with pytest.raises(Exception):  # FrozenInstanceError
            b.max_turns = 10  # type: ignore[misc]

    def test_custom_values(self):
        """自定义值生效。"""
        b = HardBudget(max_turns=20, max_seconds=600, max_tokens=500_000, max_cost_usd=5.0)
        assert b.max_turns == 20
        assert b.max_seconds == 600
        assert b.max_tokens == 500_000
        assert b.max_cost_usd == 5.0


# ------------------------------------------------------------------
# RunBudget 累积测试
# ------------------------------------------------------------------

class TestRunBudget:
    """RunBudget 累积行为测试。"""

    def test_initial_state(self):
        """初始状态全零。"""
        run = RunBudget()
        assert run.turns == 0
        assert run.input_tokens == 0
        assert run.output_tokens == 0
        assert run.cache_read_tokens == 0
        assert run.cost_usd == 0.0

    def test_add_usage_accumulates(self):
        """add_usage 累积 token 和 cost。"""
        run = RunBudget()
        run.add_usage(input_tokens=1000, output_tokens=500, cost_usd=0.003)
        run.add_usage(input_tokens=2000, output_tokens=800, cost_usd=0.005)
        assert run.input_tokens == 3000
        assert run.output_tokens == 1300
        assert run.cost_usd == pytest.approx(0.008)

    def test_total_tokens(self):
        """total_tokens = input + output。"""
        run = RunBudget()
        run.add_usage(input_tokens=1000, output_tokens=500)
        assert run.total_tokens == 1500

    def test_cache_read_deducted_from_billable(self):
        """cache-read token 从计费基数扣除 —— 鼓励 prompt cache。"""
        run = RunBudget()
        run.add_usage(input_tokens=2000, output_tokens=500, cache_read_tokens=1500)
        # total = 2500, cache_read = 1500, billable = 1000
        assert run.total_tokens == 2500
        assert run.cache_read_tokens == 1500
        assert run.billable_tokens == 1000

    def test_billable_not_negative(self):
        """billable_tokens 不为负 —— 兜底 cache_read > total 的情况。"""
        run = RunBudget()
        run.add_usage(input_tokens=100, output_tokens=50, cache_read_tokens=500)  # cache_read > total
        assert run.billable_tokens == 0  # max(0, ...)

    def test_increment_turn(self):
        """increment_turn 增加迭代计数。"""
        run = RunBudget()
        run.increment_turn()
        run.increment_turn()
        assert run.turns == 2

    def test_elapsed_seconds_positive(self):
        """elapsed_seconds 基于 monotonic，随时间增长。"""
        run = RunBudget()
        time.sleep(0.01)
        assert run.elapsed_seconds > 0

    def test_reset(self):
        """reset 清零累积，但 start_time 重置。"""
        run = RunBudget()
        run.add_usage(input_tokens=1000, output_tokens=500, cost_usd=0.003)
        run.increment_turn()
        run.reset()
        assert run.turns == 0
        assert run.input_tokens == 0
        assert run.output_tokens == 0
        assert run.cache_read_tokens == 0
        assert run.cost_usd == 0.0


# ------------------------------------------------------------------
# check_budget 四轴检查测试
# ------------------------------------------------------------------

class TestCheckBudget:
    """check_budget 四轴硬检查测试。"""

    def test_pass_when_under_limit(self):
        """未超限时不抛异常。"""
        run = RunBudget()
        run.increment_turn()
        run.add_usage(input_tokens=100, output_tokens=50, cost_usd=0.001)
        budget = HardBudget(max_turns=5, max_seconds=300, max_tokens=200_000, max_cost_usd=1.0)
        check_budget(run, budget)  # 不抛

    def test_turns_exceeded(self):
        """turns 超限抛 BudgetExceeded。"""
        run = RunBudget()
        for _ in range(6):
            run.increment_turn()
        budget = HardBudget(max_turns=5)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(run, budget, run_id="run-123")
        assert exc_info.value.axis == "turns"
        assert exc_info.value.value == 6
        assert exc_info.value.limit == 5
        assert exc_info.value.run_id == "run-123"

    def test_tokens_exceeded(self):
        """tokens 超限抛 BudgetExceeded。"""
        run = RunBudget()
        run.add_usage(input_tokens=150_000, output_tokens=60_000)  # billable=210000 > 200000
        budget = HardBudget(max_tokens=200_000)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(run, budget)
        assert exc_info.value.axis == "tokens"

    def test_cost_exceeded(self):
        """cost_usd 超限抛 BudgetExceeded。"""
        run = RunBudget()
        run.add_usage(input_tokens=1000, output_tokens=500, cost_usd=1.5)
        budget = HardBudget(max_cost_usd=1.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(run, budget)
        assert exc_info.value.axis == "cost_usd"

    def test_cache_read_reduces_billable(self):
        """cache-read token 减少 billable，可能避免触发 tokens 超限。"""
        run = RunBudget()
        # total = 250000，但 cache_read = 150000，billable = 100000 < 200000
        run.add_usage(input_tokens=200_000, output_tokens=50_000, cache_read_tokens=150_000)
        budget = HardBudget(max_tokens=200_000)
        check_budget(run, budget)  # 不抛 —— cache_read 救了它

    def test_seconds_exceeded(self):
        """seconds 超限抛 BudgetExceeded（模拟时间流逝）。"""
        run = RunBudget()
        # 直接改 start_time 模拟时间流逝（monotonic 不可 mock）
        run.start_time = time.monotonic() - 400  # 400 秒前开始
        budget = HardBudget(max_seconds=300)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(run, budget)
        assert exc_info.value.axis == "seconds"


# ------------------------------------------------------------------
# 异常 context 测试
# ------------------------------------------------------------------

class TestBudgetExceededContext:
    """BudgetExceeded 异常携带结构化 context 测试。"""

    def test_context_contains_axis_value_limit(self):
        """异常 context 含 axis / value / limit。"""
        run = RunBudget()
        run.add_usage(input_tokens=250_000, output_tokens=0)
        budget = HardBudget(max_tokens=200_000)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(run, budget, run_id="run-456")
        ctx = exc_info.value.context
        assert ctx["axis"] == "tokens"
        assert ctx["value"] == 250_000
        assert ctx["limit"] == 200_000
        assert ctx["run_id"] == "run-456"

    def test_to_dict_serializable(self):
        """to_dict 可序列化。"""
        run = RunBudget()
        run.add_usage(cost_usd=2.0)
        budget = HardBudget(max_cost_usd=1.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(run, budget)
        d = exc_info.value.to_dict()
        assert d["error_type"] == "BudgetExceeded"
        assert d["context"]["axis"] == "cost_usd"


# ------------------------------------------------------------------
# get_budget_status 测试
# ------------------------------------------------------------------

class TestGetBudgetStatus:
    """get_budget_status 查询测试。"""

    def test_returns_all_axes(self):
        """返回四轴状态。"""
        run = RunBudget()
        run.increment_turn()
        run.add_usage(input_tokens=50_000, output_tokens=20_000, cost_usd=0.3)
        budget = HardBudget(max_turns=10, max_tokens=200_000, max_cost_usd=1.0)
        status = get_budget_status(run, budget)
        assert "turns" in status
        assert "seconds" in status
        assert "tokens" in status
        assert "cost_usd" in status

    def test_ratio_calculation(self):
        """使用比例计算正确。"""
        run = RunBudget()
        run.increment_turn()  # 1/5 = 0.2
        run.add_usage(input_tokens=40_000, output_tokens=10_000)  # billable=50000/200000=0.25
        budget = HardBudget(max_turns=5, max_tokens=200_000)
        status = get_budget_status(run, budget)
        assert status["turns"]["ratio"] == pytest.approx(0.2)
        assert status["tokens"]["ratio"] == pytest.approx(0.25)

    def test_no_exception_when_over_limit(self):
        """超限时也不抛异常（纯查询用途）。"""
        run = RunBudget()
        run.add_usage(cost_usd=2.0)
        budget = HardBudget(max_cost_usd=1.0)
        status = get_budget_status(run, budget)  # 不抛
        assert status["cost_usd"]["ratio"] > 1.0


# ------------------------------------------------------------------
# 配置测试
# ------------------------------------------------------------------

class TestBudgetConfig:
    """Settings 配置项测试。"""

    def test_config_has_budget_settings(self):
        """Settings 含 AGENT_BUDGET_* 配置项。"""
        s = get_settings()
        assert hasattr(s, "AGENT_BUDGET_MAX_TURNS")
        assert hasattr(s, "AGENT_BUDGET_MAX_SECONDS")
        assert hasattr(s, "AGENT_BUDGET_MAX_TOKENS")
        assert hasattr(s, "AGENT_BUDGET_MAX_COST_USD")

    def test_config_defaults_positive(self):
        """默认值为正数。"""
        s = get_settings()
        assert s.AGENT_BUDGET_MAX_TURNS > 0
        assert s.AGENT_BUDGET_MAX_SECONDS > 0
        assert s.AGENT_BUDGET_MAX_TOKENS > 0
        assert s.AGENT_BUDGET_MAX_COST_USD > 0

    def test_build_hard_budget_from_settings(self):
        """从 Settings 构建 HardBudget。"""
        s = get_settings()
        b = HardBudget(
            max_turns=s.AGENT_BUDGET_MAX_TURNS,
            max_seconds=s.AGENT_BUDGET_MAX_SECONDS,
            max_tokens=s.AGENT_BUDGET_MAX_TOKENS,
            max_cost_usd=s.AGENT_BUDGET_MAX_COST_USD,
        )
        assert b.max_turns == s.AGENT_BUDGET_MAX_TURNS
        assert b.max_tokens == s.AGENT_BUDGET_MAX_TOKENS
