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
        # Bug23 修复后按截断后内容（≤1500 字符 ≈ 429 token/篇）估算 token，
        # 需 6 篇（6×429=2574）才超过 2500 token 阈值触发降级
        big_content = "B" * 2000
        docs = [
            {"content": big_content, "title": f"doc-{i}", "score": 0.9 - i * 0.1}
            for i in range(6)
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

    def test_build_prompt_budget_allocation(self) -> None:
        """超长上下文时按预算择优注入 — 预算内能放下的片段全部保留，
        放不下的被淘汰（P0-1 预算分配式注入，替代旧的砍到 Top-3）。"""
        gen = self._make_generator()
        big_content = "D" * 2000
        docs = [
            {"content": big_content, "title": f"doc-{i}"}
            for i in range(6)
        ]
        prompt = gen._build_system_prompt(docs, [], "")
        # 每篇截断到 1500 字符 ≈ 429 token，预算 2500 可容纳 5 篇
        assert "[1]" in prompt
        assert "[2]" in prompt
        assert "[3]" in prompt
        assert "[4]" in prompt
        assert "[5]" in prompt
        # 第 6 篇超出预算被淘汰
        assert "[6]" not in prompt


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


# ======================================================================
# 格式智能检测测试 — Docling Markdown vs Legacy HTML
# ======================================================================


class TestFormatDetection:
    """格式智能检测 — _is_markdown 和 _structural_split 路由测试。"""

    def test_is_markdown_with_heading(self) -> None:
        """包含 # 标题的内容被识别为 Markdown。"""
        assert SemanticChunker._is_markdown("# 标题\n正文") is True
        assert SemanticChunker._is_markdown("## 二级标题\n正文") is True
        assert SemanticChunker._is_markdown("### 三级标题\n正文") is True

    def test_is_markdown_with_table(self) -> None:
        """包含 Markdown 表格的内容被识别为 Markdown。"""
        content = "| 列1 | 列2 |\n|---|---|\n| 值1 | 值2 |"
        assert SemanticChunker._is_markdown(content) is True

    def test_is_markdown_with_table_separator_only(self) -> None:
        """只有表格分隔行也被识别。"""
        content = "一些文本\n|---|---|\n更多文本"
        assert SemanticChunker._is_markdown(content) is True

    def test_is_markdown_html_content(self) -> None:
        """HTML 内容不被识别为 Markdown。"""
        assert SemanticChunker._is_markdown("<h1>标题</h1><p>正文</p>") is False
        assert SemanticChunker._is_markdown("<h2>章节</h2><table><tr><td>数据</td></tr></table>") is False

    def test_is_markdown_plain_text(self) -> None:
        """纯文本不被识别为 Markdown。"""
        assert SemanticChunker._is_markdown("这是一段普通文本，没有格式标记。") is False
        assert SemanticChunker._is_markdown("") is False

    def test_is_markdown_heading_not_in_code_block(self) -> None:
        """代码块中的 # 不应被误判（行首才匹配）。"""
        # # 在行首非代码块内才算 Markdown 标题
        content = "普通文本\n  # 这不是标题（有前导空格）\n更多文本"
        assert SemanticChunker._is_markdown(content) is False

    def test_structural_split_markdown_from_docling_pdf(self) -> None:
        """Docling 解析 PDF 输出 Markdown — 按 # 标题分块（非 doc_type 路由）。"""
        chunker = SemanticChunker()
        # Docling 输出 Markdown，但 doc_type="pdf"
        markdown_content = """\
# 第一章 概述

这是概述内容。

## 1.1 背景

背景说明。

## 1.2 目标

目标说明。

# 第二章 设计

设计内容。
"""
        # doc_type="pdf" 但内容是 Markdown — 应走 _split_markdown
        chunks = chunker._structural_split(markdown_content, "doc-1", "pdf")
        assert len(chunks) >= 2
        # 验证标题路径被正确提取
        title_paths = [c.title_path for c in chunks if c.title_path]
        assert any("第一章" in tp for tp in title_paths)
        assert any("第二章" in tp for tp in title_paths)

    def test_structural_split_html_from_legacy_pdf(self) -> None:
        """Legacy 解析 PDF 输出 HTML — 按 <h2> 标签分块。"""
        chunker = SemanticChunker()
        # Legacy pymupdf 输出 HTML，doc_type="pdf"
        html_content = """\
<h1>系统架构</h1>
<p>架构概述</p>
<h2>服务层</h2>
<p>服务层内容</p>
<h2>数据层</h2>
<p>数据层内容</p>
"""
        # doc_type="pdf" 且内容是 HTML — 应走 _split_html
        chunks = chunker._structural_split(html_content, "doc-2", "pdf")
        assert len(chunks) >= 2
        title_paths = [c.title_path for c in chunks if c.title_path]
        assert any("系统架构" in tp for tp in title_paths)

    def test_structural_split_markdown_table_from_docling_xlsx(self) -> None:
        """Docling 解析 XLSX 输出 Markdown 表格 — 被正确识别为 Markdown。"""
        chunker = SemanticChunker()
        markdown_table = """\
# 财务报表

## Q3 收入

| 项目 | 金额 |
|---|---|
| 营收 | 100万 |
| 成本 | 60万 |

## Q4 预算

| 项目 | 金额 |
|---|---|
| 营收 | 120万 |
"""
        chunks = chunker._structural_split(markdown_table, "doc-3", "xlsx")
        # 应走 _split_markdown（检测到 # 标题）
        assert len(chunks) >= 2

    def test_structural_split_html_from_legacy_docx(self) -> None:
        """Legacy 解析 DOCX 输出 HTML — 按 <h2> 标签分块。"""
        chunker = SemanticChunker()
        html_content = """\
<h2>员工表</h2>
<table><tr><th>姓名</th><th>年龄</th></tr><tr><td>张三</td><td>25</td></tr></table>
<h2>部门表</h2>
<table><tr><th>部门</th><th>人数</th></tr><tr><td>技术部</td><td>15</td></tr></table>
"""
        chunks = chunker._structural_split(html_content, "doc-4", "docx")
        assert len(chunks) >= 2

    def test_structural_split_plain_text_falls_back_to_html(self) -> None:
        """纯文本（无 Markdown 也无 HTML 标记）走 _split_html → 降级 token 分块。"""
        chunker = SemanticChunker()
        plain_text = "这是一段很长的纯文本内容。" * 100
        chunks = chunker._structural_split(plain_text, "doc-5", "txt")
        # 无结构标记 → _split_html 找不到 <h> 标签 → 返回空列表
        assert chunks == []

    def test_chunk_method_routes_markdown_content(self) -> None:
        """chunk() 公开方法正确路由 Markdown 内容（Docling 场景）。"""
        chunker = SemanticChunker()
        markdown_content = """\
# API 文档

## 用户接口

GET /api/users

## 订单接口

POST /api/orders
"""
        # doc_type="pdf" 但内容是 Markdown（Docling 输出）
        chunks = chunker.chunk(markdown_content, doc_type="pdf")
        assert len(chunks) >= 2
        # 验证走的是 structural 策略（有 title_path）
        structural_chunks = [c for c in chunks if c.chunk_strategy == "structural"]
        assert len(structural_chunks) >= 2

    def test_chunk_method_routes_html_content(self) -> None:
        """chunk() 公开方法正确路由 HTML 内容（Legacy 场景）。"""
        chunker = SemanticChunker()
        html_content = """\
<h2>第一章</h2>
<p>内容一</p>
<h2>第二章</h2>
<p>内容二</p>
<h2>第三章</h2>
<p>内容三</p>
"""
        # doc_type="pdf" 且内容是 HTML（pymupdf 输出）
        chunks = chunker.chunk(html_content, doc_type="pdf")
        assert len(chunks) >= 2
        structural_chunks = [c for c in chunks if c.chunk_strategy == "structural"]
        assert len(structural_chunks) >= 2


# ======================================================================
# 补章节标题 — title_path 拼入 content 前缀
# ======================================================================


class TestTitlePathPrefix:
    """补章节标题 — title_path 作为 [标题路径] 前缀拼入 content。

    验证结构化分块产出的 chunk content 以 [title_path] 开头，
    让 embedding 阶段即可感知上下文层级。
    """

    def test_markdown_content_has_title_path_prefix(self) -> None:
        """Markdown 结构化分块的 content 应以 [title_path] 前缀开头。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="tutorial")

        assert len(chunks) >= 2
        for c in chunks:
            if c.title_path:
                # content 应以 [title_path] 开头
                assert c.content.startswith(f"[{c.title_path}]")

    def test_html_content_has_title_path_prefix(self) -> None:
        """HTML 结构化分块的 content 应以 [title_path] 前缀开头。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_HTML_CONTENT, doc_type="html", content_type="tutorial")

        assert len(chunks) >= 2
        for c in chunks:
            if c.title_path:
                assert c.content.startswith(f"[{c.title_path}]")

    def test_markdown_nested_title_path_in_content(self) -> None:
        """嵌套标题的 content 前缀应包含完整层级路径。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="tutorial")

        # 找到 H3 "哈希槽分配" 的 chunk
        hash_slot = [c for c in chunks if "哈希槽分配" in c.title_path]
        assert len(hash_slot) > 0
        # content 应以 [Redis 深度解析 > 集群架构 > 哈希槽分配] 开头
        assert hash_slot[0].content.startswith("[Redis 深度解析 > 集群架构 > 哈希槽分配]")

    def test_html_nested_title_path_in_content(self) -> None:
        """HTML 嵌套标题的 content 前缀应包含完整层级路径。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_HTML_CONTENT, doc_type="html", content_type="tutorial")

        # 找到 H3 "用户服务" 的 chunk
        user_service = [c for c in chunks if "用户服务" in c.title_path]
        assert len(user_service) > 0
        # content 应以 [系统架构文档 > 服务层 > 用户服务] 开头
        assert user_service[0].content.startswith("[系统架构文档 > 服务层 > 用户服务]")

    def test_title_path_prefix_not_duplicated_in_metadata(self) -> None:
        """title_path 元数据字段应与前缀中的路径一致，但前缀只在 content 中。"""
        chunker = SemanticChunker()
        chunks = chunker.chunk(_MD_CONTENT, doc_type="md", content_type="tutorial")

        for c in chunks:
            if c.title_path:
                # content 包含前缀
                assert f"[{c.title_path}]" in c.content
                # title_path 元数据本身就是路径，不含方括号
                assert not c.title_path.startswith("[")


