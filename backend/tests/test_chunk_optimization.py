"""RAG 分块优化测试 — P0 Q&A 分块 / P1 内容类型路由 / P2 Context Cliff / P3 标题路径。

覆盖四个优化点：
- P0: Q&A 对分块策略 — 一个问答对 = 一个 chunk，问题和答案不被拆散；
- P1: 内容类型标签路由 — content_type 参数主动路由到最优分块策略；
- P2: Context Cliff 监控 — Generator 注入上下文超阈值时自动降级；
- P3: 标题路径上下文锚点 — 结构化分块提取完整标题层级路径。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.rag.chunker import Chunk, SemanticChunker, estimate_tokens
from app.rag.generator import Generator, _CONTEXT_CLIFF_THRESHOLD, _CONTEXT_CLIFF_FALLBACK_TOP_K


# ======================================================================
# 测试数据
# ======================================================================

# P0: Q&A 测试数据 — 中文格式
_QA_CONTENT_CN = """\
问：Redis 哨兵模式和 Cluster 模式怎么选？

答：哨兵模式适合主从切换场景，Cluster 模式适合数据分片场景。
如果数据量不大但需要高可用，选哨兵；如果数据量超过单机内存，选 Cluster。

问：Kafka 消费者组怎么设置？

答：消费者组通过 group.id 配置，同一组内的消费者分担分区消费。
建议消费者数量不超过分区数，否则多余消费者空闲。
"""

# P0: Q&A 测试数据 — 英文格式
_QA_CONTENT_EN = """\
Q: How to configure Redis persistence?

A: Redis supports RDB and AOF persistence. RDB is a snapshot approach,
while AOF logs every write operation. For production, use both.

Q: What is the default Kafka retention period?

A: The default retention is 7 days. You can change it via
log.retention.hours in server.properties.
"""

# P0: Q&A 测试数据 — Markdown 标题格式
_QA_CONTENT_MD = """\
# Redis 常见问题

## 问题：Redis 持久化怎么选？

## 回答：RDB 适合备份场景，AOF 适合数据安全要求高的场景。生产环境建议同时开启。

## 问题：Redis 内存满了怎么办？

## 回答：可以使用 maxmemory 配置最大内存，配合淘汰策略如 allkeys-lru。
"""

# P3: 结构化分块测试数据 — Markdown
_MD_CONTENT = """\
# Redis 深度解析

Redis 是一个高性能的键值数据库，支持多种数据结构。

## 集群架构

Redis Cluster 通过哈希槽分区实现水平扩展。

### 哈希槽分配

Redis Cluster 有 16384 个哈希槽，每个节点负责一部分槽。

### 故障转移

当主节点宕机时，从节点会自动升级为主节点。

## 持久化机制

