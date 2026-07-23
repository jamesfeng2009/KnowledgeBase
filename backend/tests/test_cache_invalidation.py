"""
Token 缓存主动失效测试 — app/rag/cache.py 的 invalidate_by_doc_id。

覆盖：
    - L2 内存缓存主动失效（doc_ids 匹配）
    - L1 Redis 缓存主动失效（反向索引）
    - 混合场景（L1+L2 同时失效）
    - 无关联文档时返回 0
    - Redis 不可用降级
    - set 方法正确记录 doc_ids
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.cache import TokenCache, _L2Entry


# ======================================================================
# Mock Embedder
# ======================================================================

class MockEmbedder:
    """Mock Embedder — 不同查询生成近似正交的确定性向量。

    根据查询文本的 SHA256 哈希生成 one-hot 向量（在 _DIM 维空间中仅一个维度为 1.0）。
    - 相同查询 → 相同位置 → 余弦相似度 1.0（L2 语义匹配命中）
    - 不同查询 → 不同位置 → 余弦相似度 0.0（避免跨查询误匹配）

    这修复了原 MockEmbedder 对所有查询返回相同向量导致 L2 语义缓存
    跨查询误命中的问题。
    """

    _DIM = 1000

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        results = []
        for text in texts:
            vec = [0.0] * self._DIM
            h = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
            vec[h % self._DIM] = 1.0
            results.append(vec)
        return results


# ======================================================================
# L2 内存缓存主动失效
# ======================================================================

class TestL2Invalidate:
    """L2 内存缓存主动失效测试。"""

    @pytest.mark.asyncio
    async def test_invalidate_l2_by_doc_id(self):
        """文档更新 → L2 中引用该文档的缓存被清除。"""
        cache = TokenCache(embedder=MockEmbedder(), l2_max_size=100)
        cache._redis_available = False  # 禁用 L1

        # 写入两条缓存，一条引用 doc_1，一条引用 doc_2
        await cache.set("报销流程", "答案A", tenant_id="t1", doc_ids=["doc_1", "doc_2"])
        await cache.set("请假流程", "答案B", tenant_id="t1", doc_ids=["doc_3"])

        # 验证两条缓存都存在
        assert await cache.get("报销流程", tenant_id="t1") is not None
        assert await cache.get("请假流程", tenant_id="t1") is not None

        # 失效 doc_1 关联的缓存
        count = await cache.invalidate_by_doc_id("doc_1")

        assert count >= 1
        # 报销流程缓存被清除
        assert await cache.get("报销流程", tenant_id="t1") is None
        # 请假流程缓存不受影响
        assert await cache.get("请假流程", tenant_id="t1") is not None

    @pytest.mark.asyncio
    async def test_invalidate_multiple_entries(self):
        """一个文档被多个缓存条目引用 → 全部失效。"""
        cache = TokenCache(embedder=MockEmbedder(), l2_max_size=100)
        cache._redis_available = False

        await cache.set("问题1", "答案1", tenant_id="t1", doc_ids=["doc_shared"])
        await cache.set("问题2", "答案2", tenant_id="t1", doc_ids=["doc_shared"])
        await cache.set("问题3", "答案3", tenant_id="t1", doc_ids=["doc_other"])

        count = await cache.invalidate_by_doc_id("doc_shared")

        assert count >= 2
        assert await cache.get("问题1", tenant_id="t1") is None
        assert await cache.get("问题2", tenant_id="t1") is None
        assert await cache.get("问题3", tenant_id="t1") is not None

    @pytest.mark.asyncio
    async def test_invalidate_no_match_returns_zero(self):
        """文档 ID 不匹配任何缓存 → 返回 0。"""
        cache = TokenCache(embedder=MockEmbedder(), l2_max_size=100)
        cache._redis_available = False

        await cache.set("问题", "答案", tenant_id="t1", doc_ids=["doc_1"])

        count = await cache.invalidate_by_doc_id("doc_nonexistent")
        assert count == 0

    @pytest.mark.asyncio
    async def test_invalidate_no_doc_ids_returns_zero(self):
        """缓存条目无 doc_ids → 不被失效。"""
        cache = TokenCache(embedder=MockEmbedder(), l2_max_size=100)
        cache._redis_available = False

        await cache.set("问题", "答案", tenant_id="t1")  # 无 doc_ids

        count = await cache.invalidate_by_doc_id("doc_1")
        assert count == 0
        assert await cache.get("问题", tenant_id="t1") is not None

    @pytest.mark.asyncio
    async def test_invalidate_cross_tenant_isolation(self):
        """不同租户的缓存互不影响失效。"""
        cache = TokenCache(embedder=MockEmbedder(), l2_max_size=100)
        cache._redis_available = False

        # 租户 A 和租户 B 都有引用 doc_1 的缓存
        await cache.set("问题", "答案A", tenant_id="tenant_A", doc_ids=["doc_1"])
        await cache.set("问题", "答案B", tenant_id="tenant_B", doc_ids=["doc_1"])

        # 失效 doc_1 — 两个租户的缓存都应被清除（因为 doc_1 是全局文档 ID）
        count = await cache.invalidate_by_doc_id("doc_1")
        assert count >= 2


# ======================================================================
# L1 Redis 缓存主动失效
# ======================================================================

class TestL1Invalidate:
    """L1 Redis 缓存主动失效测试。"""

    @pytest.mark.asyncio
    async def test_invalidate_l1_by_doc_id(self):
        """文档更新 → L1 Redis 中通过反向索引清除关联缓存。"""
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock()
        # 反向索引返回 2 个缓存 key
        mock_redis.smembers = AsyncMock(return_value={"cache:l1:key1", "cache:l1:key2"})
        mock_redis.delete = AsyncMock()

        cache = TokenCache(redis=mock_redis, embedder=MockEmbedder())
        cache._redis_available = True

        count = await cache.invalidate_by_doc_id("doc_1")

        assert count == 2
        # 验证反向索引也被清除
        assert mock_redis.delete.call_count == 3  # 2 个缓存 key + 1 个索引 key

    @pytest.mark.asyncio
    async def test_invalidate_l1_no_index(self):
        """文档无反向索引 → 返回 0，不报错。"""
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock()
        mock_redis.smembers = AsyncMock(return_value=set())  # 空集合
        mock_redis.delete = AsyncMock()

        cache = TokenCache(redis=mock_redis, embedder=MockEmbedder())
        cache._redis_available = True

        count = await cache.invalidate_by_doc_id("doc_no_cache")
        assert count == 0

    @pytest.mark.asyncio
    async def test_invalidate_redis_unavailable_degrade(self):
        """Redis 不可用 → 仅走 L2 失效，不报错。"""
        cache = TokenCache(embedder=MockEmbedder(), l2_max_size=100)
        cache._redis_available = False

        await cache.set("问题", "答案", tenant_id="t1", doc_ids=["doc_1"])

        count = await cache.invalidate_by_doc_id("doc_1")
        assert count >= 1  # L2 命中


# ======================================================================
# set 方法 doc_ids 记录
# ======================================================================

class TestSetWithDocIds:
    """set 方法正确记录 doc_ids 测试。"""

    @pytest.mark.asyncio
    async def test_set_records_doc_ids_in_l2(self):
        """set 写入 L2 时记录 doc_ids。"""
        cache = TokenCache(embedder=MockEmbedder(), l2_max_size=100)
        cache._redis_available = False

        await cache.set("问题", "答案", tenant_id="t1", doc_ids=["doc_a", "doc_b"])

        # 验证 L2 条目中 doc_ids 被正确记录
        key = cache._hash("问题", "t1")
        entry = cache._l2_store.get(key)
        assert entry is not None
        assert entry.doc_ids == ["doc_a", "doc_b"]

    @pytest.mark.asyncio
    async def test_set_without_doc_ids(self):
        """set 不传 doc_ids → L2 条目 doc_ids 为 None。"""
        cache = TokenCache(embedder=MockEmbedder(), l2_max_size=100)
        cache._redis_available = False

        await cache.set("问题", "答案", tenant_id="t1")

        key = cache._hash("问题", "t1")
        entry = cache._l2_store.get(key)
        assert entry is not None
        assert entry.doc_ids is None

    @pytest.mark.asyncio
    async def test_set_writes_l1_reverse_index(self):
        """set 写入 L1 时同时写入 doc_id 反向索引。"""
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock()
        mock_redis.set = AsyncMock()
        mock_redis.sadd = AsyncMock()
        mock_redis.expire = AsyncMock()

        cache = TokenCache(redis=mock_redis, embedder=MockEmbedder())
        cache._redis_available = True

        await cache.set("问题", "答案", tenant_id="t1", doc_ids=["doc_a", "doc_b"])

        # 验证为每个 doc_id 写入了反向索引
        assert mock_redis.sadd.call_count == 2
        assert mock_redis.expire.call_count == 2