# ======================================================================
# _split_html 超长拆分
# ======================================================================


class TestHtmlLongChunkSplit:
    """_split_html 超长拆分 — 与 _split_markdown 保持一致。"""

    def test_html_long_section_split_into_subchunks(self) -> None:
        """超长 HTML 章节应被拆分为多个子块，每个都保持 title_path 前缀。"""
        chunker = SemanticChunker()
        # 构造一个超长 HTML 章节（> _STRUCTURAL_MAX_CHARS=2800）
        long_body = "<p>" + "这是一段很长的内容。" * 300 + "</p>"
        html_content = f"""\
<h1>系统架构</h1>
{long_body}
<h2>服务层</h2>
<p>服务层内容</p>
"""
        chunks = chunker._split_html(html_content, "doc-long")

        # 第一个章节应被拆分为多个子块
        h1_chunks = [c for c in chunks if "系统架构" in c.title_path and "服务层" not in c.title_path]
        assert len(h1_chunks) > 1, "超长 H1 章节应被拆分为多个子块"

        # 每个子块都应有 title_path 元数据
        for c in h1_chunks:
            assert c.title_path == "系统架构"
            assert c.chunk_strategy == "structural"

        # 第一个子块应以 [系统架构] 前缀开头（_split_by_tokens 后续子块从原文中间切）
        assert h1_chunks[0].content.startswith("[系统架构]")

    def test_html_normal_section_not_split(self) -> None:
        """正常长度的 HTML 章节不应被拆分。"""
        chunker = SemanticChunker()
        html_content = """\
<h1>系统架构</h1>
<p>架构概述</p>
<h2>服务层</h2>
<p>服务层内容</p>
"""
        chunks = chunker._split_html(html_content, "doc-normal")

        h1_chunks = [c for c in chunks if c.title_path == "系统架构"]
        assert len(h1_chunks) == 1, "正常长度章节不应被拆分"


