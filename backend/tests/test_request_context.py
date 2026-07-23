"""Tests for app.utils.request_context — request_id contextvar 传递。"""

import pytest

from app.utils.request_context import (
    get_request_id,
    set_request_id,
    reset_request_id,
)


class TestRequestContext:
    """request_id contextvar 测试。"""

    def test_default_is_none(self) -> None:
        """非 HTTP 请求上下文中 request_id 为 None。"""
        assert get_request_id() is None

    def test_set_and_get(self) -> None:
        """set_request_id 后 get_request_id 返回设置的值。"""
        token = set_request_id("req-abc-123")
        assert get_request_id() == "req-abc-123"
        reset_request_id(token)
        assert get_request_id() is None

    def test_reset_restores_previous(self) -> None:
        """reset_request_id 恢复到之前的值。"""
        token1 = set_request_id("req-1")
        token2 = set_request_id("req-2")
        assert get_request_id() == "req-2"
        reset_request_id(token2)
        assert get_request_id() == "req-1"
        reset_request_id(token1)
        assert get_request_id() is None

    def test_nested_context(self) -> None:
        """嵌套上下文不互相干扰。"""
        token = set_request_id("outer")
        assert get_request_id() == "outer"

        # 模拟嵌套调用
        inner_token = set_request_id("inner")
        assert get_request_id() == "inner"
        reset_request_id(inner_token)
        assert get_request_id() == "outer"

        reset_request_id(token)
        assert get_request_id() is None