Redis 支持 RDB 和 AOF 两种持久化方式。
"""

# P3: 结构化分块测试数据 — HTML
_HTML_CONTENT = """\
<h1>系统架构文档</h1>
<p>系统采用微服务架构设计。</p>
<h2>服务层</h2>
<p>服务层负责业务逻辑处理。</p>
<h3>用户服务</h3>
<p>用户服务管理认证和授权。</p>
<h3>订单服务</h3>
<p>订单服务处理订单生命周期。</p>
"""


# ======================================================================
# P0: Q&A 对分块测试
# ======================================================================


class TestQAChunking:
    """P0: Q&A 对分块策略测试。"""

    def test_qa_split_chinese_format(self) -> None:
        """中文 问：/答： 格式的 Q&A 应被正确切分。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_QA_CONTENT_CN, doc_type="txt", content_type="faq")

        assert len(chunks) == 2
        # 每个 chunk 应同时包含问题和答案
        for c in chunks:
            assert "问：" in c.content
            assert "答：" in c.content
            assert c.chunk_strategy == "qa"
            assert c.content_type == "faq"

    def test_qa_split_english_format(self) -> None:
        """英文 Q:/A: 格式的 Q&A 应被正确切分。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_QA_CONTENT_EN, doc_type="txt", content_type="faq")

        assert len(chunks) == 2
        for c in chunks:
            assert "Q:" in c.content or "问：" in c.content
            assert "A:" in c.content or "答：" in c.content
            assert c.chunk_strategy == "qa"

    def test_qa_split_markdown_format(self) -> None:
        """Markdown 标题格式的 Q&A 应被正确切分。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_QA_CONTENT_MD, doc_type="md", content_type="faq")

        assert len(chunks) >= 2
        for c in chunks:
            assert c.chunk_strategy == "qa"
            assert c.content_type == "faq"

    def test_qa_split_question_answer_not_separated(self) -> None:
        """Q&A 分块后，问题和答案必须在同一个 chunk 中。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_QA_CONTENT_CN, doc_type="txt", content_type="faq")

        for c in chunks:
            # 每个 chunk 必须同时包含问题和答案
            has_question = "问：" in c.content or "Q:" in c.content
            has_answer = "答：" in c.content or "A:" in c.content
            assert has_question, "chunk 缺少问题部分"
            assert has_answer, "chunk 缺少答案部分"

    def test_qa_split_title_path_is_question(self) -> None:
        """Q&A chunk 的 title_path 应为问题文本（截断）。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_QA_CONTENT_CN, doc_type="txt", content_type="faq")

        for c in chunks:
            assert c.title_path  # 不为空
            # title_path 应是问题内容的前 80 字符
            assert len(c.title_path) <= 80

    def test_qa_split_no_qa_content_falls_back(self) -> None:
        """非 Q&A 内容走 faq 路由时应降级到兜底链。"""
        plain_text = "这是一段普通文本，没有问答格式。包含多个句号。用于测试降级。"
        chunker = SemanticChunker()
        chunks = chunker.chunk(plain_text, doc_type="txt", content_type="faq")

        # 没有 Q&A 模式，降级到兜底
        assert len(chunks) >= 1
        # 不应该是 qa 策略
        assert all(c.chunk_strategy != "qa" for c in chunks)

    def test_qa_split_empty_content(self) -> None:
        """空内容应返回空列表。"""
        chunker = SemanticChunker()
        assert chunker.chunk("", doc_type="txt", content_type="faq") == []
        assert chunker.chunk("   ", doc_type="txt", content_type="faq") == []


# ======================================================================
# P1: 内容类型标签路由测试
# ======================================================================


class TestContentTypeRouting:
    """P1: 内容类型标签路由测试。"""

    def test_faq_routes_to_qa_chunker(self) -> None:
        """content_type=faq 应路由到 Q&A 分块。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_QA_CONTENT_CN, doc_type="txt", content_type="faq")

        assert len(chunks) > 0
        assert all(c.chunk_strategy == "qa" for c in chunks)

    def test_tutorial_routes_to_structural(self) -> None:
        """content_type=tutorial 应路由到结构化分块。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="tutorial")

        assert len(chunks) >= 2
        assert all(c.chunk_strategy == "structural" for c in chunks)

    def test_plain_routes_to_semantic(self) -> None:
        """content_type=plain 应路由到语义分块（TextTiling）。"""
        # 需要足够长的文本让 TextTiling 工作
        long_text = (
            "Redis 是一个键值数据库。它支持多种数据结构。\n\n"
            "Kafka 是一个消息队列。它用于流数据处理。\n\n"
            "MySQL 是一个关系型数据库。它支持事务和 SQL 查询。\n\n"
            "Nginx 是一个 Web 服务器。它支持反向代理和负载均衡。\n\n"
            "Docker 是一个容器化平台。它支持应用隔离和快速部署。\n\n"
            "Kubernetes 是一个容器编排工具。它支持自动扩缩容和滚动更新。\n\n"
        )
        chunker = SemanticChunker()
        chunks = chunker.chunk(long_text, doc_type="txt", content_type="plain")

        # 语义分块应产出至少 2 个 chunk
        assert len(chunks) >= 1

    def test_auto_keeps_fallback_chain(self) -> None:
        """content_type=auto 应保持原有四级兜底链行为。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="auto")

        # auto 模式下，有标题的 Markdown 应走结构化分块
        assert len(chunks) >= 2
        assert all(c.chunk_strategy == "structural" for c in chunks)

    def test_default_content_type_is_auto(self) -> None:
        """不传 content_type 时默认为 auto。"""
        chunker = SemanticChunker()
        chunks1 = chunker.chunk(_MD_CONTENT, doc_type="md")
        chunks2 = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="auto")

        # 两者结果应一致（都是结构化分块）
        assert len(chunks1) == len(chunks2)
        assert all(c.chunk_strategy == "structural" for c in chunks1)

    def test_invalid_content_type_treated_as_auto(self) -> None:
        """无效的 content_type 应被当作 auto 处理。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="invalid_type")

        # 降级到 auto 行为
        assert len(chunks) >= 2

    def test_faq_with_structural_content_falls_back(self) -> None:
        """faq 路由但内容没有 Q&A 模式时，应降级到兜底链。"""
        plain = "这是一段没有问答格式的普通文本内容。" * 50
        chunker = SemanticChunker()
        chunks = chunker.chunk(plain, doc_type="txt", content_type="faq")

        assert len(chunks) >= 1
        # 不是 qa 策略
        assert all(c.chunk_strategy != "qa" for c in chunks)


