"""SSE 心跳保活回归测试。

Bug 背景：原实现用 ``asyncio.wait_for(generator.__anext__(), timeout=30)``
包装心跳 — 超时即取消 pending 的 ``__anext__`` 任务，LLM 静默超过 30s
时整个流被杀死。修复后改用 ``asyncio.wait(..., timeout=...)``：超时只
发送心跳，随后继续 await 同一个 anext 任务，流不被中断。

覆盖：
- LLM 长静默后发心跳，静默结束 chunk 仍完整送达（流不被杀死）；
- pending 的 anext 任务在心跳期间不被取消/重建（同一任务被持续等待）；
- 长静默产生多个心跳；已 yield done 时不重复兜底；
- 正常速度流不产生心跳；
- 流被提前关闭时 pending 的 anext 任务被清理（不泄漏）；
- format_sse_event 基本格式（Optional 标注修正后的行为保持）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.utils import sse as sse_module
from app.utils.sse import SSEEvent, _to_sse_stream, format_sse_event


@pytest.fixture
def fast_heartbeat(monkeypatch):
    """将心跳间隔缩小到 50ms，避免测试等待真实的 30s。"""
    monkeypatch.setattr(sse_module, "_HEARTBEAT_INTERVAL", 0.05)


# ---------------------------------------------------------------------------
# 心跳不杀死慢速流
# ---------------------------------------------------------------------------


class TestHeartbeatKeepsStreamAlive:
    """慢速 / 静默生成器在心跳保活下不被中断。"""

    async def test_slow_generator_survives_heartbeat(self, fast_heartbeat):
        """LLM 静默超过心跳间隔 — 先发心跳，静默结束后 chunk 仍送达。"""

        async def slow_gen():
            await asyncio.sleep(0.2)  # 4x 心跳间隔
            yield "hello"
            yield "world"

        chunks = [c async for c in _to_sse_stream(slow_gen())]

        heartbeats = [c for c in chunks if c == ": heartbeat\n\n"]
        data = [c for c in chunks if c.startswith("data:")]
        assert len(heartbeats) >= 1, "静默期间应至少发送一个心跳"
        assert "data: hello\n\n" in data
        assert "data: world\n\n" in data
        # 末尾自动补 done 兜底事件
        assert chunks[-1].startswith("event: done")

    async def test_pending_anext_not_cancelled_on_timeout(self, fast_heartbeat):
        """超时后继续等待同一个 __anext__ 任务（不取消、不重建）。"""
        anext_calls = 0
        release = asyncio.Event()

        async def gated_gen():
            nonlocal anext_calls
            anext_calls += 1
            await release.wait()  # 若被取消则在此处抛 CancelledError
            yield "token"

        stream = _to_sse_stream(gated_gen())

        # 等待期间应先收到心跳（生成器尚未放行）
        first = await stream.__anext__()
        assert first == ": heartbeat\n\n"
        second = await stream.__anext__()
        assert second == ": heartbeat\n\n"

        # 放行生成器 — 若 anext 任务此前未被取消，chunk 正常送达
        release.set()
        third = await stream.__anext__()
        assert third == "data: token\n\n"

        # 旧实现（wait_for 超时取消重建）会使 __anext__ 被调用多次
        assert anext_calls == 1

        # 排空剩余事件（done 兜底）
        rest = [c async for c in stream]
        assert rest and rest[-1].startswith("event: done")

    async def test_multiple_heartbeats_during_long_silence(self, fast_heartbeat):
        """长静默期间应发送多个心跳；已产出 done 事件则不重复兜底。"""

        async def very_slow():
            await asyncio.sleep(0.23)  # ~4.6x 心跳间隔
            yield SSEEvent(data={"ok": True}, event="done")

        chunks = [c async for c in _to_sse_stream(very_slow())]

        heartbeats = [c for c in chunks if c == ": heartbeat\n\n"]
        assert len(heartbeats) >= 2, "长静默期间应发送多个心跳"
        # 生成器已产出 done — 不再追加兜底 done
        done_events = [c for c in chunks if c.startswith("event: done")]
        assert done_events == ['event: done\ndata: {"ok": true}\n\n']

    async def test_fast_generator_no_heartbeat(self, fast_heartbeat):
        """正常速度的流不产生心跳，行为与原实现一致。"""

        async def fast_gen():
            yield "a"
            yield "b"

        chunks = [c async for c in _to_sse_stream(fast_gen())]

        assert ": heartbeat\n\n" not in chunks
        assert chunks[:2] == ["data: a\n\n", "data: b\n\n"]
        assert chunks[-1].startswith("event: done")


# ---------------------------------------------------------------------------
# 资源清理
# ---------------------------------------------------------------------------


class TestStreamCleanup:
    """流被提前关闭时，pending 的 anext 任务必须被清理。"""

    async def test_pending_anext_cancelled_on_close(self, fast_heartbeat):
        """aclose() 关闭 SSE 流时，底层挂起的 anext 任务被取消，不泄漏。"""
        cancelled = asyncio.Event()

        async def blocking_gen():
            try:
                await asyncio.Event().wait()  # 永久挂起直到被取消
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield "x"  # pragma: no cover — 不会到达

        stream = _to_sse_stream(blocking_gen())

        # 先取一个心跳，确保底层 anext 任务已在运行
        heartbeat = await stream.__anext__()
        assert heartbeat == ": heartbeat\n\n"

        # 模拟客户端断连 — 关闭 SSE 流（GeneratorExit 抛入）
        await stream.aclose()

        # finally 中的 cancel 需要一个事件循环节拍生效
        for _ in range(5):
            await asyncio.sleep(0)
        assert cancelled.is_set(), "pending 的 anext 任务应被取消"


# ---------------------------------------------------------------------------
# format_sse_event 格式（Optional 标注修正后行为保持）
# ---------------------------------------------------------------------------


class TestFormatSSEEvent:
    """format_sse_event 协议格式测试。"""

    def test_data_only(self):
        assert format_sse_event("hello") == "data: hello\n\n"

    def test_with_event_and_id(self):
        text = format_sse_event('{"a": 1}', event="done", id="42")
        assert text == 'id: 42\nevent: done\ndata: {"a": 1}\n\n'

    def test_multiline_data(self):
        text = format_sse_event("line1\nline2")
        assert text == "data: line1\ndata: line2\n\n"

    def test_defaults_are_none(self):
        """event / id 默认 None 时不输出对应字段（Optional 标注修正）。"""
        import inspect

        sig = inspect.signature(format_sse_event)
        assert sig.parameters["event"].default is None
        assert sig.parameters["id"].default is None
        text = format_sse_event("x")
        assert "event:" not in text
        assert "id:" not in text