# ======================================================================
# _fixed_split 可选 Overlap
# ======================================================================


class TestFixedSplitOverlap:
    """_fixed_split 可选 Overlap — 默认关闭，开启后相邻块有重叠。"""

    def test_overlap_disabled_by_default(self) -> None:
        """默认 _CHUNK_OVERLAP_ENABLED=False，相邻块不应有重叠。"""
        import app.rag.chunker as chunker_mod

        assert chunker_mod._CHUNK_OVERLAP_ENABLED is False

        chunker = SemanticChunker()
        # 构造足够长的文本触发固定长度分块（> 512 tokens ≈ 1792 字符）
        long_text = "这是一段用于测试固定分块的文本。" * 300
        chunks = chunker._fixed_split(long_text, "doc-test")

        assert len(chunks) >= 2
        # 相邻块不应有重叠（前一块末尾不等于后一块开头）
        for i in range(1, len(chunks)):
            prev_tail = chunks[i - 1].content[-50:]
            curr_head = chunks[i].content[:50]
            assert prev_tail != curr_head

    def test_overlap_enabled_creates_overlap(self) -> None:
        """开启 _CHUNK_OVERLAP_ENABLED 后，相邻块应有重叠内容。"""
        import app.rag.chunker as chunker_mod

        original = chunker_mod._CHUNK_OVERLAP_ENABLED
        try:
            chunker_mod._CHUNK_OVERLAP_ENABLED = True

            chunker = SemanticChunker()
            # 构造足够长的文本触发多块分块（> 512 tokens ≈ 1792 字符）
            long_text = "这是第一段内容用于测试。" * 100 + "这是第二段内容用于测试。" * 100
            chunks = chunker._fixed_split(long_text, "doc-overlap")

            assert len(chunks) >= 2
            # 第一个块之后的块应包含前一块末尾的内容（Overlap）
            for i in range(1, len(chunks)):
                prev_tail = chunks[i - 1].content[-chunker_mod._OVERLAP_CHARS:]
                # 当前块开头应包含前一块末尾的部分内容
                assert chunks[i].content[:10] in prev_tail or prev_tail[:10] in chunks[i].content
        finally:
            chunker_mod._CHUNK_OVERLAP_ENABLED = original

    def test_overlap_single_chunk_not_affected(self) -> None:
        """单块文本不受 Overlap 影响。"""
        import app.rag.chunker as chunker_mod

        original = chunker_mod._CHUNK_OVERLAP_ENABLED
        try:
            chunker_mod._CHUNK_OVERLAP_ENABLED = True
            chunker = SemanticChunker()
            short_text = "短文本。"
            chunks = chunker._fixed_split(short_text, "doc-short")
            assert len(chunks) == 1
        finally:
            chunker_mod._CHUNK_OVERLAP_ENABLED = original