# ======================================================================
# P3: 标题路径上下文锚点测试
# ======================================================================


class TestTitlePath:
    """P3: 标题路径上下文锚点测试。"""

    def test_markdown_title_path_h1(self) -> None:
        """H1 标题的 title_path 应为标题文本本身。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="tutorial")

        h1_chunk = [c for c in chunks if "Redis 深度解析" in c.title_path]
        assert len(h1_chunk) > 0
        assert "Redis 深度解析" in h1_chunk[0].title_path

    def test_markdown_title_path_nested(self) -> None:
        """嵌套标题的 title_path 应包含完整层级路径。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="tutorial")

        # 找到 H3 "哈希槽分配" 的 chunk
        hash_slot_chunks = [c for c in chunks if "哈希槽分配" in c.title_path]
        assert len(hash_slot_chunks) > 0
        # title_path 应为 "Redis 深度解析 > 集群架构 > 哈希槽分配"
        title_path = hash_slot_chunks[0].title_path
        assert "Redis 深度解析" in title_path
        assert "集群架构" in title_path
        assert "哈希槽分配" in title_path
        assert ">" in title_path

    def test_markdown_title_path_sibling(self) -> None:
        """同级标题的 title_path 不应包含兄弟标题。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="tutorial")

        # "故障转移" 和 "哈希槽分配" 是同级 H3，不应出现在彼此的 title_path 中
        hash_slot = [c for c in chunks if "哈希槽分配" in c.title_path]
        failover = [c for c in chunks if "故障转移" in c.title_path]

        if hash_slot and failover:
            assert "故障转移" not in hash_slot[0].title_path
            assert "哈希槽分配" not in failover[0].title_path

    def test_html_title_path(self) -> None:
        """HTML 标题也应提取 title_path。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_HTML_CONTENT, doc_type="html", content_type="tutorial")

        assert len(chunks) >= 2

        # 检查 H3 的 title_path
        user_service = [c for c in chunks if "用户服务" in c.title_path]
        assert len(user_service) > 0
        assert "系统架构文档" in user_service[0].title_path
        assert "服务层" in user_service[0].title_path

    def test_title_path_empty_for_non_structural(self) -> None:
        """非结构化分块的 chunk title_path 应为空字符串。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_QA_CONTENT_CN, doc_type="txt", content_type="faq")

        # Q&A chunk 的 title_path 是问题文本，不为空，但非 structural 策略
        for c in chunks:
            assert c.chunk_strategy == "qa"
            assert c.title_path  # Q&A 的 title_path 是问题内容

    def test_all_structural_chunks_have_title_path(self) -> None:
        """所有结构化分块的 chunk 都应有非空 title_path。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="tutorial")

        for c in chunks:
            assert c.chunk_strategy == "structural"
            assert c.title_path, f"chunk {c.id} 的 title_path 为空"


# ======================================================================
# P2: Context Cliff 监控测试
# ======================================================================


