"""
Deep Research 进度事件源（P4-SSE）测试。

覆盖：
    - research() 的 progress 回调：依次产出 decomposed / subtopic / overview 事件；
    - publish_progress：Redis 不可用返回 False 不抛错；可用时写快照并发布；
    - subscribe_stream：Redis 不可用降级为 error 事件；
      快照含 done 时回放后直接收尾（断线重连正确性）；
    - API：/result 认证强制 + PENDING/SUCCESS 分支；/stream 认证强制。

mock 风格参照 notification / recommendation 测试，不依赖外部 Redis。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio


class _FakeLLM:
    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses

    async def chat(self, messages, stream=True):
        prompt = messages[0].get("content", "")
        for kw, text in self._responses.items():
            if kw in prompt:
                yield text
                return
        yield "default"


class _FakeRetriever:
    def __init__(self, docs=None) -> None:
        self._docs = docs or []

    async def search(self, query, kb_ids=None, top_k=5):
        return self._docs


def _internal(title: str, score: float, content: str = "") -> dict:
    return {"doc_id": f"/kb/{title}", "metadata": {"title": title},
            "content": content or f"{title}内部内容", "score": score}


def _collector():
    events: list[dict] = []

    async def _cb(event: dict) -> None:
        events.append(event)

    return events, _cb


def _make_user():
    return SimpleNamespace(id=uuid4(), role="editor", is_active=True)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest_asyncio.fixture
async def raw_client():
    """无认证覆盖的客户端 — 用于测试认证强制。"""
    from app.main import app
    from app.middleware import get_rate_limiter

    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()
    app.dependency_overrides.clear()
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.clear()


# ======================================================================
# research() 进度回调
# ======================================================================


@pytest.mark.asyncio
async def test_research_emits_progress_events() -> None:
    from app.services.deep_research_service import DeepResearchService

    llm = _FakeLLM({
        "研究课题规划专家": "主题A\n主题B",
        "知识库文档": '{"conclusion":"结论。", "confidence":0.8}',
        "研究报告撰写专家": "摘要",
    })
    retriever = _FakeRetriever([_internal("甲", 0.9)])
    service = DeepResearchService(llm, retriever)

    events, cb = _collector()
    await service.research("调研某主题", kb_ids=None, progress=cb)

    types = [e["type"] for e in events]
    assert types == ["decomposed", "subtopic", "subtopic", "overview"]
    assert events[0]["topics"] == ["主题A", "主题B"]
    assert events[1]["index"] == 0 and events[1]["total"] == 2
    assert events[3]["type"] == "overview" and "summary" in events[3]


# ======================================================================
# research_progress 模块
# ======================================================================


@pytest.mark.asyncio
async def test_publish_progress_redis_down_returns_false() -> None:
    from app.services import research_progress

    with patch.object(research_progress, "_get_redis", new=AsyncMock(return_value=None)):
        ok = await research_progress.publish_progress(
            "t1", {"type": "decomposed"}
        )
    assert ok is False


@pytest.mark.asyncio
async def test_publish_progress_writes_snapshot_and_publishes() -> None:
    from app.services import research_progress

    redis = MagicMock()
    redis.rpush = AsyncMock(return_value=1)
    redis.ltrim = AsyncMock(return_value=True)
    redis.publish = AsyncMock(return_value=1)
    with patch.object(research_progress, "_get_redis", new=AsyncMock(return_value=redis)):
        ok = await research_progress.publish_progress(
            "t9", {"type": "subtopic", "subtopic": "A"}
        )
    assert ok is True
    redis.rpush.assert_awaited_once()
    redis.ltrim.assert_awaited_once()
    redis.publish.assert_awaited_once()
    payload = redis.publish.await_args.args[1]
    assert json.loads(payload)["type"] == "subtopic"


@pytest.mark.asyncio
async def test_subscribe_stream_degrades_when_redis_down() -> None:
    from app.services import research_progress

    with patch.object(
        research_progress, "_get_redis", new=AsyncMock(return_value=None)
    ):
        chunks = [c async for c in research_progress.subscribe_stream("t1")]
    assert any("error" in c for c in chunks)


@pytest.mark.asyncio
async def test_subscribe_stream_replays_snapshot_done() -> None:
    """快照末尾已含 done：回放后直接收尾，不再空等实时事件。"""
    from app.services import research_progress

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock(return_value=None)
    pubsub.unsubscribe = AsyncMock(return_value=None)
    pubsub.get_message = AsyncMock(return_value=None)

    redis = MagicMock()
    redis.pubsub.return_value = pubsub
    redis.lrange = AsyncMock(return_value=[
        json.dumps({"type": "decomposed", "topics": ["A"]}, ensure_ascii=False),
        json.dumps({"type": "done"}, ensure_ascii=False),
    ])

    with patch.object(research_progress, "_get_redis", new=AsyncMock(return_value=redis)):
        chunks = [c async for c in research_progress.subscribe_stream("t7")]

    text = "\n".join(chunks)
    assert '"decomposed"' in text
    assert "event: done" in text
    # 已见 done，未进入实时等待
    assert pubsub.get_message.await_count == 0


# ======================================================================
# API：/result 与 /stream
# ======================================================================


def test_result_pending_and_success() -> None:
    """/result：PENDING → 进行中；SUCCESS → 返回报告。"""
    from app.api.v1.research import get_research_result

    with patch("celery.result.AsyncResult") as ar_cls:
        ar_cls.return_value = SimpleNamespace(state="PENDING")
        assert _run(get_research_result("tid", _make_user())).data["status"] == "pending"

        ar_cls.return_value = SimpleNamespace(
            state="SUCCESS", result={"goal": "g", "summary": "s"}
        )
        r = _run(get_research_result("tid", _make_user()))
        assert r.data["status"] == "success"
        assert r.data["report"]["summary"] == "s"


def test_stream_requires_auth(raw_client) -> None:
    """未认证访问 /stream 应 401。"""
    assert _run(raw_client.get("/api/v1/research/x/stream")).status_code == 401


def test_result_requires_auth(raw_client) -> None:
    """未认证访问 /result 应 401。"""
    assert _run(raw_client.get("/api/v1/research/x/result")).status_code == 401