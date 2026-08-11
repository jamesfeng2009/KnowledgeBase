"""
P1-A 熔断器 — 三态状态机 + Provider 集成 + 状态 API 测试。

测试覆盖：
    1. CircuitBreaker 状态机（CLOSED → OPEN → HALF_OPEN → CLOSED/OPEN）
    2. 全局注册表（get_circuit_breaker 单例）
    3. circuit_call 装饰器（异步函数）
    4. LLM Provider 集成（VLLM / DashScope / Anthropic）
    5. 状态 API 端点
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    circuit_call,
    get_all_circuit_status,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


# ======================================================================
# CircuitBreaker 状态机测试
# ======================================================================


class TestCircuitBreakerStateMachine:
    """熔断器三态状态机测试。"""

    def setup_method(self):
        """每个测试前重置全局注册表。"""
        reset_all_circuit_breakers()

    def test_initial_state_is_closed(self):
        """新创建的熔断器初始状态为 CLOSED。"""
        cb = CircuitBreaker(name="test_service", failure_threshold=3, recovery_timeout=1.0)
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_closed_to_open_after_threshold(self):
        """连续失败达到阈值后从 CLOSED 转为 OPEN。"""
        cb = CircuitBreaker(name="test_svc", failure_threshold=3, recovery_timeout=60.0)

        cb._record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 1

        cb._record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 2

        cb._record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

    def test_success_does_not_reset_window_failures_in_closed(self):
        """CLOSED 状态下成功调用不重置窗口内失败计数（P2-8 滑动窗口语义）。

        旧机制（连续失败计数）：成功立即清零 failure_count。
        新机制（滑动窗口）：成功不抹除历史失败证据，窗口内未过期的失败
        时间戳仍在 deque 中。只有 HALF_OPEN 探测成功才清空 deque
        （见 test_half_open_to_closed_on_success）。
        """
        # failure_window 默认 60s，窗口内失败不会过期
        cb = CircuitBreaker(name="test_svc", failure_threshold=3, recovery_timeout=60.0)

        cb._record_failure()
        cb._record_failure()
        assert cb.failure_count == 2

        cb._record_success()
        # 滑动窗口语义：成功不重置，窗口内失败仍在
        assert cb.failure_count == 2
        assert cb.state == CircuitState.CLOSED

    def test_success_evicts_expired_failures_in_closed(self):
        """CLOSED 状态下成功调用清理窗口外过期时间戳（P2-8 滑动窗口语义）。

        成功调用触发 _evict_expired_timestamps，窗口外（已过期）的失败
        时间戳从 deque 左侧淘汰。
        """
        # failure_window=0.1s，快速过期
        cb = CircuitBreaker(
            name="test_svc", failure_threshold=3, recovery_timeout=60.0,
            failure_window=0.1,
        )

        cb._record_failure()
        cb._record_failure()
        assert cb.failure_count == 2

        # 等待窗口过期
        time.sleep(0.15)

        # 成功调用触发过期清理
        cb._record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_open_to_half_open_after_recovery_timeout(self):
        """OPEN 状态经过 recovery_timeout 后转为 HALF_OPEN。"""
        cb = CircuitBreaker(name="test_svc", failure_threshold=1, recovery_timeout=0.1)

        # 触发熔断
        cb._record_failure()
        assert cb.state == CircuitState.OPEN

        # 等待 recovery_timeout
        time.sleep(0.15)

        # 检查是否应该转换
        assert cb._should_transition_to_half_open() is True

    def test_half_open_to_closed_on_success(self):
        """HALF_OPEN 状态下探测成功转为 CLOSED。"""
        cb = CircuitBreaker(name="test_svc", failure_threshold=1, recovery_timeout=0.1)

        # 触发熔断
        cb._record_failure()
        assert cb.state == CircuitState.OPEN

        # 等待恢复
        time.sleep(0.15)
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 0

        # 探测成功
        cb._record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_half_open_to_open_on_failure(self):
        """HALF_OPEN 状态下探测失败回到 OPEN。"""
        cb = CircuitBreaker(name="test_svc", failure_threshold=1, recovery_timeout=0.1)

        # 触发熔断 → 等待恢复 → 半开
        cb._record_failure()
        time.sleep(0.15)
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 0

        # 探测失败
        cb._record_failure()
        assert cb.state == CircuitState.OPEN

    def test_open_state_rejects_calls(self):
        """OPEN 状态下上下文管理器抛出 CircuitBreakerOpenError。"""
        cb = CircuitBreaker(name="test_svc", failure_threshold=1, recovery_timeout=60.0)
        cb._record_failure()
        assert cb.state == CircuitState.OPEN

        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(cb.__aenter__())

    def test_half_open_limits_concurrent_calls(self):
        """HALF_OPEN 状态限制并发探测请求数。"""
        cb = CircuitBreaker(name="test_svc", failure_threshold=1, recovery_timeout=0.1)
        cb.half_open_max_calls = 1

        cb._record_failure()
        time.sleep(0.15)
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 0

        # 第一次探测允许
        asyncio.run(cb.__aenter__())
        cb.half_open_calls = 1
        asyncio.run(cb.__aexit__(None, None, None))

        # 重置到 half_open 状态
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 1

        # 第二次探测被拒绝
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(cb.__aenter__())


# ======================================================================
# 全局注册表测试
# ======================================================================


class TestCircuitBreakerRegistry:
    """全局注册表测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    def test_get_circuit_breaker_returns_singleton(self):
        """同名熔断器返回同一实例。"""
        cb1 = get_circuit_breaker("test_svc")
        cb2 = get_circuit_breaker("test_svc")
        assert cb1 is cb2

    def test_get_circuit_breaker_different_names(self):
        """不同名返回不同实例。"""
        cb1 = get_circuit_breaker("svc_a")
        cb2 = get_circuit_breaker("svc_b")
        assert cb1 is not cb2
        assert cb1.name == "svc_a"
        assert cb2.name == "svc_b"

    def test_get_all_circuit_status_returns_dict(self):
        """get_all_circuit_status 返回字典。"""
        get_circuit_breaker("svc_a")
        get_circuit_breaker("svc_b")
        statuses = get_all_circuit_status()
        assert isinstance(statuses, dict)
        assert "svc_a" in statuses
        assert "svc_b" in statuses

    def test_get_all_circuit_status_has_required_fields(self):
        """状态快照包含必要字段。"""
        get_circuit_breaker("test_svc")
        statuses = get_all_circuit_status()
        status = statuses["test_svc"]
        assert "name" in status
        assert "state" in status
        assert "failure_count" in status
        assert "failure_threshold" in status
        assert "recovery_timeout" in status

    def test_reset_all_circuit_breakers(self):
        """reset_all_circuit_breakers 恢复所有熔断器到 CLOSED。"""
        cb = get_circuit_breaker("reset_test_svc", failure_threshold=1)
        cb._record_failure()
        assert cb.state == CircuitState.OPEN

        reset_all_circuit_breakers()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0