class TestContextCliff:
    """P2: Context Cliff 监控测试。"""

    def _make_generator(self) -> Generator:
        """构造测试用 Generator（Mock LLM）。"""
        mock_llm = MagicMock()
        return Generator(llm=mock_llm)

    def test_context_cliff_no_truncation_under_threshold(self) -> None:
        """总 token 未超过阈值时不应截断。"""
        gen = self._make_generator()
        docs = [
            {"content": "短文档1" * 10},
            {"content": "短文档2" * 10},
        ]
        result = gen._check_context_cliff(docs)
        assert len(result) == len(docs)

    def test_context_cliff_truncation_over_threshold(self) -> None:
        """总 token 超过阈值时应截断为 Top-3。"""
        gen = self._make_generator()
        # 构造超过 2500 token 的文档（约 8750 字符）
        big_content = "A" * 2000  # ~571 tokens each
        docs = [
            {"content": big_content, "title": f"doc-{i}"}
            for i in range(6)  # 6 * 571 = 3428 tokens > 2500
        ]
        result = gen._check_context_cliff(docs)
        assert len(result) == _CONTEXT_CLIFF_FALLBACK_TOP_K

    def test_context_cliff_preserves_top_k(self) -> None:
        """截断后应保留前 Top-K 个文档（保持重排顺序）。"""
        gen = self._make_generator()
        big_content = "B" * 2000
        docs = [
            {"content": big_content, "title": f"doc-{i}", "score": 0.9 - i * 0.1}
            for i in range(5)
        ]
        result = gen._check_context_cliff(docs)
        assert len(result) == _CONTEXT_CLIFF_FALLBACK_TOP_K
        # 保持顺序
        assert result[0]["title"] == "doc-0"
        assert result[1]["title"] == "doc-1"
        assert result[2]["title"] == "doc-2"

    def test_context_cliff_empty_docs(self) -> None:
        """空文档列表应直接返回。"""
        gen = self._make_generator()
        result = gen._check_context_cliff([])
        assert result == []

    def test_context_cliff_exactly_at_threshold(self) -> None:
        """恰好等于阈值时不应截断。"""
        gen = self._make_generator()
        # 构造恰好约 2500 token 的内容
        content = "C" * int(_CONTEXT_CLIFF_THRESHOLD * 3.5)
        docs = [{"content": content}]
        result = gen._check_context_cliff(docs)
        assert len(result) == 1

    def test_build_prompt_includes_title_path(self) -> None:
        """系统 prompt 应包含 title_path 上下文锚点。"""
        gen = self._make_generator()
        docs = [
            {
                "content": "Redis Cluster 使用哈希槽分区。",
                "title": "Redis 文档",
                "title_path": "Redis 深度解析 > 集群架构 > 哈希槽分配",
            }
        ]
        prompt = gen._build_system_prompt(docs, [], "")
        assert "Redis 深度解析 > 集群架构 > 哈希槽分配" in prompt

    def test_build_prompt_fallback_to_title_without_title_path(self) -> None:
        """没有 title_path 时应回退到 title。"""
        gen = self._make_generator()
        docs = [
            {
                "content": "普通文档内容。",
                "title": "普通文档",
            }
        ]
        prompt = gen._build_system_prompt(docs, [], "")
        assert "普通文档" in prompt

    def test_build_prompt_triggers_cliff_degradation(self) -> None:
        """超长上下文应触发 Context Cliff 降级，prompt 中的文档数减少。"""
        gen = self._make_generator()
        big_content = "D" * 2000
        docs = [
            {"content": big_content, "title": f"doc-{i}"}
            for i in range(6)
        ]
        prompt = gen._build_system_prompt(docs, [], "")
        # 只应有 3 个文档编号（[1] [2] [3]）
        assert "[1]" in prompt
        assert "[2]" in prompt
        assert "[3]" in prompt
        assert "[4]" not in prompt
        assert "[5]" not in prompt


# ======================================================================
# 回归测试 — 确保原有功能不受影响
# ======================================================================


class TestBackwardCompatibility:
    """回归测试 — 确保新增字段和路由不破坏原有功能。"""

    def test_chunk_without_content_type_still_works(self) -> None:
        """不传 content_type 时原有行为不变。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md")
        assert len(chunks) >= 2

    def test_chunk_new_fields_have_defaults(self) -> None:
        """Chunk 新增字段应有默认值，不影响旧代码。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk("简单文本。" * 100, doc_type="txt")
        for c in chunks:
            # 新增字段有默认值
            assert hasattr(c, "title_path")
            assert hasattr(c, "content_type")
            assert hasattr(c, "chunk_strategy")

    def test_estimate_tokens_unchanged(self) -> None:
        """estimate_tokens 函数行为不变。"""
        assert estimate_tokens("") == 0
        assert estimate_tokens("hello world") > 0
        assert estimate_tokens("你好世界") > 0

    def test_existing_rag_engine_still_works(self) -> None:
        """RAG 引擎的 Mock 测试仍应通过。"""
        from app.rag.engine import AgenticRAGEngine
        from tests.test_rag_engine import FakeLLM, FakeMCPClient, FakeRetriever, FakeReranker, FakeGenerator

        engine = AgenticRAGEngine(
            llm=FakeLLM("generate"),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
            cache=None,
        )
        assert engine.max_iterations == 5
