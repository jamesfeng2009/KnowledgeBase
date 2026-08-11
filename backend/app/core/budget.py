"""
四轴硬预算 —— turns / seconds / tokens / cost_usd 任一触顶即硬停。

设计动机：
    无人值守的 Agent 循环可能"烧钱"——turns 越绕越多、token 越拖越长、
    单次调用成本失控。max_iterations 只限循环次数，无法在 token 累积达上限时
    主动终止。本模块提供四轴硬上限，任一触顶即抛 BudgetExceeded 硬停。

    保守默认：无人值守的 run 应"宁可早死，不要烧钱"。

四轴语义：
    - turns: 决策循环迭代次数（已有 max_iterations，本模块统一纳管）
    - seconds: wall-clock 时间（已有 _total_timeout_s，本模块统一纳管）
    - tokens: 累积 token 用量（cache-read 从计费基数扣除，鼓励 prompt cache）
    - cost_usd: 累积成本（美元）

cache-read 折扣：
    Anthropic / OpenAI 的 prompt cache 命中时，cache-read token 以折扣价计费
    （Anthropic 0.1x，OpenAI 0.5x）。本模块把 cache-read token 从 token 计费基数
    扣除 —— 鼓励 prompt cache 复用，缓存命中的 token 不计入预算压力。

与现有 max_iterations / _total_timeout_s 的关系：
    现有逻辑在决策循环里 break（不抛异常），用于"软终止"。
    本模块的 check_budget 抛 BudgetExceeded（不可重试异常），用于"硬终止"。
    两者并存：先 check_budget 硬检查，再走现有软终止逻辑。
    这样既不破坏现有逻辑，又增加了 token/cost 硬闸门。

使用示例::

    from app.core.budget import HardBudget, RunBudget, check_budget

    budget = HardBudget(max_turns=5, max_tokens=200_000, max_cost_usd=1.0)
    run = RunBudget(start_time=time.monotonic())

    # 每次累积 usage 后更新
    run.add_usage(input_tokens=1000, output_tokens=500, cost_usd=0.003)

    # 决策循环每轮开始前检查
    try:
        check_budget(run, budget, run_id="run-123")
    except BudgetExceeded as exc:
        log.warning("budget.exceeded", **exc.context)
        break

遵循单一职责：本模块仅提供预算检查，不含业务逻辑。
遵循依赖倒置：所有阈值从 HardBudget dataclass 读取，由调用方从 config 注入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.exceptions import BudgetExceeded


@dataclass(frozen=True)
class HardBudget:
    """四轴硬上限 —— 不可变值对象，任一触顶即硬停。

    保守默认值 —— 无人值守的 run 应"宁可早死，不要烧钱"。

    Attributes:
        max_turns: 决策循环最大迭代次数。
        max_seconds: wall-clock 最大时间（秒）。
        max_tokens: 累积 token 上限（cache-read 从基数扣除）。
        max_cost_usd: 累积成本上限（美元）。
    """

    max_turns: int = 5
    max_seconds: float = 300.0
    max_tokens: int = 200_000
    max_cost_usd: float = 1.0


@dataclass
class RunBudget:
    """运行时累积预算 —— 每次 LLM 调用后更新，check_budget 前读取。

    可变对象 —— 在决策循环中持续累加 token / cost / turns。
    """

    start_time: float = field(default_factory=time.monotonic)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    run_id: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        """已运行时间（秒）—— 基于 monotonic，免疫时钟回拨。"""
        return time.monotonic() - self.start_time

    @property
    def total_tokens(self) -> int:
        """总 token 数（input + output）。"""
        return self.input_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        """计费 token 数 —— cache-read 从基数扣除，鼓励 prompt cache 复用。

        Anthropic / OpenAI 的 prompt cache 命中时，cache-read token 以折扣价计费
        （Anthropic 0.1x，OpenAI 0.5x）。把 cache-read 从基数扣除 ——
        缓存命中的 token 不计入预算压力。

        max(0, ...) 兜底负数（cache_read > total 的统计口径错位时）。
        """
        return max(0, self.total_tokens - self.cache_read_tokens)

    def add_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """累积单次 LLM 调用的 token / cost 用量。

        Args:
            input_tokens: 输入 token 数。
            output_tokens: 输出 token 数。
            cache_read_tokens: prompt cache 命中读取的 token 数（从计费基数扣除）。
            cost_usd: 本次调用成本（美元，由调用方从 PricingTable 算好传入）。
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cost_usd += cost_usd

    def increment_turn(self) -> None:
        """增加一轮迭代计数。"""
        self.turns += 1

    def reset(self) -> None:
        """重置预算累积（新一轮 answer 时调用）。"""
        self.start_time = time.monotonic()
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cost_usd = 0.0