# ======================================================================
# circuit_call 装饰器测试
# ======================================================================


class TestCircuitCallDecorator:
    """circuit_call 装饰器测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    def test_circuit_call_success(self):
        """成功调用不触发熔断。"""
        @circuit_call("success_svc", failure_threshold=3)
        async def succeed():
            return "ok"

        result = asyncio.run(succeed())
        assert result == "ok"

        cb = get_circuit_breaker("success_svc")
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_circuit_call_failure_triggers_breaker(self):
        """连续失败触发熔断。"""
        @circuit_call("fail_svc", failure_threshold=2)
        async def always_fail():
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            asyncio.run(always_fail())
        with pytest.raises(ConnectionError):
            asyncio.run(always_fail())

        cb = get_circuit_breaker("fail_svc")
        assert cb.state == CircuitState.OPEN

    def test_circuit_call_open_rejects_fast(self):
        """熔断开启后快速失败，不执行函数。"""
        call_count = 0

        @circuit_call("reject_svc", failure_threshold=1)
        async def protected():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("down")

        # 第一次失败触发熔断
        with pytest.raises(ConnectionError):
            asyncio.run(protected())
        assert call_count == 1

        # 第二次被熔断器拦截
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(protected())
        assert call_count == 1  # 函数未执行


# ======================================================================
# CircuitBreakerOpenError 测试
# ======================================================================


class TestCircuitBreakerOpenError:
    """CircuitBreakerOpenError 异常测试。"""

    def test_error_message_contains_name(self):
        """错误消息包含熔断器名称。"""
        err = CircuitBreakerOpenError("dashscope", CircuitState.OPEN)
        assert "dashscope" in str(err)

    def test_error_message_contains_state(self):
        """错误消息包含当前状态。"""
        err = CircuitBreakerOpenError("dashscope", CircuitState.OPEN)
        assert "open" in str(err).lower()

    def test_error_has_name_attribute(self):
        """异常对象有 name 属性。"""
        err = CircuitBreakerOpenError("milvus", CircuitState.HALF_OPEN)
        assert err.name == "milvus"

    def test_error_has_state_attribute(self):
        """异常对象有 state 属性。"""
        err = CircuitBreakerOpenError("milvus", CircuitState.HALF_OPEN)
        assert err.state == CircuitState.HALF_OPEN


# ======================================================================
# LLM Provider 集成验证
# ======================================================================


class TestLLMProviderCircuitBreaker:
    """LLM Provider 熔断器集成验证。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    def test_vllm_provider_has_circuit_breaker(self):
        """VLLMProvider 有熔断器属性。"""
        from app.llm.vllm_provider import VLLMProvider

        provider = VLLMProvider.__new__(VLLMProvider)
        assert hasattr(VLLMProvider, "_circuit_breaker_name")
        assert VLLMProvider._circuit_breaker_name == "vllm"

    def test_dashscope_provider_has_circuit_breaker_name(self):
        """DashScopeProvider 有熔断器名称。"""
        from app.llm.dashscope_provider import DashScopeProvider

        assert hasattr(DashScopeProvider, "_circuit_breaker_name")
        assert DashScopeProvider._circuit_breaker_name == "dashscope"

    def test_anthropic_provider_has_circuit_breaker_name(self):
        """AnthropicProvider 有熔断器名称。"""
        from app.llm.anthropic_provider import AnthropicProvider

        assert hasattr(AnthropicProvider, "_circuit_breaker_name")
        assert AnthropicProvider._circuit_breaker_name == "anthropic"

    def test_vllm_source_has_circuit_breaker_import(self):
        """vllm_provider.py 源码包含熔断器导入。"""
        import app.llm.vllm_provider as mod

        source = open(mod.__file__).read()
        assert "circuit_breaker" in source
        assert "get_circuit_breaker" in source

    def test_anthropic_source_has_circuit_breaker_import(self):
        """anthropic_provider.py 源码包含熔断器导入。"""
        import app.llm.anthropic_provider as mod

        source = open(mod.__file__).read()
        assert "circuit_breaker" in source
        assert "get_circuit_breaker" in source

    def test_dashscope_source_has_circuit_breaker_import(self):
        """dashscope_provider.py 源码包含熔断器导入。"""
        import app.llm.dashscope_provider as mod

        source = open(mod.__file__).read()
        assert "circuit_breaker" in source
        assert "get_circuit_breaker" in source


