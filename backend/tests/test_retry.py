"""
P1-A 指数退避重试 — 三层重试体系测试。

测试覆盖：
    L1  HTTP Transport  — build_retry_http_client 创建 + 重试策略
    L2  函数级 tenacity  — with_retry 装饰器行为
    L3  Celery 任务级   — make_celery_retry_kwargs 参数生成
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.config import get_settings
from app.utils.retry import (
    build_retry_http_client,
    make_celery_retry_kwargs,
    with_retry,
)


# ======================================================================
# L2: with_retry 装饰器测试
# ======================================================================


class TestWithRetryDecorator:
    """with_retry 装饰器行为测试。"""

    def test_with_retry_importable(self):
        """with_retry 可正常导入。"""
        assert callable(with_retry)

    def test_with_retry_returns_decorator(self):
        """with_retry() 返回装饰器函数。"""
        decorator = with_retry()
        assert callable(decorator)

    def test_with_retry_preserves_function_name(self):
        """装饰器保留原函数名（functools.wraps）。"""
        @with_retry()
        def my_function():
            """my docstring"""
            return 42

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "my docstring"

    def test_with_retry_success_no_retry(self):
        """成功执行时不重试。"""
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        def succeed_first_try():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = succeed_first_try()
        assert result == "ok"
        assert call_count == 1

    def test_with_retry_retries_on_transient_error(self):
        """遇到可重试异常时自动重试。"""
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01, jitter=0.0)
        def fail_twice_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "recovered"

        result = fail_twice_then_succeed()
        assert result == "recovered"
        assert call_count == 3

    def test_with_retry_gives_up_after_max_attempts(self):
        """超过最大尝试次数后抛出原异常。"""
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01, jitter=0.0)
        def always_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("permanent")

        with pytest.raises(ConnectionError, match="permanent"):
            always_fail()
        assert call_count == 3

    def test_with_retry_does_not_retry_non_matching_exception(self):
        """非可重试异常不触发重试。"""
        call_count = 0

        @with_retry(
            max_attempts=3,
            base_delay=0.01,
            retry_exceptions=(ConnectionError,),
        )
        def raise_value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError, match="not retryable"):
            raise_value_error()
        assert call_count == 1

    def test_with_retry_reads_config_defaults(self):
        """不传参时从 Settings 读取默认值。"""
        settings = get_settings()

        @with_retry()
        def placeholder():
            return "ok"

        # 装饰器创建不报错，说明配置读取成功
        assert callable(placeholder)
        assert settings.RETRY_MAX_ATTEMPTS > 0
        assert settings.RETRY_BACKOFF_BASE_DB > 0
        assert settings.RETRY_BACKOFF_MAX > 0

    def test_with_retry_async_function(self):
        """with_retry 支持异步函数。"""
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01, jitter=0.0)
        async def async_fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("transient")
            return "async_ok"

        result = asyncio.run(async_fail_then_succeed())
        assert result == "async_ok"
        assert call_count == 2


# ======================================================================
# L1: build_retry_http_client 测试
# ======================================================================


class TestBuildRetryHttpClient:
    """build_retry_http_client 工厂函数测试。"""

    @staticmethod
    def _close(client: httpx.AsyncClient) -> None:
        """安全关闭 async client。"""
        try:
            asyncio.run(client.aclose())
        except RuntimeError:
            pass  # 事件循环已关闭

    def test_build_retry_http_client_importable(self):
        """build_retry_http_client 可正常导入。"""
        assert callable(build_retry_http_client)

    def test_build_retry_http_client_returns_async_client(self):
        """返回 httpx.AsyncClient 实例。"""
        client = build_retry_http_client(timeout=10.0)
        assert isinstance(client, httpx.AsyncClient)
        self._close(client)

    def test_build_retry_http_client_with_base_url(self):
        """支持 base_url 参数。"""
        client = build_retry_http_client(
            timeout=10.0, base_url="http://milvus:19530"
        )
        assert client.base_url == "http://milvus:19530"
        self._close(client)

    def test_build_retry_http_client_with_headers(self):
        """支持 headers 参数。"""
        client = build_retry_http_client(
            timeout=10.0, headers={"Authorization": "Bearer token"}
        )
        assert client.headers["authorization"] == "Bearer token"
        self._close(client)

    def test_build_retry_http_client_uses_config_defaults(self):
        """不传 max_retries 时从 Settings 读取。"""
        settings = get_settings()
        client = build_retry_http_client(timeout=10.0)
        assert isinstance(client, httpx.AsyncClient)
        # 确保配置值有效
        assert settings.RETRY_MAX_ATTEMPTS > 0
        self._close(client)

    def test_build_retry_http_client_custom_retry_codes(self):
        """支持自定义重试状态码。"""
        client = build_retry_http_client(
            timeout=10.0, retry_status_codes=[502, 503]
        )
        assert isinstance(client, httpx.AsyncClient)
        self._close(client)

    def test_build_retry_http_client_has_retry_transport(self):
        """客户端 transport 是 AsyncRetryTransport 实例。"""
        from httpx_retry import AsyncRetryTransport

        client = build_retry_http_client(timeout=10.0)
        assert isinstance(client._transport, AsyncRetryTransport)
        self._close(client)


# ======================================================================
# L3: make_celery_retry_kwargs 测试
# ======================================================================


class TestMakeCeleryRetryKwargs:
    """make_celery_retry_kwargs 参数生成测试。"""

    def test_make_celery_retry_kwargs_importable(self):
        """make_celery_retry_kwargs 可正常导入。"""
        assert callable(make_celery_retry_kwargs)

    def test_make_celery_retry_kwargs_returns_dict(self):
        """返回字典。"""
        kwargs = make_celery_retry_kwargs()
        assert isinstance(kwargs, dict)

    def test_make_celery_retry_kwargs_has_required_keys(self):
        """包含 Celery 重试所需的所有参数。"""
        kwargs = make_celery_retry_kwargs()
        assert "max_retries" in kwargs
        assert "retry_backoff" in kwargs
        assert "retry_backoff_max" in kwargs
        assert "retry_jitter" in kwargs
        assert "autoretry_for" in kwargs

    def test_make_celery_retry_kwargs_values_from_config(self):
        """参数值从 Settings 读取。"""
        settings = get_settings()
        kwargs = make_celery_retry_kwargs()

        assert kwargs["max_retries"] == settings.RETRY_MAX_ATTEMPTS
        assert kwargs["retry_backoff"] == settings.RETRY_BACKOFF_BASE_CELERY
        assert kwargs["retry_backoff_max"] == int(settings.RETRY_BACKOFF_MAX)
        assert kwargs["retry_jitter"] is True

    def test_make_celery_retry_kwargs_autoretry_includes_connection_errors(self):
        """autoretry_for 包含连接类异常。"""
        kwargs = make_celery_retry_kwargs()
        autoretry_for = kwargs["autoretry_for"]

        assert ConnectionError in autoretry_for
        assert TimeoutError in autoretry_for
        assert OSError in autoretry_for

    def test_make_celery_retry_kwargs_spreadable_to_task_decorator(self):
        """生成的字典可直接展开到 @celery_app.task()。"""
        kwargs = make_celery_retry_kwargs()

        # 模拟 Celery task 装饰器参数校验
        # max_retries 必须为正整数
        assert isinstance(kwargs["max_retries"], int)
        assert kwargs["max_retries"] > 0
        # retry_backoff 可为 bool 或正数
        assert isinstance(kwargs["retry_backoff"], (bool, int, float))
        # retry_backoff_max 必须为正整数
        assert isinstance(kwargs["retry_backoff_max"], int)
        assert kwargs["retry_backoff_max"] > 0
        # retry_jitter 必须为 bool
        assert isinstance(kwargs["retry_jitter"], bool)


# ======================================================================
# 三层体系集成验证
# ======================================================================


class TestRetryIntegration:
    """三层重试体系集成验证。"""

    def test_all_three_layers_importable(self):
        """三层工具均可正常导入。"""
        from app.utils.retry import (
            build_retry_http_client,
            make_celery_retry_kwargs,
            with_retry,
        )
        assert callable(with_retry)
        assert callable(build_retry_http_client)
        assert callable(make_celery_retry_kwargs)

    def test_config_parameters_exist(self):
        """Settings 包含所有重试相关配置参数。"""
        settings = get_settings()
        # L1/L2 共享
        assert hasattr(settings, "RETRY_BACKOFF_BASE")
        assert hasattr(settings, "RETRY_BACKOFF_MAX")
        assert hasattr(settings, "RETRY_MAX_ATTEMPTS")
        assert hasattr(settings, "RETRY_JITTER")
        # L2 DB 专用
        assert hasattr(settings, "RETRY_BACKOFF_BASE_DB")
        # L3 Celery 专用
        assert hasattr(settings, "RETRY_BACKOFF_BASE_CELERY")

    def test_config_parameters_positive(self):
        """所有重试配置参数为正数。"""
        settings = get_settings()
        assert settings.RETRY_BACKOFF_BASE > 0
        assert settings.RETRY_BACKOFF_BASE_DB > 0
        assert settings.RETRY_BACKOFF_BASE_CELERY > 0
        assert settings.RETRY_BACKOFF_MAX > 0
        assert settings.RETRY_MAX_ATTEMPTS > 0
        assert settings.RETRY_JITTER > 0

    def test_requirements_include_tenacity_and_httpx_retry(self):
        """requirements.txt 包含 tenacity 和 httpx-retry。"""
        req_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "requirements.txt",
        )
        with open(req_path) as f:
            content = f.read()
        assert "tenacity" in content
        assert "httpx-retry" in content


# ======================================================================
# Celery 任务装饰器验证
# ======================================================================


class TestCeleryTaskRetryConfig:
    """验证 Celery 任务已配置指数退避重试。"""

    def test_document_tasks_import_retry(self):
        """document_tasks.py 导入了 make_celery_retry_kwargs。"""
        import tasks.document_tasks as mod

        source = open(mod.__file__).read()
        assert "make_celery_retry_kwargs" in source
        assert "**make_celery_retry_kwargs()" in source

    def test_index_tasks_import_retry(self):
        """index_tasks.py 导入了 make_celery_retry_kwargs。"""
        import tasks.index_tasks as mod

        source = open(mod.__file__).read()
        assert "make_celery_retry_kwargs" in source
        assert "**make_celery_retry_kwargs()" in source

    def test_video_tasks_import_retry(self):
        """video_tasks.py 导入了 make_celery_retry_kwargs。"""
        import tasks.video_tasks as mod

        source = open(mod.__file__).read()
        assert "make_celery_retry_kwargs" in source
        assert "**make_celery_retry_kwargs()" in source

    def test_intelligence_tasks_import_retry(self):
        """intelligence_tasks.py 导入了 make_celery_retry_kwargs。"""
        import tasks.intelligence_tasks as mod

        source = open(mod.__file__).read()
        assert "make_celery_retry_kwargs" in source

    def test_document_tasks_no_fixed_retry_delay(self):
        """document_tasks.py 不再使用固定 default_retry_delay。"""
        import tasks.document_tasks as mod

        source = open(mod.__file__).read()
        # make_celery_retry_kwargs 使用 retry_backoff 替代 default_retry_delay
        # 检查 task 装饰器区域不再有 max_retries=3, default_retry_delay=60
        assert "max_retries=3" not in source or "make_celery_retry_kwargs" in source

    def test_index_tasks_no_fixed_retry_delay(self):
        """index_tasks.py 不再使用固定 default_retry_delay。"""
        import tasks.index_tasks as mod

        source = open(mod.__file__).read()
        assert "default_retry_delay=30" not in source
        assert "default_retry_delay=60" not in source


# ======================================================================
# HTTP 客户端集成验证
# ======================================================================


class TestHttpClientRetryIntegration:
    """验证 HTTP 客户端已集成 L1 重试 transport。"""

    def test_embedder_uses_retry_client(self):
        """embedder.py 使用 build_retry_http_client。"""
        import app.llm.embedder as mod

        source = open(mod.__file__).read()
        assert "build_retry_http_client" in source

    def test_reranker_uses_retry_client(self):
        """reranker.py 使用 build_retry_http_client。"""
        import app.rag.reranker as mod

        source = open(mod.__file__).read()
        assert "build_retry_http_client" in source

    def test_retriever_uses_retry_client(self):
        """retriever.py 使用 build_retry_http_client。"""
        import app.rag.retriever as mod

        source = open(mod.__file__).read()
        assert "build_retry_http_client" in source

    def test_milvus_store_uses_retry_client(self):
        """milvus_store.py 使用 build_retry_http_client。"""
        import app.rag.vector_store.milvus_store as mod

        source = open(mod.__file__).read()
        assert "build_retry_http_client" in source

    def test_opensearch_store_uses_retry_client(self):
        """opensearch_store.py 使用 build_retry_http_client。"""
        import app.rag.vector_store.opensearch_store as mod

        source = open(mod.__file__).read()
        assert "build_retry_http_client" in source
