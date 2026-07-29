"""Generator 错误处理回归测试。

Bug 背景：LLM 抛错时，原实现将错误文本 ``"\\n\\n[生成出错: ...]"`` 当作正常
答案 token yield — 上游 ``AgenticRAGEngine.answer`` 把它拼接进 answer 并
``cache.set`` 回写缓存（同时被 chat_service 持久化为 assistant 消息），
导致错误答案被缓存复用、持续污染后续相同查询。

修复：错误不再作为答案产出 — 记录 ``generator.error`` 日志后原样抛出，
由上层决定降级策略；异常路径跳过上层的 cache.set，错误不会写入缓存。

覆盖：
- LLM 直接抛错 → 不产生任何答案 token，异常原样抛出，无"[生成出错"文本；
- 先产出部分答案后抛错 → 已产出 token 保留，错误不追加为答案；
- 正常流不受影响（行为保持）；
- 端到端（engine 级）：LLM 故障时 cache.set 不被调用；成功时正常回写。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.rag.generator import Generator


# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------


class _FailingLLM:
    """chat() 立即抛错的 LLM。"""

    async def chat(self, messages, tools=None, stream=False, **kwargs) -> AsyncIterator[str]:
        raise RuntimeError("LLM 服务不可用")
        yield  # pragma: no cover — 使本方法成为 async generator


class _PartialFailingLLM:
    """先产出部分答案、随后连接中断的 LLM。"""

    async def chat(self, messages, tools=None, stream=False, **kwargs) -> AsyncIterator[str]:
        yield "前半部分"
        raise RuntimeError("连接中断")


class _OKLLM:
    """正常产出答案的 LLM（同时满足 engine._think 的决策调用）。"""

    def __init__(self, think_response: str = "generate"):
        self._think_response = think_response

    async def chat(self, messages, tools=None, stream=False, **kwargs) -> AsyncIterator[str]:
        if not stream:
            yield self._think_response
            return
        yield "正常"
        yield "答案"


# ---------------------------------------------------------------------------
# Generator 单元测试
# ---------------------------------------------------------------------------


class TestGeneratorErrorNotYieldedAsAnswer:
    """LLM 错误不得作为答案产出。"""

    def test_error_not_yielded_as_answer(self):
        gen = Generator(llm=_FailingLLM())

        async def consume():
            tokens = []
            with pytest.raises(RuntimeError, match="LLM 服务不可用"):
                async for token in gen.generate("问题", [], []):
                    tokens.append(token)
            return tokens

        tokens = asyncio.run(consume())
        # 错误文本不得作为答案产出
        assert tokens == []

    def test_partial_answer_then_raise(self):
        """已产出的真实 token 保留；错误不追加为答案 token。"""
        gen = Generator(llm=_PartialFailingLLM())

        async def consume():
            tokens = []
            with pytest.raises(RuntimeError, match="连接中断"):
                async for token in gen.generate("问题", [], []):
                    tokens.append(token)
            return tokens

        tokens = asyncio.run(consume())
        assert tokens == ["前半部分"]
        assert all("生成出错" not in t for t in tokens)

    def test_normal_stream_unchanged(self):
        """正常流式生成不受影响（行为保持）。"""
        gen = Generator(llm=_OKLLM())

        async def consume():
            return [t async for t in gen.generate("问题", [], [])]

        assert asyncio.run(consume()) == ["正常", "答案"]


# ---------------------------------------------------------------------------
# Engine 级端到端：错误不写入缓存
# ---------------------------------------------------------------------------


class _FakeRetriever:
    async def retrieve(self, query: str) -> list:
        return []


class _FakeReranker:
    async def rerank(self, query: str, documents: list) -> list:
        return documents


class _FakeMCPClient:
    pass


class _FakeCache:
    """记录 set 调用、永远未命中 的缓存。"""

    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str]] = []

    async def get(self, query: str, **kwargs):
        return None

    async def set(self, query: str, answer: str, **kwargs):
        self.set_calls.append((query, answer))


class _NullRewriteResult:
    """空重写结果 — get_search_query() 返回原查询（等于不重写）。"""

    strategy: list = []
    latency_ms: float = 0.0

    def __init__(self, query: str) -> None:
        self._query = query

    def get_search_query(self) -> str:
        return self._query


class _NullQueryRewriter:
    """空查询重写器 — 避免测试触发真实 LLM 重写调用。"""

    async def rewrite(self, query: str, context: str = "") -> _NullRewriteResult:
        return _NullRewriteResult(query)


class _NullQualityGuard:
    """空质量守卫 — 生成质量评估返回 None，使 engine 降级到内联 reflect
    （内联 reflect 自身捕获异常，不触发外部 LLM 调用）。"""

    def check_retrieval_quality(self, reranked_docs: list) -> None:
        return None

    def should_retry_retrieval(self, query: str, reranked_docs: list, retry_count: int) -> bool:
        return False

    def get_expanded_top_k(self) -> int:
        return 5

    async def check_generation_quality(self, query: str, answer: str, contexts: list) -> None:
        return None

    async def check_and_regenerate(self, query: str, answer: str, contexts: list, generator) -> tuple:
        """空实现 — 不重生成，eval_result 为 None（与幻觉防护流水线接口对齐）。"""
        return answer, None

    def is_low_confidence(self, eval_result) -> bool:
        return False


def _make_engine(llm, cache) -> "object":
    """构造最小可用的 AgenticRAGEngine（空质量守卫，避免外部依赖）。"""
    from app.rag import engine as engine_mod

    return engine_mod.AgenticRAGEngine(
        llm=llm,
        mcp_client=_FakeMCPClient(),
        retriever=_FakeRetriever(),
        reranker=_FakeReranker(),
        generator=Generator(llm=llm),
        cache=cache,
        quality_guard=_NullQualityGuard(),
        query_rewriter=_NullQueryRewriter(),
    )


class TestErrorNotWrittenToCache:
    """端到端：LLM 故障 → 错误不写入缓存；成功 → 正常回写（行为保持）。"""

    def test_llm_failure_skips_cache_set(self):
        cache = _FakeCache()
        engine = _make_engine(_FailingLLM(), cache)

        async def consume():
            with pytest.raises(RuntimeError, match="LLM 服务不可用"):
                async for _ in engine.answer(
                    query="故障测试", user_id="u1", session_id="s1"
                ):
                    pass

        asyncio.run(consume())

        # 错误路径不得回写缓存（修复前："[生成出错: ...]" 被缓存为正确答案）
        assert cache.set_calls == []

    def test_success_still_writes_cache(self):
        cache = _FakeCache()
        engine = _make_engine(_OKLLM(), cache)

        async def consume():
            chunks = []
            async for chunk in engine.answer(
                query="正常测试", user_id="u1", session_id="s1"
            ):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(consume())

        # 答案正常产出
        assert "正常" in chunks and "答案" in chunks
        # 成功路径缓存正常回写，且内容不含错误文本
        assert len(cache.set_calls) == 1
        query, answer = cache.set_calls[0]
        assert query == "正常测试"
        assert "正常答案" in answer
        assert "生成出错" not in answer