# ======================================================================
# 配置参数验证
# ======================================================================


class TestCircuitBreakerConfig:
    """熔断器配置参数验证。"""

    def test_config_parameters_exist(self):
        """Settings 包含熔断器配置参数。"""
        from app.config import get_settings

        settings = get_settings()
        assert hasattr(settings, "CIRCUIT_BREAKER_FAILURE_THRESHOLD")
        assert hasattr(settings, "CIRCUIT_BREAKER_RECOVERY_TIMEOUT")
        assert hasattr(settings, "CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS")

    def test_config_parameters_positive(self):
        """所有熔断器配置参数为正数。"""
        from app.config import get_settings

        settings = get_settings()
        assert settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD > 0
        assert settings.CIRCUIT_BREAKER_RECOVERY_TIMEOUT > 0
        assert settings.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS > 0

    def test_circuit_breaker_reads_config_defaults(self):
        """CircuitBreaker 从 Settings 读取默认值。"""
        from app.config import get_settings

        settings = get_settings()
        cb = CircuitBreaker(name="config_test")
        assert cb.failure_threshold == settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD
        assert cb.recovery_timeout == settings.CIRCUIT_BREAKER_RECOVERY_TIMEOUT
        assert cb.half_open_max_calls == settings.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS


# ======================================================================
# 状态 API 端点验证
# ======================================================================


class TestCircuitBreakerAPI:
    """熔断器状态 API 端点验证。"""

    def test_main_source_has_circuit_breaker_endpoint(self):
        """main.py 包含熔断器状态查询端点。"""
        import app.main as mod

        source = open(mod.__file__).read()
        assert "/health/circuit-breakers" in source
        assert "get_all_circuit_status" in source

    def test_get_all_circuit_status_callable(self):
        """get_all_circuit_status 可调用。"""
        assert callable(get_all_circuit_status)

    def test_get_all_circuit_status_returns_serializable(self):
        """状态快照可 JSON 序列化。"""
        import json

        reset_all_circuit_breakers()
        get_circuit_breaker("test_svc")
        statuses = get_all_circuit_status()
        # 确保可序列化
        json_str = json.dumps(statuses)
        parsed = json.loads(json_str)
        assert "test_svc" in parsed
