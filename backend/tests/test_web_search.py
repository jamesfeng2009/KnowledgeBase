"""
P4 Tavily 提供商 / 限速 / 配额测试。

覆盖：
    - TokenBucket：burst 瞬发放行、rate=0 不限速、rate>0 超发被节流
    - DailyQuota：内存降级路径的计数与超限拒绝
    - TavilyProvider：真实 HTTP 结构解析（MockTransport 注入）、失败降级为空
    - build_provider：Tavily+Key → TavilyProvider；缺 Key / mock → MockProvider
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import httpx
import pytest

from app.rag.web_search import (
    DailyQuota,
    MockProvider,
    TavilyProvider,
    TokenBucket,
    build_provider,
    dedup_web_by_url,
)


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_burst_allows_immediate(self) -> None:
        """burst=3 时连续 3 次 acquire 立即放行（不经等待）。"""
        bucket = TokenBucket(rate_per_second=0.01, burst=3)
        t0 = time.monotonic()
        for _ in range(3):
            await asyncio.wait_for(bucket.acquire(), timeout=0.5)
        assert time.monotonic() - t0 < 0.4

    @pytest.mark.asyncio
    async def test_rate_zero_is_unlimited(self) -> None:
        """rate=0 表示不限速，多次 acquire 即取即回。"""
        bucket = TokenBucket(rate_per_second=0.0, burst=1)
        for _ in range(10):
            await asyncio.wait_for(bucket.acquire(), timeout=0.2)

    @pytest.mark.asyncio
    async def test_excess_burst_is_throttled(self) -> None:
        """burst 耗尽后再 acquire 被节流等待回填（rate=50 → 第 4 次须等待）。"""
        bucket = TokenBucket(rate_per_second=50.0, burst=2)
        for _ in range(2):
            await asyncio.wait_for(bucket.acquire(), timeout=0.5)
        t0 = time.monotonic()
        await asyncio.wait_for(bucket.acquire(), timeout=1.0)
        assert time.monotonic() - t0 >= 0.005  # 出现等待即视为被节流


class TestDailyQuotaMemory:
    @pytest.mark.asyncio
    async def test_count_and_exceed(self) -> None:
        """本地路径（use_redis=False 规避环境 Redis 的状态污染）：limit=3 前 3 放行第 4 拒绝。"""
        q = DailyQuota(3, per_tenant=False, use_redis=False)
        results = [await q.consume() for _ in range(4)]
        assert results == [True, True, True, False]

    @pytest.mark.asyncio
    async def test_unlimited(self) -> None:
        """limit=0 表示不限量。"""
        q = DailyQuota(0, use_redis=False)
        assert await q.consume() is True
        assert await q.consume() is True

    @pytest.mark.asyncio
    async def test_per_tenant_isolation(self) -> None:
        """按 scope（tenant）隔离配额：租户 A 的超限不影响租户 B。"""
        q = DailyQuota(2, use_redis=False)
        assert [await q.consume("tenant:a") for _ in range(2)] == [True, True]
        assert await q.consume("tenant:a") is False   # A 已到顶
        assert await q.consume("tenant:b") is True    # B 独立计数，仍放行


class TestTavilyProvider:
    @pytest.mark.asyncio
    async def test_parses_results(self) -> None:
        """MockTransport 注入：正确解析 Tavily results 并映射为 WebHit 结构。"""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer test-key"
            return httpx.Response(200, json={
                "results": [
                    {"title": "甲", "url": "https://a.com/x", "content": "快照A",
                     "score": 0.9},
                    {"title": "乙", "url": "https://b.com/y", "content": "快照B",
                     "score": 0.7},
                    {"title": "no-url", "content": "无 url 应跳过", "score": 0.5},
                ]
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = TavilyProvider("test-key", client=client)
        hits = await provider.search("某查询", max_results=5)
        await client.aclose()

        assert len(hits) == 2
        first = hits[0]
        assert first["url"] == "https://a.com/x"
        assert first["snippet"] == "快照A"
        assert first["score"] == 0.9

    @pytest.mark.asyncio
    async def test_http_failure_degrades_empty(self) -> None:
        """HTTP 5xx → 返回空列表而非抛错（降级不阻塞）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = TavilyProvider("test-key", client=client)
        hits = await provider.search("某查询")
        await client.aclose()
        assert hits == []

    def test_requires_api_key(self) -> None:
        with pytest.raises(ValueError):
            TavilyProvider("")  # noqa: E1120 空 Key 拒绝构建


class TestBuildProvider:
    def test_tavily_with_key_builds_real(self) -> None:
        p = build_provider("tavily", api_key="k")
        assert isinstance(p, TavilyProvider)

    def test_tavily_without_key_falls_back_mock(self) -> None:
        p = build_provider("tavily")
        assert isinstance(p, MockProvider)

    def test_mock_default(self) -> None:
        assert isinstance(build_provider("mock"), MockProvider)
        assert isinstance(build_provider(""), MockProvider)


class TestDedup:
    def test_url_dedup_only(self) -> None:
        hits = [
            {"title": "a", "url": "https://x.com/1"},
            {"title": "a2", "url": "https://x.com/1"},   # 同 URL 去重
            {"title": "b", "url": "https://x.com/2"},
        ]
        out = dedup_web_by_url(hits)
        assert len(out) == 2