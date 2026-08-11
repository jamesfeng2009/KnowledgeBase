"""
Span Sampler 测试 — 覆盖采样决策、配置读取与 LangFuse 接入。

测试范围：
    1. SpanSampler.should_sample — 采样决策优先级（root > error > 正常）
    2. 采样关闭时所有 span 上报（向后兼容）
    3. error span 强制上报（force_error 开关）
    4. 正常 span 按采样率随机（rate=0.0 全丢 / rate=1.0 全留 / 中间值统计）
    5. root span 强制上报（Trace 锚点）
    6. is_error 显式标记 vs metadata.error 推断
    7. 采样率钳制到 [0.0, 1.0]
    8. get_stats / reset 统计准确性
    9. get_default_sampler 单例与 config 读取
    10. langfuse_tracer.py 接入点（end_span / span）
        — 采样时不写 LangFuse，error span 强制写，本地 SpanRecord 不受影响
    11. config.py 配置项与验证器
"""

from __future__ import annotations

import random
import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock celery（测试环境未安装，参考 test_pii_scrubber.py / test_span_record.py）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# SpanSampler.should_sample — 采样关闭（向后兼容）
# ======================================================================


class TestSamplingDisabled:
    """采样关闭时所有 span 都上报（向后兼容）。"""

    def test_disabled_all_sampled(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=False, sampling_rate=0.1)
        # 即使 rate=0.1，关闭时也全部上报
        for _ in range(20):
            assert s.should_sample(metadata={}) is True

    def test_disabled_normal_span(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=False, sampling_rate=0.0)
        assert s.should_sample(metadata={}) is True

    def test_disabled_error_span(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=False, sampling_rate=0.0)
        assert s.should_sample(metadata={"error": "boom"}) is True

    def test_disabled_root_span(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=False, sampling_rate=0.0)
        assert s.should_sample(metadata={}, is_root=True) is True


# ======================================================================
# SpanSampler.should_sample — error span 强制上报
# ======================================================================