def check_budget(
    run: RunBudget,
    budget: HardBudget,
    *,
    run_id: str | None = None,
) -> None:
    """四轴硬预算检查 —— 任一触顶即抛 BudgetExceeded。

    检查顺序：turns > seconds > tokens > cost（按轴优先级）。
    报错时明确哪个轴先爆，便于诊断。

    Args:
        run: 当前累积的运行预算。
        budget: 硬上限配置。
        run_id: 关联的 run 标识（用于日志关联，可选）。

    Raises:
        BudgetExceeded: 任一轴触顶时抛出，携带 axis / value / limit / run_id。

    Note:
        本函数不返回布尔值 —— 超限即抛异常，调用方 try/except 处理。
        这样保证"硬停"语义：不可重试，不可忽略。
    """
    effective_run_id = run_id or run.run_id

    # 1. turns 轴
    if run.turns > budget.max_turns:
        raise BudgetExceeded(
            axis="turns",
            value=float(run.turns),
            limit=float(budget.max_turns),
            run_id=effective_run_id,
        )

    # 2. seconds 轴（wall-clock，基于 monotonic）
    elapsed = run.elapsed_seconds
    if elapsed > budget.max_seconds:
        raise BudgetExceeded(
            axis="seconds",
            value=elapsed,
            limit=budget.max_seconds,
            run_id=effective_run_id,
        )

    # 3. tokens 轴（cache-read 从基数扣除）
    billable = run.billable_tokens
    if billable > budget.max_tokens:
        raise BudgetExceeded(
            axis="tokens",
            value=float(billable),
            limit=float(budget.max_tokens),
            run_id=effective_run_id,
        )

    # 4. cost_usd 轴
    if run.cost_usd > budget.max_cost_usd:
        raise BudgetExceeded(
            axis="cost_usd",
            value=run.cost_usd,
            limit=budget.max_cost_usd,
            run_id=effective_run_id,
        )


def get_budget_status(run: RunBudget, budget: HardBudget) -> dict:
    """获取预算使用状态 —— 用于监控 / 告警 / 日志。

    返回各轴的当前值 / 上限 / 使用比例，便于 UI 展示进度条或告警。
    不抛异常 —— 纯查询用途。
    """
    return {
        "turns": {
            "used": run.turns,
            "limit": budget.max_turns,
            "ratio": run.turns / budget.max_turns if budget.max_turns > 0 else 0.0,
        },
        "seconds": {
            "used": round(run.elapsed_seconds, 1),
            "limit": budget.max_seconds,
            "ratio": run.elapsed_seconds / budget.max_seconds if budget.max_seconds > 0 else 0.0,
        },
        "tokens": {
            "used": run.billable_tokens,
            "total": run.total_tokens,
            "cache_read": run.cache_read_tokens,
            "limit": budget.max_tokens,
            "ratio": run.billable_tokens / budget.max_tokens if budget.max_tokens > 0 else 0.0,
        },
        "cost_usd": {
            "used": round(run.cost_usd, 6),
            "limit": budget.max_cost_usd,
            "ratio": run.cost_usd / budget.max_cost_usd if budget.max_cost_usd > 0 else 0.0,
        },
    }
