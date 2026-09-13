"""
Deep Research 进度事件源（P4-SSE）测试。

覆盖：
    - research() 的 progress 回调：依次产出 decomposed / subtopic / overview 事件；
    - publish_progress：Redis 不可用返回 False 不抛错；可用时写快照并发布；
    - 最终结果持久化：save_result / save_failure / load_result（Redis 不可用降级、
      带 TTL、无记录返回 None）
    - subscribe_stream：Redis 不可用降级为 error 事件；
      快照含 done 时回放后直接收尾（断线重连正确性）；
    - API：/result 认证强制 + 无记录/success/failed 分支；/stream 认证强制。

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
    redis.expire = AsyncMock(return_value=True)
    redis.publish = AsyncMock(return_value=1)
    with patch.object(research_progress, "_get_redis", new=AsyncMock(return_value=redis)):
        ok = await research_progress.publish_progress(
            "t9", {"type": "subtopic", "subtopic": "A"}
        )
    assert ok is True
    redis.rpush.assert_awaited_once()
    redis.ltrim.assert_awaited_once()
    redis.expire.assert_awaited_once()
    redis.publish.assert_awaited_once()
    # 快照 key 每次发布重设 TTL，防止键无限累积
    exp_key = redis.expire.await_args.args[0]
    assert exp_key == "research_events:t9"
    assert redis.expire.await_args.args[1] == research_progress.SNAPSHOT_TTL
    payload = redis.publish.await_args.args[1]
    assert json.loads(payload)["type"] == "subtopic"


@pytest.mark.asyncio
async def test_save_result_redis_down_returns_false() -> None:
    """Redis 不可用时 save_result 返回 False 不抛错。"""
    from app.services import research_progress

    with patch.object(research_progress, "_get_redis", new=AsyncMock(return_value=None)):
        ok = await research_progress.save_result("t1", {"summary": "s"})
    assert ok is False


@pytest.mark.asyncio
async def test_save_and_load_result_roundtrip() -> None:
    """save_result 写入带 TTL 的结果；load_result 可读回成功报告。"""
    from app.services import research_progress

    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=json.dumps(
        {"status": "success", "report": {"summary": "结论"}}, ensure_ascii=False
    ))

    with patch.object(research_progress, "_get_redis", new=AsyncMock(return_value=redis)):
        ok = await research_progress.save_result("t9", {"summary": "结论"})
        data = await research_progress.load_result("t9")

    assert ok is True
    key = redis.set.await_args.args[0]
    assert key == "research_result:t9"
    assert redis.set.await_args.kwargs.get("ex") == research_progress.RESULT_TTL
    assert data == {"status": "success", "report": {"summary": "结论"}}


@pytest.mark.asyncio
async def test_save_failure_and_load() -> None:
    """save_failure 写入失败原因；load_result 返回 failed 结构。"""
    from app.services import research_progress

    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=json.dumps(
        {"status": "failed", "error": "timeout"}, ensure_ascii=False
    ))

    with patch.object(research_progress, "_get_redis", new=AsyncMock(return_value=redis)):
        ok = await research_progress.save_failure("t3", "timeout")
        data = await research_progress.load_result("t3")

    assert ok is True
    assert data == {"status": "failed", "error": "timeout"}


@pytest.mark.asyncio
async def test_load_result_no_record_returns_none() -> None:
    """无结果记录（get 返回 None）时 load_result 返回 None。"""
    from app.services import research_progress

    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)

    with patch.object(research_progress, "_get_redis", new=AsyncMock(return_value=redis)):
        data = await research_progress.load_result("t5")

    assert data is None


@pytest.mark.asyncio
async def test_cleanup_stale_research_deletes_only_persistent_keys() -> None:
    """仅删除无 TTL 的快照/结果键，带 TTL 的跳过（防误删进行中任务）。"""
    import redis as redis_module

    from tasks import scheduled_tasks

    persistent = {"research_events:la", "research_result:rc"}

    def _ttl(key):
        return -1 if key in persistent else 123

    client = MagicMock()
    client.ttl = MagicMock(side_effect=_ttl)
    client.delete = MagicMock(return_value=1)
    client.close = MagicMock()

    def _scan(match, count):
        if match.startswith("research_events:"):
            return iter(["research_events:la", "research_events:lb"])
        return iter(["research_result:rc", "research_result:rd"])

    client.scan_iter = MagicMock(side_effect=_scan)

    fake_settings = MagicMock(REDIS_URL="redis://fake:6379/0")
    with patch("app.config.get_settings", return_value=fake_settings), patch.object(
        redis_module, "from_url", return_value=client
    ):
        result = await scheduled_tasks._cleanup_stale_research_async()

    assert result["snapshots_cleaned"] == 1  # la
    assert result["results_cleaned"] == 1    # rc
    client.delete.assert_any_call("research_events:la")
    client.delete.assert_any_call("research_result:rc")
    # 带 TTL 的键跳过
    for skipped in ("research_events:lb", "research_result:rd"):
        assert skipped not in {c.args[0] for c in client.delete.call_args_list}


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


def _make_user(uid=None):
    return SimpleNamespace(id=uid or uuid4(), role="editor", is_active=True)


def _db_returning(job):
    """构造返回指定 job 行的 mock db 会话（endpoint 直调用）。"""
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=job))
    )
    return db


def test_result_running_success_failed() -> None:
    """/result 三态（P0-1 DB 权威）：queued→running/自愈；
    success→读 output_json（不查 Redis）；failed→last_error。"""
    from app.api.v1.research import get_research_result

    jid, uid = uuid4(), uuid4()
    queued = SimpleNamespace(
        id=jid, user_id=uid, status="queued", output_json=None, last_error=None
    )

    with patch("app.services.research_progress.load_result") as lr:
        # queued 且 Redis 无终态 → running
        lr.return_value = None
        r = _run(get_research_result(str(jid), _make_user(uid), _db_returning(queued)))
        assert r.data["status"] == "running"

        # queued 且 Redis 有终态 → 自愈回填后返回成功
        lr.return_value = {"status": "success", "report": {"goal": "g", "summary": "s"}}
        with patch(
            "app.services.research_job_service.backfill_result_from_redis",
            new=AsyncMock(return_value=True),
        ) as bf:
            r = _run(get_research_result(str(jid), _make_user(uid), _db_returning(queued)))
            assert r.data["status"] == "success"
            assert r.data["report"]["summary"] == "s"
            bf.assert_awaited_once()

        # queued 且 Redis 记录失败 → failed
        lr.return_value = {"status": "failed", "error": "boom"}
        with patch(
            "app.services.research_job_service.backfill_result_from_redis",
            new=AsyncMock(return_value=True),
        ):
            r = _run(get_research_result(str(jid), _make_user(uid), _db_returning(queued)))
            assert r.data["status"] == "failed"
            assert r.data["error"] == "boom"

    # DB 已 success → 直读 output_json，Redis 不被查询（DB 权威）
    success_job = SimpleNamespace(
        id=jid, user_id=uid, status="success",
        output_json={"summary": "s"}, last_error=None,
    )
    with patch(
        "app.services.research_progress.load_result", new=AsyncMock(return_value=None)
    ) as lr:
        r = _run(get_research_result(str(jid), _make_user(uid), _db_returning(success_job)))
        assert r.data["status"] == "success"
        assert r.data["report"]["summary"] == "s"
        lr.assert_not_awaited()

    # DB 已 failed → last_error
    failed_job = SimpleNamespace(
        id=jid, user_id=uid, status="failed", output_json=None, last_error="boom"
    )
    r = _run(get_research_result(str(jid), _make_user(uid), _db_returning(failed_job)))
    assert r.data["status"] == "failed"
    assert r.data["error"] == "boom"


def test_stream_requires_auth(raw_client) -> None:
    """未认证访问 /stream 应 401。"""
    assert _run(raw_client.get("/api/v1/research/x/stream")).status_code == 401


def test_result_requires_auth(raw_client) -> None:
    """未认证访问 /result 应 401。"""
    assert _run(raw_client.get("/api/v1/research/x/result")).status_code == 401