class TestErrorSpanForced:
    """error span 强制上报（force_error=True）。"""

    def test_error_metadata_forced_with_sampling(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        # rate=0.0 正常 span 全丢，但 error span 强制上报
        assert s.should_sample(metadata={"error": "timeout"}) is True

    def test_error_explicit_flag_forced(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        assert s.should_sample(metadata={}, is_error=True) is True

    def test_error_empty_string_not_forced(self) -> None:
        """空字符串 error 不算 error span（falsy）。"""
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        assert s.should_sample(metadata={"error": ""}) is False

    def test_error_none_not_forced(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        assert s.should_sample(metadata={"error": None}) is False

    def test_force_error_disabled_drops_error_span(self) -> None:
        """force_error=False 时 error span 也按采样率。"""
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=False)
        # error span 也被采样丢弃
        assert s.should_sample(metadata={"error": "boom"}) is False

    def test_force_error_disabled_keeps_error_at_full_rate(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=1.0, force_error=False)
        assert s.should_sample(metadata={"error": "boom"}) is True

    def test_error_priority_over_sampling(self) -> None:
        """error span 优先级高于采样率。"""
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        # 多次调用都强制上报
        for _ in range(10):
            assert s.should_sample(metadata={"error": "fail"}, is_error=True) is True


# ======================================================================
# SpanSampler.should_sample — root span 强制上报
# ======================================================================


class TestRootSpanForced:
    """root span 强制上报（Trace 锚点）。"""

    def test_root_forced_with_sampling_zero_rate(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0)
        assert s.should_sample(metadata={}, is_root=True) is True

    def test_root_priority_over_error(self) -> None:
        """root span 优先级最高（即使有 error 也走 root 分支）。"""
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=False)
        # root 优先于 error，即使 force_error=False 也强制上报
        assert s.should_sample(metadata={"error": "boom"}, is_root=True) is True

    def test_root_forced_force_error_disabled(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=False)
        assert s.should_sample(metadata={}, is_root=True) is True


# ======================================================================
# SpanSampler.should_sample — 正常 span 按采样率随机
# ======================================================================


class TestNormalSpanSampling:
    """正常 span 按采样率随机决策。"""

    def test_rate_zero_all_dropped(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=False)
        for _ in range(50):
            assert s.should_sample(metadata={}) is False

    def test_rate_one_all_sampled(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=1.0, force_error=False)
        for _ in range(50):
            assert s.should_sample(metadata={}) is True

    def test_rate_half_approximately_half(self) -> None:
        """采样率 0.5 时约一半上报（统计容差）。"""
        from app.observability.span_sampler import SpanSampler

        random.seed(42)
        s = SpanSampler(sampling_enabled=True, sampling_rate=0.5, force_error=False)
        results = [s.should_sample(metadata={}) for _ in range(1000)]
        sampled = sum(results)
        # 1000 次中应在 400-600 之间（统计容差）
        assert 400 <= sampled <= 600

    def test_rate_one_tenth_approximately_ten_percent(self) -> None:
        from app.observability.span_sampler import SpanSampler

        random.seed(123)
        s = SpanSampler(sampling_enabled=True, sampling_rate=0.1, force_error=False)
        results = [s.should_sample(metadata={}) for _ in range(1000)]
        sampled = sum(results)
        # 1000 次中应在 50-200 之间（统计容差，10%）
        assert 50 <= sampled <= 200

    def test_normal_span_no_error_not_forced(self) -> None:
        """正常 span（无 error）不强制上报。"""
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        # force_error=True 但 span 无 error → 走采样分支
        assert s.should_sample(metadata={}) is False


# ======================================================================
# SpanSampler.should_sample — is_error 显式标记 vs metadata 推断
# ======================================================================


class TestErrorInference:
    """error 状态推断：显式 is_error 优先于 metadata.error。"""

    def test_explicit_error_true_overrides_no_metadata(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        assert s.should_sample(metadata={}, is_error=True) is True

    def test_explicit_error_false_overrides_metadata_error(self) -> None:
        """显式 is_error=False 优先于 metadata.error（调用方明确说不是 error）。"""
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        # metadata 有 error 但 is_error=False → 不强制
        assert s.should_sample(metadata={"error": "boom"}, is_error=False) is False

    def test_is_error_none_infers_from_metadata(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        # is_error=None → 从 metadata.error 推断
        assert s.should_sample(metadata={"error": "fail"}, is_error=None) is True

    def test_is_error_none_no_metadata_no_error(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        assert s.should_sample(metadata=None, is_error=None) is False

    def test_metadata_none_with_is_error_true(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        assert s.should_sample(metadata=None, is_error=True) is True


# ======================================================================
# SpanSampler — 采样率钳制
# ======================================================================


class TestRateClamping:
    """采样率钳制到 [0.0, 1.0]。"""

    def test_negative_rate_clamped_to_zero(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=-0.5, force_error=False)
        assert s.rate == 0.0
        # rate=0.0 → 正常 span 全丢
        assert s.should_sample(metadata={}) is False

    def test_rate_above_one_clamped_to_one(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=1.5, force_error=False)
        assert s.rate == 1.0
        # rate=1.0 → 正常 span 全留
        assert s.should_sample(metadata={}) is True

    def test_rate_normal_value(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.3, force_error=False)
        assert s.rate == 0.3


# ======================================================================
# SpanSampler — 属性与统计
# ======================================================================


class TestSpanSamplerProperties:
    """SpanSampler 属性访问。"""

    def test_enabled_property(self) -> None:
        from app.observability.span_sampler import SpanSampler

        assert SpanSampler(sampling_enabled=True).enabled is True
        assert SpanSampler(sampling_enabled=False).enabled is False

    def test_rate_property(self) -> None:
        from app.observability.span_sampler import SpanSampler

        assert SpanSampler(sampling_rate=0.3).rate == 0.3

    def test_force_error_property(self) -> None:
        from app.observability.span_sampler import SpanSampler

        assert SpanSampler(force_error=True).force_error is True
        assert SpanSampler(force_error=False).force_error is False


class TestSpanSamplerStats:
    """SpanSampler 统计与重置。"""

    def test_stats_initial_zero(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.5, force_error=True)
        stats = s.get_stats()
        assert stats["sampled_count"] == 0
        assert stats["dropped_count"] == 0
        assert stats["forced_error_count"] == 0
        assert stats["forced_root_count"] == 0

    def test_stats_after_sampling(self) -> None:
        from app.observability.span_sampler import SpanSampler

        random.seed(0)
        s = SpanSampler(sampling_enabled=True, sampling_rate=0.5, force_error=True)
        for _ in range(100):
            s.should_sample(metadata={})
        stats = s.get_stats()
        assert stats["sampled_count"] + stats["dropped_count"] == 100

    def test_stats_forced_error_count(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        s.should_sample(metadata={"error": "e1"})
        s.should_sample(metadata={"error": "e2"})
        stats = s.get_stats()
        assert stats["forced_error_count"] == 2

    def test_stats_forced_root_count(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        s.should_sample(metadata={}, is_root=True)
        s.should_sample(metadata={}, is_root=True)
        stats = s.get_stats()
        assert stats["forced_root_count"] == 2

    def test_reset_clears_counts(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=1.0, force_error=True)
        s.should_sample(metadata={})
        s.should_sample(metadata={"error": "e"})
        s.should_sample(metadata={}, is_root=True)
        s.reset()
        stats = s.get_stats()
        assert stats["sampled_count"] == 0
        assert stats["dropped_count"] == 0
        assert stats["forced_error_count"] == 0
        assert stats["forced_root_count"] == 0

    def test_stats_contains_config_fields(self) -> None:
        from app.observability.span_sampler import SpanSampler

        s = SpanSampler(sampling_enabled=True, sampling_rate=0.3, force_error=True)
        stats = s.get_stats()
        assert stats["enabled"] is True
        assert stats["rate"] == 0.3
        assert stats["force_error"] is True


# ======================================================================
# get_default_sampler — 单例与 config 读取
# ======================================================================


class TestDefaultSampler:
    """get_default_sampler 单例与 config 集成。"""

    def teardown_method(self) -> None:
        from app.observability.span_sampler import reset_default_sampler

        reset_default_sampler()

    def test_returns_singleton(self) -> None:
        from app.observability.span_sampler import get_default_sampler

        s1 = get_default_sampler()
        s2 = get_default_sampler()
        assert s1 is s2

    def test_reset_clears_singleton(self) -> None:
        from app.observability.span_sampler import (
            get_default_sampler,
            reset_default_sampler,
        )

        s1 = get_default_sampler()
        reset_default_sampler()
        s2 = get_default_sampler()
        assert s1 is not s2

    def test_reads_config_defaults(self) -> None:
        from app.observability.span_sampler import get_default_sampler

        s = get_default_sampler()
        # config 默认：sampling_enabled=False, rate=0.1, force_error=True
        assert s.enabled is False
        assert s.rate == 0.1
        assert s.force_error is True

    def test_reads_config_overrides(self) -> None:
        from app.observability.span_sampler import (
            get_default_sampler,
            reset_default_sampler,
        )

        reset_default_sampler()
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                LANGFUSE_SAMPLING_ENABLED=True,
                LANGFUSE_SAMPLING_RATE=0.25,
                LANGFUSE_SAMPLING_FORCE_ERROR=False,
            )
            s = get_default_sampler()
            assert s.enabled is True
            assert s.rate == 0.25
            assert s.force_error is False

    def test_config_read_failure_defaults_disabled(self) -> None:
        from app.observability.span_sampler import (
            get_default_sampler,
            reset_default_sampler,
        )

        reset_default_sampler()
        with patch("app.config.get_settings", side_effect=Exception("boom")):
            s = get_default_sampler()
            # 读取失败 → 默认禁用采样（全上报，向后兼容）
            assert s.enabled is False
            assert s.force_error is True


# ======================================================================
# config.py — 配置项与验证器
# ======================================================================


class TestConfigFields:
    """config.py 采样配置项与验证器。"""

    def test_config_has_sampling_fields(self) -> None:
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "LANGFUSE_SAMPLING_ENABLED")
        assert hasattr(s, "LANGFUSE_SAMPLING_RATE")
        assert hasattr(s, "LANGFUSE_SAMPLING_FORCE_ERROR")

    def test_config_defaults(self) -> None:
        from app.config import get_settings

        s = get_settings()
        assert s.LANGFUSE_SAMPLING_ENABLED is False
        assert s.LANGFUSE_SAMPLING_RATE == 0.1
        assert s.LANGFUSE_SAMPLING_FORCE_ERROR is True

    def test_sampling_rate_validator_rejects_negative(self) -> None:
        """采样率 < 0 应被验证器拒绝。"""
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError):
            Settings(LANGFUSE_SAMPLING_RATE=-0.1)

    def test_sampling_rate_validator_rejects_above_one(self) -> None:
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError):
            Settings(LANGFUSE_SAMPLING_RATE=1.5)

    def test_sampling_rate_validator_accepts_zero(self) -> None:
        from app.config import Settings

        s = Settings(LANGFUSE_SAMPLING_RATE=0.0)
        assert s.LANGFUSE_SAMPLING_RATE == 0.0

    def test_sampling_rate_validator_accepts_one(self) -> None:
        from app.config import Settings

        s = Settings(LANGFUSE_SAMPLING_RATE=1.0)
        assert s.LANGFUSE_SAMPLING_RATE == 1.0

    def test_sampling_rate_validator_accepts_half(self) -> None:
        from app.config import Settings

        s = Settings(LANGFUSE_SAMPLING_RATE=0.5)
        assert s.LANGFUSE_SAMPLING_RATE == 0.5


# ======================================================================
# langfuse_tracer.py 接入 — end_span 采样
# ======================================================================


class TestEndSpanSampling:
    """TraceContext.end_span 采样集成。"""

    def _make_trace_ctx(
        self,
        trace_mock: MagicMock | None = None,
        recorder: MagicMock | None = None,
    ) -> "TraceContext":
        from app.observability.langfuse_tracer import TraceContext

        ctx = TraceContext(recorder=recorder)
        ctx._trace = trace_mock
        return ctx

    def test_end_span_sampled_writes_langfuse(self) -> None:
        trace_mock = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            mock_sampler.return_value.should_sample.return_value = True
            ctx.end_span(
                span_id=None,
                name="think_iter0",
                output_data={"result": "ok"},
                metadata={"latency_ms": 10.0},
            )
            assert trace_mock.span.called

    def test_end_span_dropped_skips_langfuse(self) -> None:
        """采样丢弃时不写 LangFuse span。"""
        trace_mock = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            mock_sampler.return_value.should_sample.return_value = False
            ctx.end_span(
                span_id=None,
                name="think_iter0",
                output_data={"result": "ok"},
                metadata={"latency_ms": 10.0},
            )
            assert not trace_mock.span.called

    def test_end_span_error_forced_writes(self) -> None:
        """error span 强制写 LangFuse（即使采样器说丢弃）。"""
        trace_mock = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            # should_sample 返回 True（error span 强制）
            mock_sampler.return_value.should_sample.return_value = True
            ctx.end_span(
                span_id=None,
                name="think_iter0",
                output_data={"error": "timeout"},
                metadata={"latency_ms": 10.0, "error": "timeout"},
            )
            assert trace_mock.span.called
            # 验证 is_error 传了 True
            call_kwargs = mock_sampler.return_value.should_sample.call_args
            assert call_kwargs.kwargs.get("is_error") is True

    def test_end_span_sampler_receives_metadata(self) -> None:
        trace_mock = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            mock_sampler.return_value.should_sample.return_value = True
            ctx.end_span(
                span_id=None,
                name="retrieve_iter0",
                metadata={"latency_ms": 5.0, "retrieved_docs": 3},
            )
            call_kwargs = mock_sampler.return_value.should_sample.call_args
            passed_meta = call_kwargs.kwargs.get("metadata")
            assert isinstance(passed_meta, dict)
            assert "latency_ms" in passed_meta

    def test_end_span_local_recorder_not_affected_by_sampling(self) -> None:
        """本地 SpanRecord 不受采样影响（评测需完整数据）。"""
        trace_mock = MagicMock()
        recorder = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock, recorder=recorder)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            # 采样丢弃 LangFuse
            mock_sampler.return_value.should_sample.return_value = False
            ctx.end_span(
                span_id="span-1",
                name="think_iter0",
                output_data={"result": "ok"},
                metadata={"latency_ms": 10.0},
            )
            # 本地 SpanRecord 仍被写入
            assert recorder.end_span.called
            assert not trace_mock.span.called


# ======================================================================
# langfuse_tracer.py 接入 — span() 采样
# ======================================================================


class TestSpanMethodSampling:
    """TraceContext.span() 采样集成。"""

    def _make_trace_ctx(
        self,
        trace_mock: MagicMock | None = None,
        recorder: MagicMock | None = None,
    ) -> "TraceContext":
        from app.observability.langfuse_tracer import TraceContext

        ctx = TraceContext(recorder=recorder)
        ctx._trace = trace_mock
        return ctx

    def test_span_sampled_writes_langfuse(self) -> None:
        trace_mock = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            mock_sampler.return_value.should_sample.return_value = True
            ctx.span(
                name="retrieve",
                input_data={"q": "test"},
                output_data={"docs": []},
            )
            assert trace_mock.span.called

    def test_span_dropped_skips_langfuse(self) -> None:
        trace_mock = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            mock_sampler.return_value.should_sample.return_value = False
            ctx.span(
                name="retrieve",
                input_data={"q": "test"},
                output_data={"docs": []},
            )
            assert not trace_mock.span.called

    def test_span_error_forced_writes(self) -> None:
        trace_mock = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            mock_sampler.return_value.should_sample.return_value = True
            ctx.span(
                name="think",
                output_data={"error": "fail"},
                metadata={"error": "fail"},
            )
            assert trace_mock.span.called
            call_kwargs = mock_sampler.return_value.should_sample.call_args
            assert call_kwargs.kwargs.get("is_error") is True

    def test_span_local_recorder_not_affected(self) -> None:
        """本地 SpanRecord 不受采样影响。"""
        trace_mock = MagicMock()
        recorder = MagicMock()
        ctx = self._make_trace_ctx(trace_mock=trace_mock, recorder=recorder)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            mock_sampler.return_value.should_sample.return_value = False
            ctx.span(
                name="retrieve",
                input_data={"q": "test"},
                output_data={"docs": []},
            )
            assert recorder.record_closed.called
            assert not trace_mock.span.called

    def test_span_no_trace_returns_early(self) -> None:
        """无 LangFuse trace 时 span() 提前返回。"""
        ctx = self._make_trace_ctx(trace_mock=None)
        # 不应抛异常
        ctx.span(name="retrieve", output_data={"ok": True})


# ======================================================================
# langfuse_tracer.py — finalize 不采样（根 trace 更新非 span 创建）
# ======================================================================


class TestFinalizeNoSampling:
    """finalize 更新根 trace（非创建 span），不受采样影响。"""

    def test_finalize_updates_trace_regardless_of_sampling(self) -> None:
        from app.observability.langfuse_tracer import TraceContext

        trace_mock = MagicMock()
        ctx = TraceContext()
        ctx._trace = trace_mock
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler"
        ) as mock_sampler:
            mock_sampler.return_value.should_sample.return_value = False
            ctx.finalize(output={"answer": "done"}, metadata={"tokens": 100})
            # finalize 调用 trace.update（非 span），不受采样影响
            assert trace_mock.update.called


# ======================================================================
# 端到端 — 采样器与 tracer 协作
# ======================================================================


class TestEndToEndSampling:
    """端到端：采样器配置 → tracer 行为。"""

    def teardown_method(self) -> None:
        from app.observability.span_sampler import reset_default_sampler

        reset_default_sampler()

    def test_disabled_sampling_all_spans_written(self) -> None:
        """采样禁用时所有 span 都写 LangFuse。"""
        from app.observability.langfuse_tracer import TraceContext
        from app.observability.span_sampler import (
            SpanSampler,
            reset_default_sampler,
        )

        reset_default_sampler()
        trace_mock = MagicMock()
        ctx = TraceContext()
        ctx._trace = trace_mock

        with patch(
            "app.observability.langfuse_tracer._get_span_sampler",
            return_value=SpanSampler(sampling_enabled=False, sampling_rate=0.0),
        ):
            for i in range(10):
                ctx.span(name=f"node_{i}", output_data={"ok": True})
            assert trace_mock.span.call_count == 10

    def test_enabled_sampling_with_error_forced(self) -> None:
        """采样开启 + rate=0 时 error span 仍写 LangFuse。"""
        from app.observability.langfuse_tracer import TraceContext
        from app.observability.span_sampler import (
            SpanSampler,
            reset_default_sampler,
        )

        reset_default_sampler()
        trace_mock = MagicMock()
        ctx = TraceContext()
        ctx._trace = trace_mock

        sampler = SpanSampler(sampling_enabled=True, sampling_rate=0.0, force_error=True)
        with patch(
            "app.observability.langfuse_tracer._get_span_sampler",
            return_value=sampler,
        ):
            # 正常 span 被丢弃
            ctx.span(name="normal", output_data={"ok": True})
            assert trace_mock.span.call_count == 0
            # error span 强制写入
            ctx.span(
                name="error_node",
                output_data={"error": "fail"},
                metadata={"error": "fail"},
            )
            assert trace_mock.span.call_count == 1
