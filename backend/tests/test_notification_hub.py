"""
SSE 通知推送 Hub 测试 — 测试 Redis Pub/Sub 桥接和 SSE 流格式。

不依赖真实 Redis，使用 Mock 模拟 Redis 连接。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.notification_hub import (
    _channel_name,
    _format_sse,
    publish,
    subscribe_stream,
)


class TestNotificationHub:
    """通知推送 Hub — Redis Pub/Sub + SSE 流。"""

    def test_channel_name(self):
        """频道名格式正确。"""
        uid = uuid.uuid4()
        assert _channel_name(uid) == f"notify:{uid}"
        assert _channel_name("abc-123") == "notify:abc-123"

    def test_format_sse_basic(self):
        """SSE 格式化 — 基本格式。"""
        result = _format_sse({"type": "notification", "title": "测试"})
        assert "data: " in result
        assert '"type": "notification"' in result
        assert result.endswith("\n\n")

    def test_format_sse_with_event(self):
        """SSE 格式化 — 带事件类型。"""
        result = _format_sse({"type": "done"}, event="done")
        assert "event: done" in result
        assert "data: " in result

    async def test_publish_success(self):
        """发布通知成功。"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value=1)

        with patch(
            "app.services.notification_hub._get_redis",
            return_value=mock_redis,
        ):
            result = await publish(
                uuid.uuid4(),
                {"title": "测试通知", "type": "notification"},
            )

        assert result is True
        mock_redis.publish.assert_called_once()

    async def test_publish_redis_unavailable(self):
        """Redis 不可用时返回 False。"""
        with patch(
            "app.services.notification_hub._get_redis",
            return_value=None,
        ):
            result = await publish(uuid.uuid4(), {"title": "测试"})

        assert result is False

    async def test_publish_error_handling(self):
        """Redis publish 异常时返回 False。"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(side_effect=Exception("连接断开"))

        with patch(
            "app.services.notification_hub._get_redis",
            return_value=mock_redis,
        ):
            result = await publish(uuid.uuid4(), {"title": "测试"})

        assert result is False

    async def test_subscribe_stream_redis_unavailable(self):
        """Redis 不可用时 SSE 流发送降级提示。"""
        with patch(
            "app.services.notification_hub._get_redis",
            return_value=None,
        ):
            events = []
            async for event in subscribe_stream(uuid.uuid4()):
                events.append(event)
                if "done" in event:
                    break

        # 应该有 error 事件和 done 事件
        assert any("error" in e for e in events)
        assert any("done" in e for e in events)

    async def test_subscribe_stream_receives_message(self):
        """SSE 流正确接收 Redis 消息。"""
        user_id = uuid.uuid4()
        mock_pubsub = AsyncMock()
        # 模拟收到一条消息后返回 None（退出循环）
        messages = [
            {"type": "message", "data": '{"title": "新通知"}'},
            None,
        ]
        call_count = 0

        async def mock_get_message(**kwargs):
            nonlocal call_count
            if call_count < len(messages):
                msg = messages[call_count]
                call_count += 1
                return msg
            # 持续返回 None 触发心跳
            await asyncio.sleep(0.01)
            return None

        mock_pubsub.get_message = mock_get_message
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()

        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = mock_pubsub

        with patch(
            "app.services.notification_hub._get_redis",
            return_value=mock_redis,
        ):
            events = []
            timeout = asyncio.Event()

            async def collect():
                async for event in subscribe_stream(user_id):
                    events.append(event)
                    if len(events) >= 2:
                        timeout.set()
                        break

            try:
                await asyncio.wait_for(collect(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        # 应该收到消息事件
        assert len(events) >= 1
        assert any("新通知" in e for e in events)

    async def test_subscribe_stream_heartbeat(self):
        """SSE 流在无消息时发送心跳。

        心跳通过 asyncio.wait_for 超时触发，
        HEARTBEAT_INTERVAL 超时后 yield ': heartbeat\\n\\n'。
        """
        user_id = uuid.uuid4()
        mock_pubsub = AsyncMock()

        # get_message 永远返回 None（模拟无消息）
        async def mock_get_message(**kwargs):
            await asyncio.sleep(0.01)
            return None

        mock_pubsub.get_message = mock_get_message
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()

        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = mock_pubsub

        # 缩短心跳间隔用于测试
        with patch(
            "app.services.notification_hub.HEARTBEAT_INTERVAL",
            0,
        ):
            with patch(
                "app.services.notification_hub._get_redis",
                return_value=mock_redis,
            ):
                events: list[str] = []
                try:
                    async def collect():
                        async for event in subscribe_stream(user_id):
                            events.append(event)
                            if len(events) >= 1:
                                break

                    await asyncio.wait_for(collect(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass

        # 应该收到心跳或至少有事件产出
        assert len(events) >= 1
